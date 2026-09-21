"""Dinamik Bilissel Yonlendirme: GERCEK get_llm fabrikasiyla (sahte LLM yok)."""
import asyncio

import pytest

import app.llm as llm_mod
from app.agent.contract_analyst import contract_analyst_node
from app.config import DATA_DIR, get_settings
from app.llm.gemini import GeminiClient
from app.llm.local_ollama import OllamaClient


@pytest.fixture
def cfg(monkeypatch):
    s = get_settings()

    def set_(use_local: bool, contract: str, gemini_key: str | None):
        monkeypatch.setattr(s, "use_local_llm", use_local)
        monkeypatch.setattr(s, "contract_analyst_llm", contract)
        monkeypatch.setattr(s, "gemini_api_key", gemini_key)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        llm_mod._client.cache_clear()

    yield set_
    llm_mod._client.cache_clear()


def test_contract_forced_to_cloud_while_others_stay_local(cfg):
    """Jurideki senaryo: USE_LOCAL_LLM=true + CONTRACT_ANALYST_LLM=cloud."""
    cfg(True, "cloud", "fake-key")
    for agent in ("router", "it_ops", "data_analyst"):
        assert isinstance(llm_mod.get_llm(agent), OllamaClient), agent
    assert isinstance(llm_mod.get_llm("contract_analyst"), GeminiClient)
    table = llm_mod.routing_table()
    assert table["contract_analyst"]["forced"] and not table["contract_analyst"]["local"]
    assert table["it_ops"]["local"] and not table["it_ops"]["forced"]


@pytest.mark.parametrize(
    "use_local,contract,expect_contract",
    [
        (True, "cloud", GeminiClient),
        (True, "local", OllamaClient),
        (True, "default", OllamaClient),
        (False, "default", GeminiClient),
        (False, "local", OllamaClient),  # tersi de mumkun: bulut genel, sozlesme yerel
    ],
)
def test_override_matrix(cfg, use_local, contract, expect_contract):
    cfg(use_local, contract, "k")
    assert isinstance(llm_mod.get_llm("contract_analyst"), expect_contract)
    general = OllamaClient if use_local else GeminiClient
    assert isinstance(llm_mod.get_llm("it_ops"), general)
    assert isinstance(llm_mod.get_llm(), general)


def test_clients_are_shared_not_rebuilt(cfg):
    cfg(True, "cloud", "k")
    assert llm_mod.get_llm("it_ops") is llm_mod.get_llm("data_analyst")


def test_forced_cloud_without_key_fails_fast_and_never_falls_back_to_local(cfg, seeded_memory, monkeypatch):
    """Anahtar yoksa sozlesme sessizce yerel modele DUSMEZ; model hic cagrilmadan acik hata."""
    cfg(True, "cloud", None)
    called = []
    monkeypatch.setattr(OllamaClient, "generate_structured", lambda *a, **k: called.append(1))
    monkeypatch.setattr(GeminiClient, "generate_structured", lambda *a, **k: called.append(1))
    out = asyncio.run(
        contract_analyst_node({
            "run_id": "r1", "attachment_name": "s.txt",
            "attachment_text": (DATA_DIR / "ornek_sozlesme_riskli.txt").read_text(),
            "injection_hits": [],
        })
    )
    assert out["status"] == "failed" and "GEMINI_API_KEY" in out["answer"]
    assert out["llm"]["provider"] == "gemini" and out["llm"]["configured"] is False
    assert called == [] and not out.get("pending_action")
