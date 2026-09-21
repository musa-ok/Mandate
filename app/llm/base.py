"""Saglayicidan bagimsiz LLM arayuzu.

Ajanlar yalnizca bu arayuzu bilir; Gemini mi Ollama mi kullanildigindan
habersizdir. Sağlayici degisimi (USE_LOCAL_LLM) tek noktada, `get_llm()` icinde olur.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Model cagrisi basarisiz oldu. Ajanlar bunu yakalayip GUVENLI tarafta biter."""


@dataclass(frozen=True)
class ToolSpec:
    """Modele tanitilan arac. `parameters` bir JSON Schema (object)."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolChatResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_model_json(raw: str, schema: type[T]) -> T:
    """Model ciktisini dogrular. Kod citlerini ve onsoz/sonsoz gurultusunu tolere eder."""
    text = _FENCE_RE.sub("", (raw or "").strip()).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError(f"sema dogrulamasi basarisiz: {exc.error_count()} hata - {exc.errors()[0]['msg']}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"gecerli JSON degil: {exc}") from exc


class LLMClient(ABC):
    provider: str = ""
    model: str = ""
    local: bool = False

    @abstractmethod
    async def _raw_json(self, system: str, user: str, schema: type[BaseModel]) -> str:
        """Sema-kisitli ham JSON metni dondurur."""

    @abstractmethod
    async def chat_with_tools(
        self, *, system: str, user: str, tools: list[ToolSpec]
    ) -> ToolChatResult:
        """Modele araclari sunar; arac cagirmak isterse `tool_calls` dolu doner.

        Araclar BURADA CALISTIRILMAZ - cagirmayi ajan/graf yonetir (insan onayi icin).
        """

    async def generate_structured(self, *, system: str, user: str, schema: type[T]) -> T:
        """Tipli (Pydantic) cikti uretir. Gecersizse hatayi modele gosterip bir kez yeniden dener."""
        last_error = ""
        for attempt in range(2):
            prompt = user
            if attempt:
                prompt += (
                    f"\n\n[Onceki yanit gecersizdi: {last_error}. "
                    "Yalnizca semaya uygun, gecerli JSON dondur.]"
                )
            raw = await self._raw_json(system, prompt, schema)
            try:
                return parse_model_json(raw, schema)
            except ValueError as exc:
                last_error = str(exc)
        raise LLMError(f"{self.provider}/{self.model} gecerli yapisal cikti uretemedi: {last_error}")

    @property
    def configured(self) -> bool:
        """Cagri yapmadan once saglayicinin kullanilabilir olup olmadigi (orn. API anahtari)."""
        return True

    def info(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "local": self.local,
            "configured": self.configured,
        }
