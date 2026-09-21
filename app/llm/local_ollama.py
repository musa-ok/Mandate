"""Yerel Ollama saglayicisi: sirket verisi makineden CIKMADAN calisir.

Iki arac-cagirma yolu vardir:
  1. Yerel (native) tool calling - qwen2.5, llama3.1+ gibi destekleyen modeller.
  2. Yedek yol - phi3 gibi desteklemeyen modellerde Ollama 400 doner; bu durumda
     araclari prompta yazip modelden SEMA-KISITLI JSON ile secim yapmasini isteriz.
Boylece model degisince kod degismez.
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
import ollama
from pydantic import BaseModel

from app.config import Settings
from app.llm.base import LLMClient, LLMError, ToolCall, ToolChatResult, ToolSpec, parse_model_json

_NO_TOOLS_MARKER = "does not support tools"
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def recover_leaked_tool_calls(text: str, tool_names: set[str]) -> tuple[list[ToolCall], str]:
    """Bazi yerel modeller (orn. qwen2.5) arac cagrisini yapisal alan yerine METNE yazar:
        'brtc {"name": "grant_access", "arguments": {...}}'
    Bu durumda cagri kaybolur ve ham JSON kullaniciya gosterilir. Burada o JSON'u ayiklayip
    gercek ToolCall'a ceviririz. Guvenli: kurtarilan cagri da kod dogrulamasindan ve insan
    onayindan gecer. Yalnizca BILINEN arac adlari kabul edilir.
    """
    calls: list[ToolCall] = []
    remaining = text
    for match in _JSON_OBJ_RE.finditer(text or ""):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        name = obj.get("name") if isinstance(obj, dict) else None
        args = obj.get("arguments", obj.get("parameters", {})) if isinstance(obj, dict) else None
        if name in tool_names and isinstance(args, dict):
            calls.append(ToolCall(name, args))
            remaining = remaining.replace(match.group(0), "")
    # sizintinin onundeki anlamsiz token artiklarini (orn. 'brtc') temizle
    remaining = re.sub(r"^\s*[A-Za-z_]{2,8}\s+(?=\S)", "", remaining.strip()) if calls else remaining
    return calls, remaining.strip()


class OllamaClient(LLMClient):
    provider = "ollama"
    local = True

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self.model = settings.ollama_model
        self._client = ollama.AsyncClient(host=settings.ollama_host, timeout=settings.ollama_timeout)
        self._native_tools: bool | None = None  # None = henuz denenmedi

    # ------------------------------------------------------------------
    def _options(self) -> dict[str, Any]:
        return {"temperature": 0.0, "num_ctx": self._s.ollama_num_ctx}

    async def _chat(self, **kwargs):
        try:
            return await self._client.chat(model=self.model, options=self._options(), **kwargs)
        except ollama.ResponseError as exc:
            if exc.status_code == 404:
                raise LLMError(
                    f"Ollama'da '{self.model}' modeli yok. Kurmak icin: ollama pull {self.model}"
                ) from exc
            raise
        except (ConnectionError, httpx.HTTPError) as exc:
            raise LLMError(
                f"Ollama'ya ulasilamadi ({self._s.ollama_host}). Calistirmak icin: ollama serve"
            ) from exc

    @staticmethod
    def _messages(system: str, user: str) -> list[dict[str, str]]:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    # ------------------------------------------------------------------
    async def _raw_json(self, system: str, user: str, schema: type[BaseModel]) -> str:
        try:
            resp = await self._chat(
                messages=self._messages(system, user), format=schema.model_json_schema()
            )
        except ollama.ResponseError as exc:
            raise LLMError(f"Ollama hatasi ({exc.status_code}): {exc.error}") from exc
        return resp.message.content or ""

    # ------------------------------------------------------------------
    async def chat_with_tools(
        self, *, system: str, user: str, tools: list[ToolSpec]
    ) -> ToolChatResult:
        if self._native_tools is not False:
            try:
                return await self._native_tool_chat(system, user, tools)
            except ollama.ResponseError as exc:
                if exc.status_code == 400 and _NO_TOOLS_MARKER in str(exc.error).lower():
                    self._native_tools = False  # bu model icin bir daha denemeyelim
                else:
                    raise LLMError(f"Ollama hatasi ({exc.status_code}): {exc.error}") from exc
        return await self._fallback_tool_chat(system, user, tools)

    async def _native_tool_chat(self, system: str, user: str, tools: list[ToolSpec]) -> ToolChatResult:
        payload = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in tools
        ]
        resp = await self._chat(messages=self._messages(system, user), tools=payload)
        self._native_tools = True
        text = resp.message.content or ""
        calls = [
            ToolCall(c.function.name, dict(c.function.arguments or {}))
            for c in (resp.message.tool_calls or [])
        ]
        if not calls:
            calls, text = recover_leaked_tool_calls(text, {t.name for t in tools})
        return ToolChatResult(text=text, tool_calls=calls)

    async def _fallback_tool_chat(self, system: str, user: str, tools: list[ToolSpec]) -> ToolChatResult:
        """Araci prompt + JSON semasi ile secturur (phi3 gibi modeller icin)."""
        names = [t.name for t in tools]
        # Tum araclarin parametrelerini tek duz semada birlestir (hepsi string).
        props: dict[str, Any] = {
            "tool": {"type": "string", "enum": [*names, "none"]},
            "answer": {"type": "string"},
        }
        for t in tools:
            for pname in t.parameters.get("properties", {}):
                props.setdefault(pname, {"type": "string"})
        schema_dict = {"type": "object", "properties": props, "required": ["tool", "answer"]}

        catalog = "\n".join(
            f"- {t.name}({', '.join(t.parameters.get('properties', {}))}): {t.description}" for t in tools
        )
        augmented = (
            f"{system}\n\nKullanabilecegin araclar:\n{catalog}\n\n"
            "YALNIZCA JSON dondur. Bir arac gerekiyorsa 'tool' alanina adini ve arac "
            "parametrelerini yaz. Arac GEREKMIYORSA tool='none' yaz ve yaniti 'answer' alanina yaz."
        )
        try:
            resp = await self._chat(messages=self._messages(augmented, user), format=schema_dict)
        except ollama.ResponseError as exc:
            raise LLMError(f"Ollama hatasi ({exc.status_code}): {exc.error}") from exc

        class _Loose(BaseModel):
            model_config = {"extra": "allow"}
            tool: str = "none"
            answer: str = ""

        try:
            parsed = parse_model_json(resp.message.content or "", _Loose)
        except ValueError as exc:
            raise LLMError(f"Ollama yedek arac secimi gecersiz cikti verdi: {exc}") from exc

        if parsed.tool in names:
            allowed = tools[names.index(parsed.tool)].parameters.get("properties", {})
            args = {k: v for k, v in (parsed.model_extra or {}).items() if k in allowed}
            return ToolChatResult(text=parsed.answer, tool_calls=[ToolCall(parsed.tool, args)])
        return ToolChatResult(text=parsed.answer)
