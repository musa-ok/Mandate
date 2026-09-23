"""Uretim altyapisi: Vault'tan sir okuma ve PostgreSQL uzerinde kalici onay kuyrugu.

PostgreSQL testleri TEST_POSTGRES_URL tanimliysa calisir (bos bir veritabani verin):
    TEST_POSTGRES_URL=postgresql://kullanici@localhost:5432/mandate_test pytest tests/test_infra.py
"""
import asyncio
import json
import os
import uuid

import httpx
import pytest

from app import vault
from app.config import Settings, build_settings

VAULT = "https://vault.acme.test"
SECRET_PATH = "/v1/secret/data/mandate"
STORED = {
    "GEMINI_API_KEY": "vault-gemini",
    "session_secret": "v" * 40,
    "DATABASE_URL": "postgresql+psycopg://mandate:pw@db/mandate",
    "FINANCE_AUTO_APPROVE_LIMIT": "999999",  # sir degil: Vault'tan gelse de yok sayilmali
}


def fake_vault(token="root-token", role=("rid", "sid"), stored=STORED, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.url.path, dict(request.headers)))
        if request.url.path == "/v1/auth/approle/login":
            data = json.loads(request.content)
            if (data.get("role_id"), data.get("secret_id")) != role:
                return httpx.Response(400, json={"errors": ["invalid role or secret ID"]})
            return httpx.Response(200, json={"auth": {"client_token": token}})
        if request.url.path == SECRET_PATH:
            if request.headers.get("X-Vault-Token") != token:
                return httpx.Response(403, json={"errors": ["permission denied"]})
            return httpx.Response(200, json={"data": {"data": stored, "metadata": {"version": 3}}})
        return httpx.Response(404, json={"errors": []})
    return httpx.MockTransport(handler)


def settings(**kw) -> Settings:
    return Settings(vault_addr=VAULT, **kw)


# ==========================================================================
# Vault
# ==========================================================================
def test_vault_token_loads_only_secret_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(vault, "_transport", fake_vault(calls=calls))
    out = vault.load_secrets(settings(vault_token="root-token", vault_namespace="acme/prod"))
    assert out == {"gemini_api_key": "vault-gemini", "session_secret": "v" * 40,
                   "database_url": "postgresql+psycopg://mandate:pw@db/mandate"}
    assert calls[0][2]["x-vault-namespace"] == "acme/prod"


def test_vault_approle_login(monkeypatch):
    calls = []
    monkeypatch.setattr(vault, "_transport", fake_vault(token="approle-token", calls=calls))
    out = vault.load_secrets(settings(vault_role_id="rid", vault_secret_id="sid"))
    assert out["gemini_api_key"] == "vault-gemini"
    assert [c[1] for c in calls] == ["/v1/auth/approle/login", SECRET_PATH]


@pytest.mark.parametrize("kw,transport,message", [
    ({"vault_token": "yanlis"}, fake_vault(), "reddedildi"),
    ({"vault_role_id": "rid", "vault_secret_id": "yanlis"}, fake_vault(), "AppRole"),
    ({}, fake_vault(), "kimlik yok"),
    ({"vault_token": "root-token", "vault_secret_path": "baska"}, fake_vault(), "bulunamadi"),
    ({"vault_token": "root-token"}, httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))), "ulasilamadi"),
])
def test_vault_failures_stop_startup(monkeypatch, kw, transport, message):
    monkeypatch.setattr(vault, "_transport", transport)
    with pytest.raises(vault.VaultError, match=message):
        vault.load_secrets(settings(**kw))


def test_vault_values_override_environment(monkeypatch):
    monkeypatch.setattr(vault, "_transport", fake_vault())
    monkeypatch.setenv("VAULT_ADDR", VAULT)
    monkeypatch.setenv("VAULT_TOKEN", "root-token")
    monkeypatch.setenv("GEMINI_API_KEY", "ortamdaki-eski-anahtar")
    monkeypatch.setenv("FINANCE_AUTO_APPROVE_LIMIT", "10000")
    s = build_settings()
    assert s.gemini_api_key == "vault-gemini"  # Vault kazanir
    assert s.finance_auto_approve_limit == 10000  # sir olmayan ayar Vault'tan degismez
    assert s.database_url.startswith("postgresql+psycopg://")


def test_no_vault_means_environment_only(monkeypatch):
    monkeypatch.setattr(vault, "_transport", httpx.MockTransport(lambda r: pytest.fail("Vault cagrilmamali")))
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    assert build_settings().vault_addr == ""


# ==========================================================================
# PostgreSQL: denetim kaydi + LangGraph checkpoint
# ==========================================================================
PG = os.environ.get("TEST_POSTGRES_URL", "")
needs_pg = pytest.mark.skipif(not PG, reason="TEST_POSTGRES_URL tanimli degil")


@pytest.fixture
def postgres(monkeypatch):
    """Uygulamanin kayit ve checkpoint deposunu PostgreSQL'e cevirir."""
    from sqlalchemy import create_engine

    from app import db
    from app.config import get_settings

    engine = create_engine(PG.replace("postgresql://", "postgresql+psycopg://", 1), pool_pre_ping=True)
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(get_settings(), "checkpoint_database_url", PG)
    yield engine
    engine.dispose()


@needs_pg
def test_postgres_approval_survives_restart(postgres, llm):
    """Talep onay beklerken uygulama kapanir; yeni surec ayni onayi PostgreSQL'den bulup yurutur."""
    from app.agent.graph import build_graph
    from app.agent.service import AgentService, RunConflict, RunForbidden
    from app.auth import ADMIN_KEY_PRINCIPAL, Principal
    from app.checkpoint import open_checkpointer
    from app.llm.base import ToolCall, ToolChatResult
    from app.schemas import RouteDecision
    from app.tools import it_tools

    llm.on(RouteDecision, lambda s, u: RouteDecision(agent="it_ops", reasoning="t", confidence=0.9))
    llm.tool_handler = lambda s, u, t: ToolChatResult(tool_calls=[ToolCall("reset_password", {"email": "ali@acme.com"})])
    cfo = Principal("sub-cem", "cem@acme.com", "Cem", frozenset({"cfo"}), frozenset(), "internal_panel", "oidc_session")
    it_admin = Principal("sub-ilker", "ilker@acme.com", "Ilker", frozenset({"it_admin"}), frozenset(), "internal_panel", "oidc_session")

    async def first_process():
        async with open_checkpointer() as saver:
            svc = AgentService(build_graph(saver))
            v = await svc.submit("sifremi sifirla", "ali@acme.com", source="internal_panel")
            assert v.status == "awaiting_approval"
            return v.run_id

    async def second_process(rid):
        async with open_checkpointer() as saver:  # yeni baglanti havuzu, bellekte hicbir sey yok
            svc = AgentService(build_graph(saver))
            assert rid in [x.run_id for x in svc.list("awaiting_approval", 500)]
            with pytest.raises(RunForbidden):
                await svc.decide(rid, True, "", approver=cfo)
            done = await svc.decide(rid, True, "", "ok", approver=it_admin)
            assert done.status == "completed" and done.approval["reviewer"] == "ilker@acme.com"
            assert done.approval["policy_rule"] == "it-islem"
            with pytest.raises(RunConflict):
                await svc.decide(rid, False, "x", approver=ADMIN_KEY_PRINCIPAL)
            return done

    rid = asyncio.run(first_process())
    done = asyncio.run(second_process(rid))
    assert rid in it_tools._EXECUTED and done.action_result

    with postgres.connect() as conn:
        from sqlalchemy import text
        assert conn.execute(text("select status from runs where id = :i"), {"i": rid}).scalar() == "completed"
        assert conn.execute(text("select count(*) from checkpoints where thread_id = :i"), {"i": rid}).scalar() > 0


@needs_pg
def test_postgres_concurrent_decisions_only_one_wins(postgres):
    """claim_for_decision PostgreSQL'de de atomik: ayni anda iki karar -> yalnizca biri."""
    from concurrent.futures import ThreadPoolExecutor

    from app import db

    rid = uuid.uuid4().hex[:16]
    db.create_run(rid, "t", "ali@acme.com", "", {}, "internal_panel")
    db.update_run(rid, status="awaiting_approval")
    with ThreadPoolExecutor(8) as pool:
        wins = list(pool.map(lambda _: db.claim_for_decision(rid), range(8)))
    assert wins.count(True) == 1
