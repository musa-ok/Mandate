"""Senaryo-yonlendirmeli sahte LLM: agin gercek model olmadan test edilmesini saglar."""
from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from app.llm.base import LLMClient, ToolChatResult, ToolSpec


class FakeLLM(LLMClient):
    provider = "fake"
    model = "fake-1"
    local = False

    def __init__(self) -> None:
        self.handlers: dict[type, Callable[[str, str], Any]] = {}
        self.tool_handler: Callable[[str, str, list[ToolSpec]], ToolChatResult] | None = None
        self.calls: list[tuple[str, str]] = []  # (sema/arac adi, kullanici metni)

    def on(self, schema: type[BaseModel], handler) -> "FakeLLM":
        """handler: (system, user) -> model | Exception. Sabit deger de verilebilir."""
        self.handlers[schema] = handler if callable(handler) else (lambda s, u, _v=handler: _v)
        return self

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    async def _raw_json(self, system, user, schema):  # pragma: no cover - kullanilmaz
        raise NotImplementedError

    async def generate_structured(self, *, system, user, schema):
        self.calls.append((schema.__name__, user))
        out = self.handlers[schema](system, user)
        if isinstance(out, Exception):
            raise out
        return out

    async def chat_with_tools(self, *, system, user, tools):
        self.calls.append(("tools", user))
        assert self.tool_handler is not None, "tool_handler tanimlanmadi"
        out = self.tool_handler(system, user, tools)
        if isinstance(out, Exception):
            raise out
        return out
