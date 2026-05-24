"""
sync_to_bq.py — SQLite → BigQuery incremental sync pipeline.

Reads Freqtrade's tradesv3.sqlite, extracts closed + open trades,
and upserts them into BigQuery (alpacatrader dataset).

Also syncs XAI narrative JSON files from /user_data/narratives/ to
the trade_narratives table as a resilient backup channel (explanation_engine.py
does a direct push on exit; this catches any that failed due to network issues).

Tables created automatically if they do not exist:
  • alpacatrader.trades            — all trades (closed + open)
  • alpacatrader.daily_summary     — aggregated daily P&L
  • alpacatrader.trade_narratives  — XAI explanations per trade

Runs every 5 minutes from docker-compose bq_sync service.
Can also be run standalone:
    python sync_to_bq.py
"""

import json
import os
import sqlite3
import logging
import datetime
from pathlib import Path

import pandas as pd
from google.cloud import bigquery
from google.cloud.exceptions import NotFound

# ── Configuration ────────────────────────────────────────────
PROJECT        = os.environ.get("GCP_PROJECT_ID", "your-gcp-project-id")
DATASET        = os.environ.get("BQ_DATASET", "alpacatrader")
SQLITE         = os.environ.get("SQLITE_PATH", "/freqtrade/user_data/tradesv3.sqlite")
NARRATIVES_DIR = Path(os.environ.get("FREQTRADE_USER_DIR",
                                      "/freqtrade/user_data")) / "narratives"
LOG_LEVEL      = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bq_sync")

# ── BigQuery Schemas ─────────────────────────────────────────
TRADES_SCHEMA = [
    bigquery.SchemaField("trade_id",          "INTEGER",   mode="REQUIRED"),
    bigquery.SchemaField("exchange",           "STRING"),
    bigquery.SchemaField("pair",               "STRING"),
    bigquery.SchemaField("is_open",            "BOOLEAN"),
    bigquery.SchemaField("fee_open",           "FLOAT"),
    bigquery.SchemaField("fee_open_cost",      "FLOAT"),
    bigquery.SchemaField("fee_open_currency",  "STRING"),
    bigquery.SchemaField("fee_close",          "FLOAT"),
    bigquery.SchemaField("fee_close_cost",     "FLOAT"),
    bigquery.SchemaField("fee_close_currency", "STRING"),
    bigquery.SchemaField("open_rate",          "FLOAT"),
    bigquery.SchemaField("open_rate_requested","FLOAT"),
    bigquery.SchemaField("open_trade_value",   "FLOAT"),
    bigquery.SchemaField("close_rate",         "FLOAT"),
    bigquery.SchemaField("close_rate_requested","FLOAT"),
    bigquery.SchemaField("close_profit",       "FLOAT"),
    bigquery.SchemaField("close_profit_abs",   "FLOAT"),
    bigquery.SchemaField("stake_amount",       "FLOAT"),
    bigquery.SchemaField("amount",             "FLOAT"),
    bigquery.SchemaField("amount_requested",   "FLOAT"),
    bigquery.SchemaField("open_date",          "TIMESTAMP"),
    bigquery.SchemaField("close_date",         "TIMESTAMP"),
    bigquery.SchemaField("stop_loss",          "FLOAT"),
    bigquery.SchemaField("stop_loss_pct",      "FLOAT"),
    bigquery.SchemaField("initial_stop_loss",  "FLOAT"),
    bigquery.SchemaField("initial_stop_loss_pct","FLOAT"),
    bigquery.SchemaField("min_rate",           "FLOAT"),
    bigquery.SchemaField("max_rate",           "FLOAT"),
    bigquery.SchemaField("sell_reason",        "STRING"),
    bigquery.SchemaField("sell_order_status",  "STRING"),
    bigquery.SchemaField("strategy",           "STRING"),
    bigquery.SchemaField("timeframe",          "INTEGER"),
    bigquery.SchemaField("synced_at",          "TIMESTAMP"),
]

NARRATIVES_SCHEMA = [
    bigquery.SchemaField("trade_key",           "STRING",  mode="REQUIRED"),
    bigquery.SchemaField("pair",                "STRING"),
    bigquery.SchemaField("open_date",           "TIMESTAMP"),
    bigquery.SchemaField("close_date",          "TIMESTAMP"),
    bigquery.SchemaField("open_rate",           "FLOAT"),
    bigquery.SchemaField("close_rate",          "FLOAT"),
    bigquery.SchemaField("close_profit_pct",    "FLOAT"),
    bigquery.SchemaField("close_profit_abs",    "FLOAT"),
    bigquery.SchemaField("exit_reason",         "STRING"),
    bigquery.SchemaField("entry_tag",           "STRING"),
    bigquery.SchemaField("regime",              "STRING"),
    bigquery.SchemaField("adx_at_entry",        "FLOAT"),
    bigquery.SchemaField("z_score_at_entry",    "FLOAT"),
    bigquery.SchemaField("mfi_at_entry",        "FLOAT"),
    bigquery.SchemaField("vwap_distance_pct",   "FLOAT"),
    bigquery.SchemaField("ema50_slope",         "FLOAT"),
    bigquery.SchemaField("poc_at_entry",        "FLOAT"),
    bigquery.SchemaField("vah_at_entry",        "FLOAT"),
    bigquery.SchemaField("val_at_entry",        "FLOAT"),
    bigquery.SchemaField("bb_width_at_entry",   "FLOAT"),
    bigquery.SchemaField("structural_stop",     "FLOAT"),
    bigquery.SchemaField("liquidity_target",    "FLOAT"),
    bigquery.SchemaField("funding_rate",        "FLOAT"),
    bigquery.SchemaField("narrative_text",      "STRING"),
    bigquery.SchemaField("chart_path",          "STRING"),
    bigquery.SchemaField("synced_at",           "TIMESTAMP"),
]

DAILY_SCHEMA = [
    bigquery.SchemaField("date",              "DATE",     mode="REQUIRED"),
    bigquery.SchemaField("pair",              "STRING"),
    bigquery.SchemaField("trades_count",      "INTEGER"),
    bigquery.SchemaField("wins",              "INTEGER"),
    bigquery.SchemaField("losses",            "INTEGER"),
    bigquery.SchemaField("win_rate",          "FLOAT"),
    bigquery.SchemaField("total_profit_pct",  "FLOAT"),
    bigquery.SchemaField("total_profit_usd",  "FLOAT"),
    bigquery.SchemaField("avg_profit_pct",    "FLOAT"),
    bigquery.SchemaField("max_profit_pct",    "FLOAT"),
    bigquery.SchemaField("max_loss_pct",      "FLOAT"),
    bigquery.SchemaField("total_volume_usd",  "FLOAT"),
    bigquery.SchemaField("synced_at",         "TIMESTAMP"),
]


def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT)


def ensure_dataset(client: bigquery.Client):
    ds_ref = bigquery.DatasetReference(PROJECT, DATASET)
    try:
        client.get_dataset(ds_ref)
        log.debug("Dataset %s.%s already exists.", PROJECT, DATASET)
    except NotFound:
        ds = bigquery.Dataset(ds_ref)
        ds.location = "EU"
        client.create_dataset(ds)
        log.info("Created dataset %s.%s", PROJECT, DATASET)


def ensure_table(client: bigquery.Client, table_id: str, schema: list):
    full_id = f"{PROJECT}.{DATASET}.{table_id}"
    try:
        client.get_table(full_id)
        log.debug("Table %s already exists.", full_id)
    except NotFound:
        table = bigquery.Table(full_id, schema=schema)
        client.create_table(table)
        log.info("Created table %s", full_id)


def read_sqlite_trades() -> pd.DataFrame:
    """Read all trades from Freqtrade SQLite database."""
    if not Path(SQLITE).exists():
        log.warning("SQLite file not found: %s", SQLITE)
        return pd.DataFrame()

    conn = sqlite3.connect(SQLITE)
    try:
        query = """
            SELECT
                id                      AS trade_id,
                exchange,
                pair,
                is_open,
                fee_open,
                fee_open_cost,
                fee_open_currency,
                fee_close,
                fee_close_cost,
                fee_close_currency,
                open_rate,
                open_rate_requested,
                open_trade_value,
                close_rate,
                close_rate_requested,
                close_profit,
                close_profit_abs,
                stake_amount,
                amount,
                amount_requested,
                open_date,
                close_date,
                stop_loss,
                stop_loss_pct,
                initial_stop_loss,
                initial_stop_loss_pct,
                min_rate,
                max_rate,
                sell_reason,
                sell_order_status,
                strategy,
                timeframe
            FROM trades
        """
        df = pd.read_sql_query(query, conn)
    except Exception as e:
        log.error("Failed to read SQLite trades: %s", e)
        return pd.DataFrame()
    finally:
        conn.close()

    if df.empty:
        return df

    # Parse timestamps
    for col in ("open_date", "close_date"):
        df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)

    df["synced_at"] = datetime.datetime.now(datetime.timezone.utc)
    df["is_open"]   = df["is_open"].astype(bool)

    return df


def build_daily_summary(trades_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate closed trades into daily P&L rows."""
    closed = trades_df[~trades_df["is_open"] & trades_df["close_date"].notna()].copy()
    if closed.empty:
        return pd.DataFrame()

    closed["date"] = closed["close_date"].dt.date

    grp = closed.groupby(["date", "pair"])
    summary = grp.agg(
        trades_count   = ("trade_id",       "count"),
        wins           = ("close_profit",    lambda x: (x > 0).sum()),
        losses         = ("close_profit",    lambda x: (x <= 0).sum()),
        total_profit_pct=("close_profit",    "sum"),
        total_profit_usd=("close_profit_abs","sum"),
        avg_profit_pct = ("close_profit",    "mean"),
        max_profit_pct = ("close_profit",    "max"),
        max_loss_pct   = ("close_profit",    "min"),
        total_volume_usd=("open_trade_value","sum"),
    ).reset_index()

    summary["win_rate"]  = summary["wins"] / summary["trades_count"].replace(0, 1)
    summary["synced_at"] = datetime.datetime.now(datetime.timezone.utc)
    summary["date"]      = pd.to_datetime(summary["date"])

    return summary


def upsert_to_bq(client: bigquery.Client, df: pd.DataFrame,
                 table_id: str, merge_key: str):
    """
    Upsert DataFrame rows into BigQuery using a MERGE-like approach:
    write to a temp table then MERGE into the target.
    Falls back to simple load_table_from_dataframe for new tables.
    """
    if df.empty:
        log.debug("Nothing to upsert into %s.", table_id)
        return

    full_id  = f"{PROJECT}.{DATASET}.{table_id}"
    temp_id  = f"{PROJECT}.{DATASET}.{table_id}_tmp"

    # Write to temp
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    client.load_table_from_dataframe(df, temp_id, job_config=job_config).result()
    log.debug("Wrote %d rows to temp table %s", len(df), temp_id)

    # Build column list for MERGE UPDATE SET
    non_key_cols = [c for c in df.columns if c != merge_key]
    update_set   = ", ".join([f"T.{c} = S.{c}" for c in non_key_cols])
    insert_cols  = ", ".join(df.columns)
    insert_vals  = ", ".join([f"S.{c}" for c in df.columns])

    merge_sql = f"""
        MERGE `{full_id}` T
        USING `{temp_id}` S
        ON T.{merge_key} = S.{merge_key}
        WHEN MATCHED THEN
            UPDATE SET {update_set}
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals})
    """

    try:
        client.query(merge_sql).result()
        log.info("Upserted %d rows into %s", len(df), full_id)
    except Exception as e:
        log.error("MERGE failed for %s: %s", table_id, e)
        # Fallback: append
        job_config2 = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
        client.load_table_from_dataframe(df, full_id, job_config=job_config2).result()
        log.info("Fallback append: %d rows into %s", len(df), full_id)
    finally:
        # Drop temp table
        client.delete_table(temp_id, not_found_ok=True)


def read_narrative_jsons() -> pd.DataFrame:
    """
    Scan /user_data/narratives/*.json and return a DataFrame of all snapshots.
    Acts as a resilient backup — explanation_engine pushes directly, but
    network hiccups can leave files unsynced.
    """
    NARRATIVES_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in sorted(NARRATIVES_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            # Only include closed trades (close_date populated)
            if data.get("close_date"):
                rows.append(data)
        except Exception as e:
            log.warning("Failed to read narrative %s: %s", p, e)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Ensure trade_key column
    if "trade_key" not in df.columns:
        df["trade_key"] = df.apply(
            lambda r: f"{str(r.get('pair','')).replace('/','_')}_{str(r.get('open_date',''))[:19].replace(':','').replace('-','').replace('T','_')}",
            axis=1
        )

    # Parse timestamps
    for col in ("open_date", "close_date", "synced_at"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)

    # Ensure all schema columns are present
    schema_cols = [f.name for f in NARRATIVES_SCHEMA]
    for col in schema_cols:
        if col not in df.columns:
            df[col] = None

    df["synced_at"] = datetime.datetime.now(datetime.timezone.utc)
    return df[schema_cols]


def run_sync():
    log.info("=== BigQuery sync started ===")

    # 1. Read SQLite
    trades_df = read_sqlite_trades()
    if trades_df.empty:
        log.info("No trades found in SQLite — nothing to sync.")
    else:
        log.info("Read %d trades from SQLite.", len(trades_df))

    # 2. Connect to BQ
    client = get_bq_client()
    ensure_dataset(client)
    ensure_table(client, "trades",           TRADES_SCHEMA)
    ensure_table(client, "daily_summary",    DAILY_SCHEMA)
    ensure_table(client, "trade_narratives", NARRATIVES_SCHEMA)

    if not trades_df.empty:
        # 3. Upsert trades
        upsert_to_bq(client, trades_df, "trades", "trade_id")

        # 4. Build & upsert daily summary
        daily_df = build_daily_summary(trades_df)
        if not daily_df.empty:
            upsert_to_bq(client, daily_df, "daily_summary", "date")
            log.info("Daily summary: %d date-pair rows.", len(daily_df))

    # 5. Sync XAI narratives (JSON backup channel)
    narratives_df = read_narrative_jsons()
    if not narratives_df.empty:
        log.info("Syncing %d XAI narrative snapshots.", len(narratives_df))
        upsert_to_bq(client, narratives_df, "trade_narratives", "trade_key")
    else:
        log.info("No narrative snapshots to sync.")

    log.info("=== BigQuery sync complete ===")


if __name__ == "__main__":
    run_sync()
