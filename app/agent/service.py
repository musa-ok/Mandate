"""Graf ile API arasindaki servis: calistirir, duraklamayi tespit eder, kaydeder.

Graf saf tutulur (kalici kayit yazmaz). Kalici kayit burada, grafin her
DURAKLAMASINDA ve BITISINDE yapilir; cunku interrupt() grafi bir "log" dugumune
ulasmadan durdurur.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from langgraph.types import Command

from app import rbac
from app.agent.graph import initial_state
from app.db import Run, claim_for_decision, create_run, get_run, list_runs, load_json, update_run
from app.llm import get_llm
from app.schemas import RunView

if TYPE_CHECKING:
    from app.auth import Principal


class RunNotFound(LookupError):
    pass


class RunConflict(RuntimeError):
    """Run bu islem icin uygun durumda degil (orn. zaten karara baglanmis)."""


class RunForbidden(PermissionError):
    """Cagiranin bu karari verme yetkisi yok (onay matrisi / dort goz)."""


def authorize_decision(
    pending_action: dict[str, Any] | None, data: dict[str, Any] | None, requester: str, approver: Principal
) -> rbac.Rule:
    """Onay matrisini uygular. Onaylamak da reddetmek de ayni yetkiyi ister."""
    rule = rbac.get_policy().rule_for(pending_action, data)
    if rule is None:
        raise RunForbidden("Onay matrisinde bu eylem icin kural yok; kimse onaylayamaz.")
    if not approver.roles & rule.roles:
        labels = ", ".join(rbac.ROLES[r] for r in sorted(rule.roles))
        raise RunForbidden(f"Bu eylemi yalnizca su roller onaylayabilir: {labels} (kural: {rule.id}).")
    # Dort goz: dogrulanmis bir kisi kendi talebine karar veremez
    if approver.is_person and requester.strip().lower() in {approver.email.lower(), approver.subject.lower()}:
        raise RunForbidden("Kendi talebinizi onaylayamazsiniz (dort goz ilkesi).")
    return rule


def _policy_view(row: Run) -> dict[str, Any] | None:
    if row.status != "awaiting_approval":
        return None
    rule = rbac.get_policy().rule_for(load_json(row.pending_action_json), load_json(row.data_json, {}))
    if rule is None:
        return {"rule": None, "roles": [], "description": "Onay matrisinde eslesen kural yok: kimse onaylayamaz."}
    return rule.to_view()


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
        approval_policy=_policy_view(row),
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

    async def decide(
        self, run_id: str, approved: bool, reviewer: str, comment: str = "", approver: Principal | None = None
    ) -> RunView:
        """`approver` API katmanindan gelir ve onay matrisine tabidir. None yalnizca API disindaki
        guvenilir cagiranlar (testler, betikler) icindir."""
        row = get_run(run_id)
        if row is None:
            raise RunNotFound(run_id)
        if row.status != "awaiting_approval":
            raise RunConflict(f"Bu talep onay beklemiyor (durum: {row.status}).")

        decision: dict[str, Any] = {"approved": approved, "reviewer": reviewer, "comment": comment}
        if approver is not None:
            rule = authorize_decision(
                load_json(row.pending_action_json), load_json(row.data_json, {}), row.requester, approver
            )
            decision |= {
                # SSO'da inceleyen adi beyana degil KIMLIGE dayanir
                "reviewer": approver.display if approver.is_person else reviewer,
                "reviewer_id": approver.subject,
                "reviewer_roles": sorted(approver.roles & rule.roles),
                "policy_rule": rule.id,
                "auth_method": approver.method,
            }
        if not claim_for_decision(run_id):  # es zamanli ikinci karari engelle
            raise RunConflict("Bu talep icin baska bir karar isleniyor veya verildi.")

        try:
            await self.graph.ainvoke(Command(resume=decision), self._config(run_id))
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

    def list(self, status: str | None = None, limit: int = 50, requester: str | None = None) -> list[RunView]:
        return [row_to_view(r) for r in list_runs(status, limit, requester)]

    def decidable(self, approver: Principal, limit: int = 200) -> list[RunView]:
        """Bu kisinin karar verebilecegi bekleyen talepler."""
        out = []
        for view in self.list("awaiting_approval", limit):
            try:
                authorize_decision(view.pending_action, view.data, view.requester, approver)
            except RunForbidden:
                continue
            out.append(view)
        return out


# --- surec duzeyi tekil ornek (FastAPI lifespan'inde kurulur) ---------------
_service: AgentService | None = None


def set_service(service: AgentService | None) -> None:
    global _service
    _service = service


def get_service() -> AgentService:
    if _service is None:
        raise RuntimeError("AgentService baslatilmadi (lifespan calismadi).")
    return _service
