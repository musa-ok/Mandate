r"""Mandate: LangGraph coklu ajan agi.

    START -> guard --(injection)--------------------------------------> END
               |
               v
             router (supervisor) --(unsupported)-----------------------> END
               |   zone=external -> YALNIZCA customer_support (kod kurali, 2 katman)
               |
               +--> data_analyst ---------> END   (salt okunur, onay gerekmez)
               +--> customer_support -----> END   (yalnizca oneri, eylem yok)
               +--> it_ops ------------+
               +--> contract_analyst --+
               +--> finance -----------+--(pending_action yoksa)--> END
               +--> procurement -------+
                        |
        (pending_action varsa)
                        v
                 human_approval   <-- interrupt(): grafik BURADA durur
                        |
             +----------+----------+
             v                     v
       execute_action         cancel_action  --> END

Tasarim kararlari
-----------------
* Ajanlar EYLEMI YURUTMEZ, yalnizca `pending_action` ONERIR. Yurutme tek bir
  yerde (`execute_action`) ve yalnizca insan onayindan sonra olur. Boylece
  "kritik eylem onaysiz calisir" hatasi yapisal olarak imkansizdir.
* `human_approval` dugumu bilerek MINIK tutulur. LangGraph, interrupt sonrasi
  dugumu BASTAN calistirir; bu dugumde yan etki olsaydi iki kez olusurdu. Yan
  etkili her sey (arac calistirma) interrupt'tan SONRAKI ayri dugumdedir.
* Durum yalnizca duz JSON tipleri tasir (dict/list/str). Checkpoint SQLite'a
  yazildigi icin bekleyen onaylar sunucu yeniden baslasa da kaybolmaz.
* Insan karari `approved is True` degilse RED sayilir (fail-closed).
"""
from __future__ import annotations

import asyncio
import operator
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.contract_analyst import contract_analyst_node
from app.agent.customer_support import customer_support_node
from app.agent.data_analyst import data_analyst_node
from app.agent.finance import finance_node
from app.agent.guardrails import guard_node
from app.agent.it_ops import it_ops_node
from app.agent.procurement import procurement_node
from app.agent.router import router_node
from app.config import get_settings
from app.security import zone_of
from app.tools.contracts import record_contract_decision
from app.tools.finance_db import record_expense
from app.tools.it_tools import execute_tool
from app.tools.procurement import record_vendor_approval

# Router'in secebilecegi uzman ajanlar (dugum adi == route degeri)
SPECIALISTS = (
    "it_ops", "data_analyst", "contract_analyst", "finance", "procurement", "customer_support",
)
# Eylem ONEREBILEN ajanlar: ciktilari human_approval kapisindan gecer
ACTION_AGENTS = ("it_ops", "contract_analyst", "finance", "procurement")
# Salt okunur / yalnizca oneri ureten ajanlar: dogrudan END
READ_ONLY_AGENTS = ("data_analyst", "customer_support")
# DIS bolgeden gelen bir talebin ulasabilecegi TEK ajan
EXTERNAL_ALLOWED = ("customer_support",)


class OSState(TypedDict, total=False):
    # --- girdi ---
    run_id: str
    request_text: str
    requester: str
    source: str  # internal_panel, internal_slack, external_web, ... (kimlik bilgisinden)
    zone: str  # internal | external - KODLA hesaplanir, disaridan verilmez
    attachment_name: str
    attachment_text: str

    # --- guard + router ---
    injection_hits: list[str]
    route: str  # it_ops | data_analyst | contract_analyst | unsupported | blocked
    route_reasoning: str
    route_confidence: float

    # --- uzman ajan ciktilari ---
    answer: str
    data: dict[str, Any]  # ajana ozgu yapisal sonuc (sql+satirlar, risk raporu, ...)
    context: list[dict[str, Any]]  # kullanilan RAG parcalari

    # --- insan onayi (HITL) ---
    pending_action: dict[str, Any] | None
    approval: dict[str, Any] | None
    action_result: dict[str, Any] | None

    # --- Dinamik Bilissel Yonlendirme: uzman ajanin fiilen kullandigi model ---
    llm: dict[str, Any]

    # --- sonuc ---
    status: str  # completed | rejected | blocked | unsupported | needs_input | failed
    error: str
    trace: Annotated[list[str], operator.add]


def initial_state(
    run_id: str,
    text: str,
    requester: str = "anonymous",
    attachment_name: str = "",
    attachment_text: str = "",
    source: str = "external_web",
) -> OSState:
    return OSState(
        run_id=run_id,
        request_text=text,
        requester=requester,
        source=source,
        zone=zone_of(source),  # bilinmeyen kaynak -> external (fail-closed)
        attachment_name=attachment_name,
        attachment_text=attachment_text,
        trace=[],
    )


# --------------------------------------------------------------------------
# Insan onayi
# --------------------------------------------------------------------------
def normalize_approval(raw: Any) -> dict[str, Any]:
    """Insan kararini guvenli bicime cevirir. Sadece acik `True` ONAY sayilir."""
    if not isinstance(raw, dict):
        return {"approved": False, "reviewer": "unknown", "comment": "gecersiz karar bicimi"}
    out: dict[str, Any] = {
        "approved": raw.get("approved") is True,
        "reviewer": str(raw.get("reviewer") or "unknown")[:120],
        "comment": str(raw.get("comment") or "")[:1000],
    }
    # Yetkilendirme kaniti (API katmani doldurur): kim, hangi rolle, matrisin hangi kuralina gore
    for key in ("reviewer_id", "policy_rule", "auth_method"):
        if raw.get(key):
            out[key] = str(raw[key])[:200]
    if isinstance(raw.get("reviewer_roles"), list):
        out["reviewer_roles"] = [str(r)[:40] for r in raw["reviewer_roles"]][:10]
    return out


def node_human_approval(state: OSState) -> dict[str, Any]:
    """Grafigi durdurur; `Command(resume={...})` gelene kadar burada bekler.

    Bu fonksiyon resume'da bastan calisir - bu yuzden interrupt'tan once yan etki yok.
    """
    action = state["pending_action"]
    decision = interrupt(
        {
            "run_id": state.get("run_id"),
            "requester": state.get("requester"),
            "route": state.get("route"),
            "action": action,
            "message": "Bu eylem insan onayi gerektiriyor.",
        }
    )
    approval = normalize_approval(decision)
    verdict = "ONAY" if approval["approved"] else "RED"
    return {
        "approval": approval,
        "trace": [
            f"insan karari: {verdict} (inceleyen: {approval['reviewer']}"
            + (f", kural: {approval['policy_rule']}" if approval.get("policy_rule") else "") + ")"
        ],
    }


async def node_execute_action(state: OSState) -> dict[str, Any]:
    """Onaylanmis eylemi yurutur. Grafikte eylemin calistigi TEK yer burasidir."""
    action = state["pending_action"] or {}
    kind = action.get("kind")
    try:
        if kind == "tool_call":
            result = await execute_tool(
                action["tool"], action["arguments"], idempotency_key=state["run_id"]
            )
        elif kind == "contract_signoff":
            result = record_contract_decision(
                contract_name=action["details"].get("contract_name", ""),
                decision="approved_for_signature",
                reviewer=(state.get("approval") or {}).get("reviewer", ""),
                risk_level=action["details"].get("risk_level", ""),
                idempotency_key=state["run_id"],
            )
        elif kind == "expense_approval":
            args = action["arguments"]
            result = await asyncio.to_thread(
                record_expense,
                get_settings().finance_db_path,
                run_id=state["run_id"],
                department=args["department"],
                amount=args["amount"],
                category=args["category"],
                description=(action.get("details") or {}).get("description", ""),
                status="approved",
                approver=(state.get("approval") or {}).get("reviewer", ""),
            )
        elif kind == "vendor_approval":
            args = action["arguments"]
            result = record_vendor_approval(
                args["vendor"], args["score"],
                (state.get("approval") or {}).get("reviewer", ""), state["run_id"],
            )
        else:
            raise ValueError(f"bilinmeyen eylem turu: {kind!r}")
    except Exception as exc:  # noqa: BLE001 - yurutme hatasi sessizce yutulmamali
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "answer": "Eylem onaylandi ancak yurutulurken hata olustu.",
            "trace": [f"eylem yurutulemedi: {exc}"],
        }

    return {
        "status": "completed",
        "action_result": result,
        "answer": f"{action.get('title', 'Eylem')} insan onayiyla gerceklestirildi. {result.get('message', '')}".strip(),
        "trace": [f"eylem yurutuldu: {action.get('title', kind)}"],
    }


def node_cancel_action(state: OSState) -> dict[str, Any]:
    action = state["pending_action"] or {}
    comment = (state.get("approval") or {}).get("comment") or "gerekce belirtilmedi"
    return {
        "status": "rejected",
        "action_result": None,
        "answer": f"{action.get('title', 'Eylem')} insan tarafindan reddedildi ({comment}). Hicbir islem yapilmadi.",
        "trace": ["eylem iptal edildi"],
    }


# --------------------------------------------------------------------------
# Kenarlar
# --------------------------------------------------------------------------
def after_guard(state: OSState) -> str:
    return END if state.get("route") == "blocked" else "router"


def after_router(state: OSState) -> str:
    route = state.get("route")
    # HAVA BOSLUGU - ikinci katman. Router dugumu dis bolgeyi zaten musteri destege
    # sabitler; bu kenar, router'da bir hata olsa bile dis talebin ic ajana gecmesini
    # grafin yapisinda engeller. Bolge, kaynaktan YENIDEN hesaplanir (state'e guvenilmez).
    if zone_of(state.get("source")) == "external" and route not in EXTERNAL_ALLOWED:
        return END
    return route if route in SPECIALISTS else END


def needs_approval(state: OSState) -> str:
    return "human_approval" if state.get("pending_action") else END


def after_approval(state: OSState) -> str:
    return "execute_action" if (state.get("approval") or {}).get("approved") else "cancel_action"


def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    """Grafi derler. interrupt() calismak icin bir checkpointer ZORUNLUDUR."""
    g = StateGraph(OSState)

    g.add_node("guard", guard_node)
    g.add_node("router", router_node)
    g.add_node("it_ops", it_ops_node)
    g.add_node("data_analyst", data_analyst_node)
    g.add_node("contract_analyst", contract_analyst_node)
    g.add_node("finance", finance_node)
    g.add_node("procurement", procurement_node)
    g.add_node("customer_support", customer_support_node)
    g.add_node("human_approval", node_human_approval)
    g.add_node("execute_action", node_execute_action)
    g.add_node("cancel_action", node_cancel_action)

    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", after_guard, {"router": "router", END: END})
    g.add_conditional_edges(
        "router", after_router, {name: name for name in SPECIALISTS} | {END: END}
    )

    for name in READ_ONLY_AGENTS:
        g.add_edge(name, END)
    for name in ACTION_AGENTS:
        g.add_conditional_edges(name, needs_approval, {"human_approval": "human_approval", END: END})

    g.add_conditional_edges(
        "human_approval",
        after_approval,
        {"execute_action": "execute_action", "cancel_action": "cancel_action"},
    )
    g.add_edge("execute_action", END)
    g.add_edge("cancel_action", END)

    return g.compile(checkpointer=checkpointer)
