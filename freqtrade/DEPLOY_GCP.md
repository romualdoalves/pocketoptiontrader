# AlpacaTrader — GCP Deployment Guide
Region: europe-west8 (Milan)

## 1. Prerequisites

- Google Cloud account with billing enabled
- `gcloud` CLI installed locally: https://cloud.google.com/sdk/docs/install
- Docker + Docker Compose installed on the VM

---

## 2. Create a GCP Project

```bash
gcloud projects create alpacatrader-prod --name="AlpacaTrader"
gcloud config set project alpacatrader-prod
gcloud services enable bigquery.googleapis.com compute.googleapis.com
```

---

## 3. Create a Service Account for BigQuery

```bash
gcloud iam service-accounts create freqtrade-bq \
  --display-name="FreqtradeBigQuery"

gcloud projects add-iam-policy-binding alpacatrader-prod \
  --member="serviceAccount:freqtrade-bq@alpacatrader-prod.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding alpacatrader-prod \
  --member="serviceAccount:freqtrade-bq@alpacatrader-prod.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"

# Download the key — upload this to your VM as /secrets/gcp_service_account.json
gcloud iam service-accounts keys create gcp_service_account.json \
  --iam-account=freqtrade-bq@alpacatrader-prod.iam.gserviceaccount.com
```

---

## 4. Create the VM (e2-micro is free-tier eligible)

```bash
gcloud compute instances create alpacatrader-vm \
  --project=alpacatrader-prod \
  --zone=europe-west8-a \
  --machine-type=e2-small \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=20GB \
  --tags=http-server,https-server \
  --metadata=startup-script='
    apt-get update -y
    apt-get install -y docker.io docker-compose git
    systemctl enable docker
    systemctl start docker
  '

# Allow ports 8080 (Freqtrade UI) and 3000 (Grafana)
gcloud compute firewall-rules create allow-trading-ports \
  --allow=tcp:8080,tcp:3000 \
  --target-tags=http-server \
  --description="AlpacaTrader Freqtrade + Grafana"
```

---

## 5. Upload Files to VM

```bash
# From your local machine
INSTANCE=alpacatrader-vm
ZONE=europe-west8-a

# Copy the entire freqtrade folder
gcloud compute scp --recurse ./freqtrade ${INSTANCE}:~/alpacatrader --zone=${ZONE}

# Copy the service account key
gcloud compute scp gcp_service_account.json ${INSTANCE}:~/alpacatrader/freqtrade/secrets/gcp_service_account.json --zone=${ZONE}
```

---

## 6. Configure .env on the VM

```bash
gcloud compute ssh ${INSTANCE} --zone=${ZONE}

cd ~/alpacatrader/freqtrade
cp .env.example .env
nano .env
# Fill in:
#   ALPACA_API_KEY=PKBB5R5OMYTDOWOZ2SOFVDRTDU
#   ALPACA_API_SECRET=<your secret>
#   GCP_PROJECT_ID=alpacatrader-prod
#   FREQTRADE_JWT_SECRET=<random 64-char hex>
#   FREQTRADE_API_PASSWORD=<strong password>
#   GRAFANA_ADMIN_PASSWORD=<strong password>
```

---

## 7. Install Grafana BigQuery Plugin

The BigQuery datasource requires the DoiT BigQuery plugin. Add this to `docker-compose.yml`
under the `grafana` service environment:

```yaml
environment:
  - GF_INSTALL_PLUGINS=doitintl-bigquery-datasource
```

This is already included in the provided `docker-compose.yml`.

---

## 8. Start the Stack

```bash
cd ~/alpacatrader/freqtrade
docker compose up -d

# Watch logs
docker compose logs -f freqtrade
docker compose logs -f bq_sync
```

---

## 9. Access the Services

| Service       | URL                                    | Credentials                     |
|---------------|----------------------------------------|----------------------------------|
| Freqtrade UI  | http://<VM_EXTERNAL_IP>:8080           | freqtrade / FREQTRADE_API_PASSWORD |
| Grafana       | http://<VM_EXTERNAL_IP>:3000           | admin / GRAFANA_ADMIN_PASSWORD   |

Get VM external IP:
```bash
gcloud compute instances describe alpacatrader-vm \
  --zone=europe-west8-a \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

---

## 10. Switch from Dry-Run to Live

In `config.json`, change:
```json
"dry_run": false
```

Then restart:
```bash
docker compose restart freqtrade
```

---

## 11. Enable Auto-Restart on VM Reboot

```bash
# On the VM
sudo systemctl enable docker
cd ~/alpacatrader/freqtrade

# Create systemd service
sudo tee /etc/systemd/system/alpacatrader.service > /dev/null <<EOF
[Unit]
Description=AlpacaTrader Docker Stack
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/home/${USER}/alpacatrader/freqtrade
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=always
User=${USER}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable alpacatrader
sudo systemctl start alpacatrader
```

---

## 12. Estimated GCP Costs (europe-west8)

| Resource             | Spec           | Est. Monthly Cost |
|----------------------|----------------|-------------------|
| e2-small VM          | 2 vCPU, 2 GB   | ~$12 USD          |
| Persistent disk 20GB | SSD            | ~$3 USD           |
| BigQuery storage     | <1 GB          | ~$0 USD (free)    |
| BigQuery queries     | <1 TB/month    | ~$0 USD (free)    |
| **Total**            |                | **~$15/month**    |

Alternatively use **e2-micro** (~$6/month) — sufficient for paper trading.
