"""Test ortami: GERCEK dosyalara dokunmaz; her sey gecici dizinde calisir.

Ortam degiskenleri app modulleri IMPORT EDILMEDEN once ayarlanmalidir
(ayarlar ve DB motoru import aninda olusur).
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="mandate-test-"))
os.environ.update(
    USE_LOCAL_LLM="false",
    GEMINI_API_KEY="",
    ADMIN_API_KEY="test-admin-key-0123456789",
    INTERNAL_SOURCE_KEYS="internal_panel:test-panel-key-0123456789,internal_slack:test-slack-key-0123456789",
    CONTRACT_ANALYST_LLM="default",
    PROCUREMENT_LLM="default",
    DATABASE_URL=f"sqlite:///{_TMP / 'runs.db'}",
    CHECKPOINT_DB_PATH=str(_TMP / "checkpoints.db"),
    SALES_DB_PATH=str(_TMP / "sales.db"),
    FINANCE_DB_PATH=str(_TMP / "finance.db"),
    QDRANT_PATH=str(_TMP / "qdrant"),
)

import pytest  # noqa: E402

from app.config import DATA_DIR  # noqa: E402
from app.db import init_db  # noqa: E402
from tests.fakes import FakeLLM  # noqa: E402

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key-0123456789"}
PANEL_HEADERS = {"X-Source-Key": "test-panel-key-0123456789"}
SLACK_HEADERS = {"X-Source-Key": "test-slack-key-0123456789"}

_PATCH_TARGETS = (
    "app.llm.get_llm",
    "app.agent.router.get_llm",
    "app.agent.it_ops.get_llm",
    "app.agent.data_analyst.get_llm",
    "app.agent.contract_analyst.get_llm",
    "app.agent.finance.get_llm",
    "app.agent.procurement.get_llm",
    "app.agent.customer_support.get_llm",
    "app.agent.service.get_llm",
)


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()


@pytest.fixture(scope="session")
def seeded_memory():
    """Gercek belgeleri gecici Qdrant'a yukler (embedding modeli onbellekte)."""
    from app.rag.store import ingest_path

    ingest_path(DATA_DIR / "it_hr_politikasi.txt", "it_hr")
    ingest_path(DATA_DIR / "kirmizi_cizgiler.txt", "red_lines")
    ingest_path(DATA_DIR / "satin_alma_sartnamesi.txt", "procurement")
    ingest_path(DATA_DIR / "public_faq.txt", "public_faq")


@pytest.fixture
def llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    for target in _PATCH_TARGETS:
        monkeypatch.setattr(target, lambda *a, **k: fake)
    return fake
