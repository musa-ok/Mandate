"""Hibrit LLM katmani + Dinamik Bilissel Yonlendirme.

Varsayilan saglayici .env'deki USE_LOCAL_LLM ile secilir. Tek tek ajanlar bunu
ezebilir (orn. CONTRACT_ANALYST_LLM=cloud): IT/Veri ajanlari yerel modelde calisirken
sozlesme analizi bulut modeline gider.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from app.config import get_settings
from app.llm.base import LLMClient, LLMError, ToolCall, ToolChatResult, ToolSpec

__all__ = [
    "get_llm", "llm_info", "resolve_provider", "routing_table", "AGENTS",
    "LLMClient", "LLMError", "ToolCall", "ToolChatResult", "ToolSpec",
]

Provider = Literal["cloud", "local"]
AGENTS = ("router", "it_ops", "data_analyst", "contract_analyst", "finance", "procurement", "customer_support")


def _override(agent: str | None) -> str:
    """Ajan ayari: <ajan>_llm (orn. CONTRACT_ANALYST_LLM, FINANCE_LLM). Yoksa 'default'."""
    return getattr(get_settings(), f"{agent}_llm", "default") if agent else "default"


def resolve_provider(agent: str | None = None) -> Provider:
    """Bir ajanin hangi saglayiciyi kullanacagini belirler (override > genel ayar)."""
    override = _override(agent)
    if override in ("cloud", "local"):
        return override  # type: ignore[return-value]
    return "local" if get_settings().use_local_llm else "cloud"


@lru_cache
def _client(provider: Provider) -> LLMClient:
    settings = get_settings()
    if provider == "local":
        from app.llm.local_ollama import OllamaClient

        return OllamaClient(settings)
    from app.llm.gemini import GeminiClient

    return GeminiClient(settings)


def get_llm(agent: str | None = None) -> LLMClient:
    """agent=None -> genel varsayilan. Ajanlar kendi adlariyla cagirir."""
    return _client(resolve_provider(agent))


def llm_info(agent: str | None = None) -> dict:
    info = get_llm(agent).info()
    info["forced"] = _override(agent) in ("cloud", "local")
    return info


def routing_table() -> dict[str, dict]:
    """Hangi ajan hangi modelde calisiyor - /health ve arayuz icin."""
    return {agent: llm_info(agent) for agent in AGENTS}
