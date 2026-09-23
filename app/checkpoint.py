"""LangGraph checkpoint deposu: bekleyen onaylar burada yasar.

CHECKPOINT_DATABASE_URL bos -> tek dosyali SQLite (gelistirme / tek sunucu).
Dolu -> PostgreSQL; birden fazla uygulama kopyasi ayni onay kuyrugunu paylasir.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import get_settings


def postgres_conninfo(url: str) -> str:
    """psycopg surucu ekini kaldirir: SQLAlchemy bicimindeki URL de kabul edilsin."""
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[object]:
    settings = get_settings()
    if settings.checkpoint_database_url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(
            postgres_conninfo(settings.checkpoint_database_url),
            max_size=10,
            # AsyncPostgresSaver'in bekledigi baglanti ayarlari
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            open=False,
        ) as pool:
            saver = AsyncPostgresSaver(pool)
            await saver.setup()  # tablolari olusturur / surum gecislerini uygular (idempotent)
            yield saver
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_db_path) as saver:
            yield saver
