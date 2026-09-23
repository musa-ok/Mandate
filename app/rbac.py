"""Rol bazli erisim ve onay matrisi.

ROLLER kisilere IdP'den (Entra ID / Okta / Google Workspace) gelir; hangi rolun hangi
eylemi hangi kosulda onaylayabilecegi ise kodda degil config/approval_policy.json'dadir.
Sirket matrisini degistirmek icin kod degismez.

Kurallar sirayla denenir; ILK eslesen kural gecerlidir. Hicbir kural eslesmezse eylemi
KIMSE onaylayamaz (fail-closed). Matris baslangicta dogrulanir: bilinmeyen rol, kosul veya
kurali olmayan eylem turu varsa uygulama baslamaz.

Gorev ayriligi: `admin` rolu sistemi yonetir (bilgi tabani) ama hicbir eylemi onaylayamaz.
Dort goz: hicbir kisi kendi talebini onaylayamaz (bkz. service.decide).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings

ROLES: dict[str, str] = {
    "employee": "Calisan",
    "it_admin": "IT yoneticisi",
    "legal": "Hukuk",
    "finance_manager": "Finans yoneticisi",
    "cfo": "CFO",
    "procurement_manager": "Satin alma yoneticisi",
    "auditor": "Denetci",
    "admin": "Sistem yoneticisi",
}
APPROVER_ROLES = frozenset({"it_admin", "legal", "finance_manager", "cfo", "procurement_manager"})

# Izinler
REQUESTS_CREATE = "requests:create"
RUNS_READ_OWN = "runs:read_own"
RUNS_READ_ALL = "runs:read_all"
APPROVALS_DECIDE = "approvals:decide"
MEMORY_READ = "memory:read"
MEMORY_WRITE = "memory:write"
SYSTEM_READ = "system:read"

_EMPLOYEE = {REQUESTS_CREATE, RUNS_READ_OWN, MEMORY_READ, SYSTEM_READ}
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "employee": frozenset(_EMPLOYEE),
    **{r: frozenset({APPROVALS_DECIDE, RUNS_READ_ALL, SYSTEM_READ}) for r in APPROVER_ROLES},
    "auditor": frozenset({RUNS_READ_ALL, SYSTEM_READ}),
    "admin": frozenset({MEMORY_READ, MEMORY_WRITE, SYSTEM_READ}),
}
# Ic entegrasyonlar (Slack/Teams botu, X-Source-Key): talep acar ve sonucunu okur; ONAYLAYAMAZ.
INTEGRATION_PERMISSIONS = frozenset({REQUESTS_CREATE, RUNS_READ_ALL, MEMORY_READ, SYSTEM_READ})

ACTION_KINDS = ("tool_call", "contract_signoff", "expense_approval", "vendor_approval")
_CONDITIONS = {"amount_gt", "amount_lte", "over_budget", "risk_level_in", "tool_in"}


def permissions_for(roles: frozenset[str] | set[str]) -> frozenset[str]:
    out: set[str] = set()
    for role in roles:
        out |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(out)


class PolicyError(ValueError):
    """Onay matrisi gecersiz: uygulama baslamamali."""


@dataclass(frozen=True)
class Rule:
    id: str
    action: str
    roles: frozenset[str]
    when: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def matches(self, facts: dict[str, Any]) -> bool:
        if facts.get("kind") != self.action:
            return False
        for cond, expected in self.when.items():
            amount = facts.get("amount")
            if cond == "amount_gt" and not (amount is not None and amount > expected):
                return False
            if cond == "amount_lte" and not (amount is not None and amount <= expected):
                return False
            if cond == "over_budget" and bool(facts.get("over_budget")) is not expected:
                return False
            if cond == "risk_level_in" and facts.get("risk_level") not in expected:
                return False
            if cond == "tool_in" and facts.get("tool") not in expected:
                return False
        return True

    def to_view(self) -> dict[str, Any]:
        return {"rule": self.id, "roles": sorted(self.roles), "description": self.description}


def _parse_rule(i: int, raw: Any) -> Rule:
    where = f"kural #{i + 1}"
    if not isinstance(raw, dict):
        raise PolicyError(f"{where}: nesne olmali")
    rule_id = str(raw.get("id") or "").strip()
    if not rule_id:
        raise PolicyError(f"{where}: 'id' zorunlu")
    where = f"kural '{rule_id}'"
    action = raw.get("action")
    if action not in ACTION_KINDS:
        raise PolicyError(f"{where}: bilinmeyen eylem {action!r} (gecerli: {', '.join(ACTION_KINDS)})")
    roles = raw.get("roles")
    if not isinstance(roles, list) or not roles:
        raise PolicyError(f"{where}: 'roles' bos olmayan bir liste olmali")
    unknown = [r for r in roles if r not in APPROVER_ROLES]
    if unknown:
        raise PolicyError(f"{where}: onay veremeyen/bilinmeyen rol: {unknown} (onaylayici roller: {sorted(APPROVER_ROLES)})")
    when = raw.get("when") or {}
    if not isinstance(when, dict):
        raise PolicyError(f"{where}: 'when' nesne olmali")
    for cond, value in when.items():
        if cond not in _CONDITIONS:
            raise PolicyError(f"{where}: bilinmeyen kosul {cond!r} (gecerli: {sorted(_CONDITIONS)})")
        if cond in ("amount_gt", "amount_lte") and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise PolicyError(f"{where}: {cond} sayi olmali")
        if cond == "over_budget" and not isinstance(value, bool):
            raise PolicyError(f"{where}: over_budget true/false olmali")
        if cond in ("risk_level_in", "tool_in") and not (isinstance(value, list) and value):
            raise PolicyError(f"{where}: {cond} bos olmayan bir liste olmali")
    return Rule(rule_id, action, frozenset(roles), dict(when), str(raw.get("description") or ""))


@dataclass(frozen=True)
class ApprovalPolicy:
    rules: tuple[Rule, ...]

    @classmethod
    def from_dict(cls, raw: Any) -> ApprovalPolicy:
        if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list):
            raise PolicyError("matris {'rules': [...]} biciminde olmali")
        rules = tuple(_parse_rule(i, r) for i, r in enumerate(raw["rules"]))
        ids = [r.id for r in rules]
        dup = sorted({x for x in ids if ids.count(x) > 1})
        if dup:
            raise PolicyError(f"tekrarlanan kural id: {dup}")
        missing = [k for k in ACTION_KINDS if not any(r.action == k for r in rules)]
        if missing:
            raise PolicyError(f"su eylem turleri icin kural yok (kimse onaylayamaz): {missing}")
        return cls(rules)

    def rule_for(self, pending_action: dict[str, Any] | None, data: dict[str, Any] | None) -> Rule | None:
        if not pending_action:
            return None
        facts = action_facts(pending_action, data or {})
        return next((r for r in self.rules if r.matches(facts)), None)


def action_facts(pending_action: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Kurallarin baktigi olgular. Tutar ve butce durumu KODUN hesapladigi alanlardan gelir."""
    args = pending_action.get("arguments") or {}
    details = pending_action.get("details") or {}
    amount = args.get("amount")
    return {
        "kind": pending_action.get("kind"),
        "tool": pending_action.get("tool"),
        "amount": float(amount) if isinstance(amount, (int, float)) and not isinstance(amount, bool) else None,
        "over_budget": bool(data.get("over_budget")),
        "risk_level": details.get("risk_level") or pending_action.get("risk_level"),
    }


def load_policy(path: str | Path) -> ApprovalPolicy:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"onay matrisi bulunamadi: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(f"onay matrisi gecerli JSON degil: {exc}") from exc
    return ApprovalPolicy.from_dict(raw)


@lru_cache
def get_policy() -> ApprovalPolicy:
    return load_policy(get_settings().approval_policy_path)
