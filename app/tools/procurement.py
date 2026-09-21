"""Onayli tedarikci listesi kaydi (MOCK). Gercekte ERP / tedarikci yonetim sistemine yazilir."""
from __future__ import annotations

from typing import Any

_RECORDED: dict[str, dict[str, Any]] = {}


def record_vendor_approval(vendor: str, score: int, reviewer: str, idempotency_key: str) -> dict[str, Any]:
    if idempotency_key in _RECORDED:
        return _RECORDED[idempotency_key] | {"replayed": True}
    result = {"ok": True, "mock": True, "vendor": vendor, "score": score, "reviewer": reviewer,
              "message": f"{vendor} onayli tedarikci listesine eklendi (SAHTE islem)."}
    _RECORDED[idempotency_key] = result
    return result
