"""LLM'den BAGIMSIZ, kod ile zorunlu kilinan harcama guvenlik katmani.

Neden gerekli: bir dil modeli ikna edilebilir, bir if blogu edilemez. Ajan
"ONAY" dese bile para ancak bu kontrollerin tamamindan gecerse hareket eder.
"""
from __future__ import annotations

import re

from app.config import get_settings
from app.db import spent_last_24h
from app.schemas import AgentDecision, Currency, PolicyCheck

settings = get_settings()

# Solana adresleri base58'dir: 0, O, I, l karakterlerini icermez.
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _valid_wallet(address: str) -> bool:
    if not _BASE58_RE.match(address or ""):
        return False
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(address)
        return True
    except Exception:
        return False


def enforce(decision: AgentDecision) -> PolicyCheck:
    """Onaylanmis bir karari zincire gondermeden once son kez dogrular."""
    violations: list[str] = []
    payment = decision.payment

    if not _valid_wallet(payment.recipient_wallet):
        violations.append(
            f"Gecersiz Solana cuzdan adresi: '{payment.recipient_wallet or '(bos)'}'"
        )

    if payment.amount <= 0:
        violations.append(f"Tutar pozitif olmali, gelen deger: {payment.amount}")

    allowlist = settings.allowlist
    if allowlist and payment.recipient_wallet not in allowlist:
        violations.append(
            "Alici cuzdan kurumsal beyaz listede degil "
            f"({len(allowlist)} kayitli adres var)."
        )

    if payment.currency == Currency.USDC:
        if payment.amount > settings.max_single_payment_usdc:
            violations.append(
                f"Tek islem limiti asildi: {payment.amount} USDC > "
                f"{settings.max_single_payment_usdc} USDC"
            )
        spent = spent_last_24h("USDC")
        if spent + payment.amount > settings.max_daily_payment_usdc:
            violations.append(
                f"24 saatlik harcama tavani asilir: son 24s {spent} USDC + "
                f"{payment.amount} USDC > {settings.max_daily_payment_usdc} USDC"
            )

    if decision.injection_detected:
        violations.append("Karar surecinde prompt injection tespit edildi.")

    return PolicyCheck(passed=not violations, violations=violations)
