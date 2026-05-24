"""
Gerenciamento de sessão SQLAlchemy para PostgreSQL.
DATABASE_URL: postgresql://crypto:crypto@postgres:5432/pocketoption
"""
import os
import logging
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from .models import Base, Configuracao

logger = logging.getLogger(__name__)

_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://crypto:crypto@localhost:5432/pocketoption",
)

_engine = create_engine(
    _DATABASE_URL,
    pool_pre_ping=True,   # reconecta automaticamente se a conexão cair
    pool_size=5,
    max_overflow=10,
)

_SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)


def init_db() -> None:
    """Cria as tabelas se não existirem e garante configuração inicial."""
    Base.metadata.create_all(_engine)
    with get_session() as session:
        if not session.query(Configuracao).first():
            default = Configuracao()
            session.add(default)
            session.commit()
            logger.info("Configuração padrão criada no banco de dados")


@contextmanager
def get_session():
    """Context manager que fornece uma Session com commit/rollback automático."""
    session: Session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
