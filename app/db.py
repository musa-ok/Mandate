"""Her otonom karari ve zincir islemini kalici olarak kaydeden log katmani."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.config import get_settings

settings = get_settings()
_engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)


class Base(DeclarativeBase):
    pass


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    requester: Mapped[str] = mapped_column(String(120), default="anonymous")
    request_text: Mapped[str] = mapped_column(Text)

    decision: Mapped[str] = mapped_column(String(16))
    reasoning: Mapped[str] = mapped_column(Text, default="")
    cited_rules_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    injection_detected: Mapped[bool] = mapped_column(Boolean, default=False)

    policy_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_violations_json: Mapped[str] = mapped_column(Text, default="[]")

    recipient_wallet: Mapped[str] = mapped_column(String(64), default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USDC")

    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tx_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    tx_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    tx_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    context_json: Mapped[str] = mapped_column(Text, default="[]")

    # --- JSON alanlari icin yardimcilar ---
    @property
    def cited_rules(self) -> list[str]:
        return json.loads(self.cited_rules_json or "[]")

    @property
    def policy_violations(self) -> list[str]:
        return json.loads(self.policy_violations_json or "[]")

    @property
    def context(self) -> list[dict]:
        return json.loads(self.context_json or "[]")


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


def spent_last_24h(currency: str = "USDC") -> float:
    """Gunluk harcama tavanini zorunlu kilmak icin zincire yazilan tutarlarin toplami."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    with session_scope() as s:
        total = s.scalar(
            select(func.coalesce(func.sum(OperationLog.amount), 0.0)).where(
                OperationLog.created_at >= since,
                OperationLog.currency == currency,
                OperationLog.decision == "ONAY",
                OperationLog.policy_passed.is_(True),
                OperationLog.tx_simulated.is_(False),
                OperationLog.tx_error.is_(None),
            )
        )
    return float(total or 0.0)


def list_operations(limit: int = 50) -> list[OperationLog]:
    with session_scope() as s:
        rows = list(
            s.scalars(select(OperationLog).order_by(OperationLog.id.desc()).limit(limit))
        )
        for r in rows:
            s.expunge(r)
        return rows
