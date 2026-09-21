"""Google Gemini (bulut) saglayicisi - google-genai SDK, tamamen async."""
from __future__ import annotations

import os

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from app.config import Settings
from app.llm.base import LLMClient, LLMError, ToolCall, ToolChatResult, ToolSpec


class GeminiClient(LLMClient):
    provider = "gemini"
    local = False

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self.model = settings.model_id
        self._client: genai.Client | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self._s.gemini_api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        )

    def _get_client(self) -> genai.Client:
        if self._client is None:
            try:
                # api_key=None ise SDK ortamdan cozer (GEMINI_API_KEY / GOOGLE_API_KEY)
                self._client = genai.Client(api_key=self._s.gemini_api_key)
            except ValueError as exc:
                raise LLMError(
                    "GEMINI_API_KEY tanimli degil. .env'e ekleyin veya USE_LOCAL_LLM=true yapin."
                ) from exc
        return self._client

    def _config(self, system: str, **extra) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,  # ayni girdi -> ayni karar: denetlenebilirlik
            max_output_tokens=self._s.max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=self._s.thinking_budget),
            # SDK araclari ASLA kendisi calistirmasin: arac cagrisi yalnizca ONERI olarak
            # doner, yurutmeyi insan onayindan sonra graf yapar.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            **extra,
        )

    async def _generate(self, user: str, config: types.GenerateContentConfig):
        client = self._get_client()
        try:
            response = await client.aio.models.generate_content(
                model=self.model, contents=user, config=config
            )
        except genai_errors.APIError as exc:
            raise LLMError(f"Gemini API hatasi ({getattr(exc, 'code', '?')}): {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - ag/zaman asimi vb.
            raise LLMError(f"Gemini cagrisi basarisiz: {type(exc).__name__}: {exc}") from exc

        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason is not None:
            raise LLMError(f"Istem Gemini guvenlik filtresince engellendi ({feedback.block_reason}).")
        candidate = (response.candidates or [None])[0]
        if candidate is None:
            raise LLMError("Gemini hicbir yanit uretmedi.")
        if candidate.finish_reason not in (None, types.FinishReason.STOP):
            raise LLMError(f"Gemini uretimi normal bitmedi (finish_reason={candidate.finish_reason}).")
        return candidate

    async def _raw_json(self, system: str, user: str, schema: type[BaseModel]) -> str:
        config = self._config(
            system, response_mime_type="application/json", response_schema=schema
        )
        candidate = await self._generate(user, config)
        parts = candidate.content.parts if candidate.content else []
        return "".join(p.text or "" for p in parts if not p.thought)

    async def chat_with_tools(
        self, *, system: str, user: str, tools: list[ToolSpec]
    ) -> ToolChatResult:
        declarations = [
            types.FunctionDeclaration(
                name=t.name, description=t.description, parameters_json_schema=t.parameters
            )
            for t in tools
        ]
        config = self._config(system, tools=[types.Tool(function_declarations=declarations)])
        candidate = await self._generate(user, config)

        result = ToolChatResult()
        for part in candidate.content.parts if candidate.content else []:
            if part.function_call is not None:
                result.tool_calls.append(
                    ToolCall(part.function_call.name or "", dict(part.function_call.args or {}))
                )
            elif part.text and not part.thought:
                result.text += part.text
        return result
