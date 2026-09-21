"""Servis katmani: kalicilik, es zamanli karar, hata sonrasi geri alma."""
import asyncio

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.graph import build_graph
from app.agent.service import AgentService, RunConflict, RunNotFound
from app.db import get_run
from app.llm.base import ToolCall, ToolChatResult
from app.schemas import RouteDecision
from app.tools import it_tools


@pytest.fixture
def it_llm(llm, seeded_memory):
    llm.on(RouteDecision, RouteDecision(agent="it_ops", reasoning="t", confidence=0.9))
    llm.tool_handler = lambda s, u, t: ToolChatResult(tool_calls=[ToolCall("reset_password", {"email": "ali@acme.com"})])
    return llm


def test_pending_approval_survives_server_restart(it_llm, tmp_path):
    """Sunucu yeniden baslasa da bekleyen onay kaybolmaz (kalici checkpoint)."""
    db = str(tmp_path / "cp.db")
    async def go():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            view = await AgentService(build_graph(saver)).submit("sifremi sifirla", "ali@acme.com", source="internal_panel")
            assert view.status == "awaiting_approval"
        # --- "restart": yepyeni checkpointer baglantisi, graf ve servis ---
        async with AsyncSqliteSaver.from_conn_string(db) as saver2:
            done = await AgentService(build_graph(saver2)).decide(view.run_id, True, "mudur@acme.com")
        assert done.status == "completed" and done.action_result["ok"]
        assert view.run_id in it_tools._EXECUTED
    asyncio.run(go())


def test_submit_persists_pending_state_to_sql(it_llm, tmp_path):
    async def go():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.db")) as saver:
            svc = AgentService(build_graph(saver))
            v = await svc.submit("sifremi sifirla", "ali@acme.com", source="internal_panel")
            row = get_run(v.run_id)
            assert row.status == "awaiting_approval" and row.route == "it_ops"
            assert row.pending_action_json and "reset_password" in row.pending_action_json
            assert row.llm_provider == "fake" and row.trace_json != "[]"
            assert v.approval is None and v.action_result is None
    asyncio.run(go())


def test_decision_lifecycle_and_conflicts(it_llm, tmp_path):
    async def go():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.db")) as saver:
            svc = AgentService(build_graph(saver))
            v = await svc.submit("sifremi sifirla", "ali@acme.com", source="internal_panel")
            done = await svc.decide(v.run_id, True, "mudur@acme.com", "ok")
            assert done.status == "completed" and done.approval["reviewer"] == "mudur@acme.com"
            with pytest.raises(RunConflict):  # ikinci karar
                await svc.decide(v.run_id, False, "baska@acme.com")
            with pytest.raises(RunNotFound):
                await svc.decide("yok", True, "x")
    asyncio.run(go())


def test_concurrent_decisions_only_one_wins(it_llm, tmp_path):
    """Iki inceleyici ayni anda karar verirse yalnizca biri islenir."""
    async def go():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.db")) as saver:
            svc = AgentService(build_graph(saver))
            v = await svc.submit("sifremi sifirla", "ali@acme.com", source="internal_panel")
            results = await asyncio.gather(
                svc.decide(v.run_id, True, "a@acme.com"),
                svc.decide(v.run_id, False, "b@acme.com"),
                return_exceptions=True,
            )
            wins = [r for r in results if not isinstance(r, Exception)]
            losses = [r for r in results if isinstance(r, RunConflict)]
            assert len(wins) == 1 and len(losses) == 1
    asyncio.run(go())


def test_non_pending_run_cannot_be_decided(llm, seeded_memory, tmp_path):
    llm.on(RouteDecision, RouteDecision(agent="it_ops", reasoning="t", confidence=0.9))
    llm.tool_handler = lambda s, u, t: ToolChatResult(text="Bilgi cevabi")
    async def go():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.db")) as saver:
            svc = AgentService(build_graph(saver))
            v = await svc.submit("VPN nasil kurulur", "ali@acme.com", source="internal_panel")
            assert v.status == "completed"
            with pytest.raises(RunConflict):
                await svc.decide(v.run_id, True, "x@acme.com")
    asyncio.run(go())


def test_infrastructure_failure_during_resume_keeps_run_pending(it_llm, tmp_path):
    """Devam ettirme altyapi hatasiyla dusse onay kaybolmaz; yeniden denenebilir."""
    class Broken:
        def __init__(self, real): self.real = real
        async def ainvoke(self, *a, **k): raise RuntimeError("veritabani kilitli")
        async def aget_state(self, *a, **k): return await self.real.aget_state(*a, **k)

    async def go():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.db")) as saver:
            real = build_graph(saver)
            svc = AgentService(real)
            v = await svc.submit("sifremi sifirla", "ali@acme.com", source="internal_panel")
            svc.graph = Broken(real)
            with pytest.raises(RuntimeError):
                await svc.decide(v.run_id, True, "mudur@acme.com")
            assert get_run(v.run_id).status == "awaiting_approval"  # geri alindi
            assert v.run_id not in it_tools._EXECUTED
            svc.graph = real  # altyapi duzeldi
            done = await svc.decide(v.run_id, True, "mudur@acme.com")
            assert done.status == "completed"
    asyncio.run(go())
