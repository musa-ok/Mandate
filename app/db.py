"""Denetim kaydi: her talep, yonlendirme, karar, insan onayi ve sonuc kalici tutulur."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, String, Text, create_engine, func, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.config import get_settings

settings = get_settings()
_engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    pool_pre_ping=True,  # PostgreSQL: yeniden baslayan sunucudan kalan olu baglantilar
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # SQLite tz tutmaz: UTC naive


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # = LangGraph thread_id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    requester: Mapped[str] = mapped_column(String(120), default="anonymous")
    source: Mapped[str] = mapped_column(String(32), index=True, default="")
    zone: Mapped[str] = mapped_column(String(16), index=True, default="external")
    request_text: Mapped[str] = mapped_column(Text)
    attachment_name: Mapped[str] = mapped_column(String(255), default="")

    status: Mapped[str] = mapped_column(String(24), index=True, default="running")
    route: Mapped[str] = mapped_column(String(32), default="")
    route_reasoning: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    data_json: Mapped[str] = mapped_column(Text, default="{}")
    context_json: Mapped[str] = mapped_column(Text, default="[]")
    pending_action_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_json: Mapped[str] = mapped_column(Text, default="[]")

    llm_provider: Mapped[str] = mapped_column(String(24), default="")
    llm_model: Mapped[str] = mapped_column(String(80), default="")


# JSON alanlari: kolon adi -> (varsayilan)
_JSON_FIELDS = {
    "data": "{}",
    "context": "[]",
    "pending_action": None,
    "approval": None,
    "action_result": None,
    "trace": "[]",
}


def init_db() -> None:
    Base.metadata.create_all(_engine)


@contextmanager
def session_scope():
    # expire_on_commit=False: commit sonrasi nesne alanlari okunabilir kalsin
    session = Session(_engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_run(
    run_id: str, text: str, requester: str, attachment_name: str, llm: dict[str, Any],
    source: str = "external_web",
) -> None:
    from app.security import zone_of

    with session_scope() as s:
        s.add(
            Run(
                id=run_id,
                request_text=text,
                requester=requester,
                source=source,
                zone=zone_of(source),
                attachment_name=attachment_name,
                llm_provider=llm.get("provider", ""),
                llm_model=llm.get("model", ""),
            )
        )


def update_run(run_id: str, **fields: Any) -> None:
    """JSON alanlari (data, pending_action, ...) dict/list olarak verilir; burada serilestirilir."""
    values: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _JSON_FIELDS:
            values[f"{key}_json"] = None if value is None else json.dumps(value, ensure_ascii=False, default=str)
        else:
            values[key] = value
    values["updated_at"] = _now()
    with session_scope() as s:
        s.execute(update(Run).where(Run.id == run_id).values(**values))


def claim_for_decision(run_id: str) -> bool:
    """awaiting_approval -> running gecisini ATOMIK yapar.

    Iki inceleyici ayni anda karar verirse yalnizca biri True alir; digeri 409 gorur.
    """
    with session_scope() as s:
        result = s.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == "awaiting_approval")
            .values(status="running", updated_at=_now())
        )
        return result.rowcount == 1


def get_run(run_id: str) -> Run | None:
    with session_scope() as s:
        row = s.get(Run, run_id)
        if row is not None:
            s.expunge(row)
        return row


def list_runs(status: str | None = None, limit: int = 50, requester: str | None = None) -> list[Run]:
    with session_scope() as s:
        query = select(Run).order_by(Run.created_at.desc()).limit(limit)
        if status:
            query = query.where(Run.status == status)
        if requester is not None:
            query = query.where(func.lower(Run.requester) == requester.lower())
        rows = list(s.scalars(query))
        for r in rows:
            s.expunge(r)
        return rows


def load_json(text: str | None, default: Any = None) -> Any:
    return json.loads(text) if text else default
