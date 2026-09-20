r"""LangGraph ile otonom karar-islem dongusu.

    retrieve -> decide -> [RED] ------------------> log
                      \-> [ONAY] -> policy -> [ihlal] -> log
                                          \-> [temiz] -> execute -> log

Her dugum saf bir fonksiyondur; durum (state) dugumler arasinda tasinir.
Grafi ayri tutmanin sebebi: yeni bir adim (orn. insan onayi, muhasebe entegrasyonu)
eklemek tek bir kenar degisikligi olsun.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.agent.decision import decide as run_decision
from app.agent.policy import enforce as run_policy
from app.chain.wallet import execute_payment
from app.db import OperationLog, session_scope
from app.rag.store import retrieve as run_retrieval
from app.schemas import (
    AgentDecision,
    Decision,
    OperationResponse,
    PolicyCheck,
    RetrievedChunk,
    TransferResult,
)


class AgentState(TypedDict, total=False):
    request_text: str
    requester: str
    context: list[RetrievedChunk]
    decision: AgentDecision
    policy: PolicyCheck
    transfer: TransferResult
    operation_id: int
    created_at: datetime


# --------------------------------------------------------------------------
# Dugumler
# --------------------------------------------------------------------------
async def node_retrieve(state: AgentState) -> dict[str, Any]:
    return {"context": run_retrieval(state["request_text"])}


async def node_decide(state: AgentState) -> dict[str, Any]:
    return {"decision": run_decision(state["request_text"], state.get("context", []))}


async def node_policy(state: AgentState) -> dict[str, Any]:
    return {"policy": run_policy(state["decision"])}


async def node_execute(state: AgentState) -> dict[str, Any]:
    return {"transfer": await execute_payment(state["decision"].payment)}


async def node_log(state: AgentState) -> dict[str, Any]:
    decision: AgentDecision = state["decision"]
    # RED kararlarinda policy dugumu hic calismaz; ihlal degil "uygulanmadi" demektir.
    policy: PolicyCheck = state.get("policy", PolicyCheck(passed=False, violations=[]))
    transfer: TransferResult = state.get("transfer", TransferResult())
    context = state.get("context", [])

    created_at = datetime.now(timezone.utc)
    row = OperationLog(
        created_at=created_at,
        requester=state.get("requester", "anonymous"),
        request_text=state["request_text"],
        decision=decision.decision.value,
        reasoning=decision.reasoning,
        cited_rules_json=json.dumps(decision.cited_rules, ensure_ascii=False),
        confidence=decision.confidence,
        injection_detected=decision.injection_detected,
        policy_passed=policy.passed,
        policy_violations_json=json.dumps(policy.violations, ensure_ascii=False),
        recipient_wallet=decision.payment.recipient_wallet,
        amount=decision.payment.amount,
        currency=decision.payment.currency.value,
        tx_hash=transfer.tx_hash,
        tx_confirmed=transfer.confirmed,
        tx_simulated=transfer.simulated,
        tx_error=transfer.error,
        context_json=json.dumps([c.model_dump() for c in context], ensure_ascii=False),
    )
    with session_scope() as s:
        s.add(row)
        s.flush()
        operation_id = row.id
    return {"operation_id": operation_id, "created_at": created_at, "policy": policy, "transfer": transfer}


# --------------------------------------------------------------------------
# Kenarlar
# --------------------------------------------------------------------------
def route_after_decision(state: AgentState) -> str:
    return "policy" if state["decision"].decision == Decision.APPROVE else "log"


def route_after_policy(state: AgentState) -> str:
    return "execute" if state["policy"].passed else "log"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("retrieve", node_retrieve)
    graph.add_node("decide", node_decide)
    graph.add_node("policy", node_policy)
    graph.add_node("execute", node_execute)
    graph.add_node("log", node_log)

    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "decide")
    graph.add_conditional_edges("decide", route_after_decision, {"policy": "policy", "log": "log"})
    graph.add_conditional_edges("policy", route_after_policy, {"execute": "execute", "log": "log"})
    graph.add_edge("execute", "log")
    graph.add_edge("log", END)
    return graph.compile()


_compiled = None


def get_agent():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


async def process_request(text: str, requester: str = "anonymous") -> OperationResponse:
    """Uctan uca tek giris noktasi: talep metni -> loglanmis karar + TxHash."""
    final = await get_agent().ainvoke({"request_text": text, "requester": requester})
    decision: AgentDecision = final["decision"]
    approved = decision.decision == Decision.APPROVE and final["policy"].passed

    return OperationResponse(
        operation_id=final["operation_id"],
        request_text=text,
        requester=requester,
        decision=decision.decision,
        reasoning=decision.reasoning,
        cited_rules=decision.cited_rules,
        confidence=decision.confidence,
        injection_detected=decision.injection_detected,
        policy=final["policy"],
        payment=decision.payment if approved else None,
        transfer=final["transfer"],
        context_used=final.get("context", []),
        created_at=final["created_at"],
    )
