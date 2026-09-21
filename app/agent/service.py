"""Graf ile API arasindaki servis: calistirir, duraklamayi tespit eder, kaydeder.

Graf saf tutulur (kalici kayit yazmaz). Kalici kayit burada, grafin her
DURAKLAMASINDA ve BITISINDE yapilir; cunku interrupt() grafi bir "log" dugumune
ulasmadan durdurur.
"""
from __future__ import annotations

import uuid
from typing import Any

from langgraph.types import Command

from app.agent.graph import initial_state
from app.db import Run, claim_for_decision, create_run, get_run, list_runs, load_json, update_run
from app.llm import get_llm
from app.schemas import RunView


class RunNotFound(LookupError):
    pass


class RunConflict(RuntimeError):
    """Run bu islem icin uygun durumda degil (orn. zaten karara baglanmis)."""


def row_to_view(row: Run) -> RunView:
    return RunView(
        run_id=row.id,
        status=row.status,  # type: ignore[arg-type]
        route=row.route,
        route_reasoning=row.route_reasoning,
        request_text=row.request_text,
        requester=row.requester,
        source=row.source,
        zone=row.zone,
        attachment_name=row.attachment_name,
        answer=row.answer,
        data=load_json(row.data_json, {}),
        pending_action=load_json(row.pending_action_json),
        approval=load_json(row.approval_json),
        action_result=load_json(row.action_result_json),
        error=row.error,
        trace=load_json(row.trace_json, []),
        llm_provider=row.llm_provider,
        llm_model=row.llm_model,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class AgentService:
    def __init__(self, graph) -> None:
        self.graph = graph

    # ------------------------------------------------------------------
    @staticmethod
    def _config(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    async def _persist(self, run_id: str) -> None:
        """Grafin guncel durumunu (duraklamis veya bitmis) SQL'e yazar."""
        snapshot = await self.graph.aget_state(self._config(run_id))
        values = snapshot.values or {}
        awaiting = bool(snapshot.next)

        if awaiting:
            status = "awaiting_approval"
        else:
            status = values.get("status") or "failed"
        error = values.get("error", "")
        if status == "failed" and not error and not values.get("status"):
            error = "graf durum bilgisi olmadan sonlandi"

        llm_used = values.get("llm") or {}
        extra = (
            {"llm_provider": llm_used.get("provider", ""), "llm_model": llm_used.get("model", "")}
            if llm_used
            else {}
        )
        update_run(
            run_id,
            **extra,
            status=status,
            route=values.get("route", ""),
            route_reasoning=values.get("route_reasoning", ""),
            answer=values.get("answer", ""),
            error=error,
            data=values.get("data") or {},
            context=values.get("context") or [],
            pending_action=values.get("pending_action"),
            approval=values.get("approval"),
            action_result=values.get("action_result"),
            trace=values.get("trace") or [],
        )

    # ------------------------------------------------------------------
    async def submit(
        self,
        text: str,
        requester: str = "anonymous",
        attachment_name: str = "",
        attachment_text: str = "",
        source: str = "external_web",
    ) -> RunView:
        """`source` yalnizca API katmaninda KIMLIK BILGISINDEN turetilmis olarak gelmelidir."""
        run_id = uuid.uuid4().hex[:16]
        create_run(run_id, text, requester, attachment_name, get_llm().info(), source)
        try:
            await self.graph.ainvoke(
                initial_state(run_id, text, requester, attachment_name, attachment_text, source),
                self._config(run_id),
            )
            await self._persist(run_id)
        except Exception as exc:  # noqa: BLE001 - beklenmeyen hata kaydedilmeli
            update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        return self.view(run_id)

    async def decide(self, run_id: str, approved: bool, reviewer: str, comment: str = "") -> RunView:
        row = get_run(run_id)
        if row is None:
            raise RunNotFound(run_id)
        if row.status != "awaiting_approval":
            raise RunConflict(f"Bu talep onay beklemiyor (durum: {row.status}).")
        if not claim_for_decision(run_id):  # es zamanli ikinci karari engelle
            raise RunConflict("Bu talep icin baska bir karar isleniyor veya verildi.")

        try:
            await self.graph.ainvoke(
                Command(resume={"approved": approved, "reviewer": reviewer, "comment": comment}),
                self._config(run_id),
            )
            await self._persist(run_id)
        except Exception as exc:  # noqa: BLE001
            # Devam ettirme basarisiz: checkpoint hala duraklamada, onay yeniden denenebilsin.
            update_run(run_id, status="awaiting_approval", error=f"karar islenemedi: {exc}")
            raise
        return self.view(run_id)

    # ------------------------------------------------------------------
    def view(self, run_id: str) -> RunView:
        row = get_run(run_id)
        if row is None:
            raise RunNotFound(run_id)
        return row_to_view(row)

    def list(self, status: str | None = None, limit: int = 50) -> list[RunView]:
        return [row_to_view(r) for r in list_runs(status, limit)]


# --- surec duzeyi tekil ornek (FastAPI lifespan'inde kurulur) ---------------
_service: AgentService | None = None


def set_service(service: AgentService | None) -> None:
    global _service
    _service = service


def get_service() -> AgentService:
    if _service is None:
        raise RuntimeError("AgentService baslatilmadi (lifespan calismadi).")
    return _service
