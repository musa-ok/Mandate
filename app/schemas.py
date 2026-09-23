"""Veri sozlesmeleri.

Iki tur model vardir:
  * LLM'e verilen semalar (RouteDecision, SQLPlan, ...): bilerek SADE tutulur -
    varsayilan deger / Optional yok. Hem Gemini hem yerel modellerin kisitli
    cozumlemesi bu sekilde daha guvenilir calisir.
  * API modelleri (RunView, ...): disariya donen sonuc.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]
RunStatus = Literal[
    "running",
    "awaiting_approval",
    "completed",
    "handed_off",  # musteri destek: cevap FAQ'de yok / aciliyet yuksek -> canli temsilci
    "rejected",
    "blocked",
    "unsupported",
    "needs_input",
    "failed",
]


# ==========================================================================
# LLM'e verilen semalar
# ==========================================================================
class RouteDecision(BaseModel):
    """Router (Supervizor) ciktisi."""

    agent: Literal[
        "it_ops", "data_analyst", "contract_analyst",
        "finance", "procurement", "customer_support", "unsupported",
    ] = Field(
        description="Talebi ele alacak uzman ajan; hicbiri uymuyorsa 'unsupported'."
    )
    reasoning: str = Field(description="Secimin kisa gerekcesi (Turkce).")
    confidence: float = Field(description="0 ile 1 arasi guven.")


class SQLPlan(BaseModel):
    """Veri Analisti: dogal dilden uretilen sorgu."""

    answerable: bool = Field(description="Talep, sema ile cevaplanabiliyor mu?")
    sql: str = Field(description="Tek bir SQLite SELECT sorgusu. Cevaplanamiyorsa bos string.")
    explanation: str = Field(description="Sorgunun ne yaptigi, Turkce, tek cumle.")


class InfoAnswer(BaseModel):
    answer: str = Field(description="Kullaniciya Turkce, kisa ve net cevap.")


class ResultSummary(BaseModel):
    summary: str = Field(description="Sonucun Turkce, is odakli kisa ozeti.")


class ClauseCheck(BaseModel):
    """Bir maddenin TEK bir kirmizi cizgiye karsi degerlendirmesi.

    Alan SIRASI bilinclidir: model once kuralin ne gerektirdigini, sonra maddenin ne
    dedigini, sonra ikisinin KARSILASTIRMASINI (analysis) yazar; ihlal kararini ANCAK
    bundan sonra verir. Karar gerekceden once gelirse kucuk modeller kendi gerekcesiyle
    celisen karar verebiliyor (canli testte: gerekce "uyumludur", violates=true). Kucuk modeller aksi halde
    konuyu eslestirip ("sorumluluk maddesi var -> KC-1 ihlali") uyumlu maddeleri de
    ihlal sayar (canli testte temiz sozlesmede 7 yanlis pozitif).
    """

    red_line: str = Field(description="Degerlendirilen kuralin numarasi ve adi (orn. 'KC-5 Uzun odeme vadesi').")
    rule_requires: str = Field(description="BU kuralin (red_line) neyi gerektirdigi / neye izin verdigi, kisaca.")
    clause_says: str = Field(description="Maddenin bu konuda ne dedigi: maddeden KELIMESI KELIMESINE kisa alinti.")
    analysis: str = Field(
        description="rule_requires ile clause_says'i KARSILASTIR: madde kuralin gerektirdigini sagliyor mu? "
        "Turkce, tek cumle. violates kararini BU karsilastirmaya gore ver."
    )
    violates: bool = Field(description="analysis'e gore madde kurali IHLAL ediyor mu? Uyumluysa false.")
    severity: Severity = Field(description="Kuralin basligindaki seviye (KRITIK->critical, YUKSEK->high, ORTA->medium).")
    recommendation: str = Field(description="Ihlal varsa onerilen duzeltme; yoksa bos string.")


class ClauseAnalysis(BaseModel):
    checks: list[ClauseCheck] = Field(
        description="Maddenin KONUSUYLA ilgili her kural icin bir degerlendirme. Ilgili kural yoksa bos liste."
    )


class ExpenseRequest(BaseModel):
    """Finans ajani: talepten cikarilan harcama bilgisi. Tutar kodla dogrulanir."""

    intent: Literal["expense_approval", "budget_query"] = Field(
        description="expense_approval: bir harcamanin onaylanmasi isteniyor. budget_query: butce durumu soruluyor."
    )
    department: str = Field(description="Departman adi; listedeki degerlerden BIREBIR biri. Belirtilmemisse bos string.")
    amount: float = Field(description="Talep edilen tutar (sayi). budget_query ise 0.")
    currency: Literal["TRY", "USD", "EUR"] = Field(description="Para birimi; belirtilmemisse TRY.")
    category: str = Field(description="Harcama kategorisi, kisa (orn. 'Dijital reklam').")
    description: str = Field(description="Harcamanin kisa aciklamasi, Turkce.")


class RequirementCheck(BaseModel):
    """Satin alma: tedarikci teklifinin TEK bir sartname maddesine karsi degerlendirmesi."""

    requirement_id: str = Field(description="Sartname madde kodu, orn. 'SA-03'.")
    proposal_says: str = Field(
        description="Teklifin bu gereksinim hakkinda ne dedigi: tekliften KELIMESI KELIMESINE kisa alinti. Teklif bu konuda sessizse bos string."
    )
    analysis: str = Field(description="Gereksinim ile teklifin karsilastirmasi, Turkce, tek cumle.")
    status: Literal["met", "not_met", "unclear"] = Field(
        description="met: teklif gereksinimi ACIKCA karsiliyor. not_met: acikca karsilamiyor. unclear: teklif sessiz/belirsiz."
    )


class VendorEvaluation(BaseModel):
    vendor_name: str = Field(description="Teklifi veren tedarikcinin adi; bulunamazsa bos string.")
    checks: list[RequirementCheck] = Field(description="Sartnamedeki HER madde icin bir degerlendirme.")


SupportCategory = Literal[
    "fatura_odeme", "teknik_ariza", "teslimat_lojistik", "urun_kalite",
    "hesap_erisim", "iade_iptal", "veri_gizliligi", "diger",
]


class SupportAnswer(BaseModel):
    """Musteri destek: triaj + YALNIZCA public FAQ'ye dayanan cevap.

    Departman, SLA ve cevabin musteriye gidip gitmeyecegine KOD karar verir.
    """

    category: SupportCategory
    sentiment: Literal["cok_olumsuz", "olumsuz", "notr", "olumlu"]
    urgency: Literal["low", "medium", "high", "critical"] = Field(
        description="critical: guvenlik/veri ihlali, yasal tehdit veya hizmetin tamamen durmasi. high: ciddi musteri kaybi riski."
    )
    summary: str = Field(description="Mesajin 1 cumlelik ozeti (IC kullanim icin), Turkce.")
    answerable: bool = Field(
        description="Sorunun cevabi <sss> parcalarinda ACIKCA var mi? Tahmin gerekiyorsa false."
    )
    used_parts: list[int] = Field(description="Cevapta kullanilan <sss> parca numaralari (orn. [1, 3]). answerable=false ise bos.")
    answer: str = Field(
        description="answerable=true ise YALNIZCA kullanilan parcalardaki bilgilerle yazilmis kisa, kibar cevap. Aksi halde bos string."
    )


# ==========================================================================
# API modelleri
# ==========================================================================
class RetrievedChunk(BaseModel):
    text: str
    source: str
    domain: str = ""
    score: float


InternalSource = Literal["internal_panel", "internal_slack", "internal_teams", "internal_api"]
ExternalSource = Literal["external_web", "external_email", "external_whatsapp"]
Source = Literal[
    "internal_panel", "internal_slack", "internal_teams", "internal_api",
    "external_web", "external_email", "external_whatsapp",
]


class RequestPayload(BaseModel):
    """Ic uclar (X-Source-Key ile). Kaynak anahtardan gelir; `source` yalnizca dogrulanir."""

    text: str = Field(min_length=3, max_length=4000, description="Serbest metin talep")
    requester: str = Field(default="anonymous", max_length=120)
    source: Source | None = Field(
        default=None,
        description="Beyan edilen kaynak. Bos -> anahtarin kaynagi. Dis bir kaynak beyan edilebilir (yetki duser); "
        "baska bir IC kaynak beyan edilemez.",
    )


class PublicSupportRequest(BaseModel):
    """Dis kanal (anahtarsiz). YALNIZCA musteri destek ajanina gider."""

    message: str = Field(min_length=3, max_length=2000)
    contact: str = Field(default="", max_length=120, description="Musterinin e-postasi / telefonu (istege bagli)")
    source: ExternalSource = "external_web"


class PublicSupportResponse(BaseModel):
    """Musteriye donen yanit. BILINCLI OLARAK DARDIR: ic yonlendirme, departman, aciliyet,
    karar izi, model adi ve benzeri hicbir ic bilgi disari cikmaz."""

    reference: str = Field(description="Musterinin destek ekibine iletebilecegi talep numarasi")
    reply: str
    handed_off: bool = Field(description="Talep canli destek temsilcisine aktarildi mi?")


class ApprovalDecisionPayload(BaseModel):
    approved: bool
    reviewer: str = Field(
        default="", max_length=120,
        description="Karari veren kisi. AUTH_MODE=keys iken zorunlu; SSO'da yok sayilir (kimlikten gelir).",
    )
    comment: str = Field(default="", max_length=1000)


class RunView(BaseModel):
    """Bir talebin uctan uca durumu: API'nin tek ve tutarli cevap bicimi."""

    run_id: str
    status: RunStatus
    route: str = ""
    route_reasoning: str = ""
    request_text: str
    requester: str
    source: str = ""
    zone: str = ""
    attachment_name: str = ""
    answer: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    pending_action: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    # Onay bekleyen eylemde: onay matrisinin eslesen kurali ve onaylayabilecek roller
    approval_policy: dict[str, Any] | None = None
    action_result: dict[str, Any] | None = None
    error: str = ""
    trace: list[str] = Field(default_factory=list)
    llm_provider: str = ""
    llm_model: str = ""
    created_at: datetime
    updated_at: datetime
