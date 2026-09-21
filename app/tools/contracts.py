"""Sozlesme kararinin kaydi (MOCK). Gercekte CLM / e-imza sistemine yazilir."""
from __future__ import annotations

from typing import Any

_RECORDED: dict[str, dict[str, Any]] = {}


def record_contract_decision(
    contract_name: str,
    decision: str,
    reviewer: str,
    risk_level: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if idempotency_key in _RECORDED:
        return _RECORDED[idempotency_key] | {"replayed": True}
    result = {
        "ok": True,
        "mock": True,
        "contract": contract_name,
        "decision": decision,
        "reviewer": reviewer,
        "risk_level": risk_level,
        "message": f"'{contract_name}' sozlesmesi {reviewer} onayiyla imzaya iletildi (SAHTE islem).",
    }
    _RECORDED[idempotency_key] = result
    return result
