"""Karar motoru: RAG baglami + talep -> structured AgentDecision.

Model cagrisi Google Gemini (google-genai SDK) uzerinden yapilir. Cikti semasi
Pydantic ile zorlanir (response_schema), boylece cuzdan adresi/tutar serbest
metinden regex ile ayiklanmaz - modelden dogrudan tipli gelir.

Tasarim notu: bu katmanin her hata yolu RED ile biter. Para hareketi soz konusu
oldugu icin ajan hicbir kosulda "acik duserek" onay veremez.
"""
from __future__ import annotations

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.agent.prompts import SYSTEM_PROMPT, build_user_message, prefilter_injection
from app.config import get_settings
from app.rag.store import build_context_block
from app.schemas import AgentDecision, Currency, Decision, PaymentInstruction, RetrievedChunk

settings = get_settings()
_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        # api_key=None ise SDK ortamdan cozer (GEMINI_API_KEY / GOOGLE_API_KEY)
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _rejected(reason: str, injection: bool = False) -> AgentDecision:
    return AgentDecision(
        decision=Decision.REJECT,
        reasoning=reason,
        cited_rules=[],
        confidence=1.0,
        injection_detected=injection,
        payment=PaymentInstruction(
            recipient_wallet="", amount=0.0, currency=Currency.USDC,
            beneficiary_name="", purpose="",
        ),
    )


def _build_config() -> types.GenerateContentConfig:
    """Karar uretimi icin deterministik, JSON semasi zorunlu konfigurasyon."""
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=AgentDecision,
        # Ayni talep + ayni kural seti her zaman ayni karari vermeli.
        temperature=0.0,
        max_output_tokens=settings.max_output_tokens,
        # -1 = dinamik dusunme (model gerektigi kadar dusunur), 0 = kapali.
        thinking_config=types.ThinkingConfig(thinking_budget=settings.thinking_budget),
    )


def decide(request_text: str, context: list[RetrievedChunk]) -> AgentDecision:
    """Tek bir talep icin ONAY/RED karari uretir."""
    # 1. katman: deterministik on filtre (LLM'e hic gitmeden bariz saldirilari kes)
    hits = prefilter_injection(request_text)
    if hits:
        return _rejected(
            "Talep metni, kurumsal kurallari gecersiz kilmaya calisan talimatlar "
            f"iceriyor (tespit edilen kalip: {', '.join(hits)}). Guvenlik geregi reddedildi.",
            injection=True,
        )

    user_message = build_user_message(request_text, build_context_block(context))

    try:
        response = get_client().models.generate_content(
            model=settings.model_id,
            contents=user_message,
            config=_build_config(),
        )
    except genai_errors.ClientError as exc:
        return _rejected(f"Model istegi reddedildi ({exc.code}: {exc.message}); karar RED.")
    except genai_errors.ServerError as exc:
        return _rejected(f"Model servisi hata dondu ({exc.code}); guvenli taraf: RED.")
    except genai_errors.APIError as exc:
        return _rejected(f"Model API hatasi ({exc}); guvenli taraf: RED.")
    except Exception as exc:  # noqa: BLE001 - ajan asla "acik" duserek onay veremez
        return _rejected(f"Beklenmeyen hata ({type(exc).__name__}: {exc}); guvenli taraf: RED.")

    # Gemini guvenlik filtresi istemi tamamen engellemis olabilir -> para hareketi yok.
    feedback = response.prompt_feedback
    if feedback is not None and feedback.block_reason is not None:
        return _rejected(
            f"Talep model guvenlik filtresi tarafindan engellendi ({feedback.block_reason}); karar RED."
        )

    # Uretim yarida kesildiyse (token limiti, safety, recitation) cikti guvenilmez.
    candidate = (response.candidates or [None])[0]
    if candidate is None:
        return _rejected("Model hicbir yanit uretmedi; karar RED.")
    if candidate.finish_reason not in (None, types.FinishReason.STOP):
        return _rejected(
            f"Model uretimi normal tamamlanmadi (finish_reason={candidate.finish_reason}); karar RED."
        )

    decision = response.parsed
    if not isinstance(decision, AgentDecision):
        return _rejected("Model gecerli bir karar semasi uretemedi; karar RED.")

    # Model injection tespit ettiyse karar her halukarda RED'e cekilir.
    if decision.injection_detected:
        decision.decision = Decision.REJECT
        decision.payment = PaymentInstruction(
            recipient_wallet="", amount=0.0, currency=decision.payment.currency
        )
    return decision
