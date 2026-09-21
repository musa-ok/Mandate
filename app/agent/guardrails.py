"""Graf girisindeki deterministik on filtre (LLM'e gitmeden).

Bu, prompt-injection savunmasinin BIRINCI katmanidir ve bilerek kaba tutulmustur:
bariz saldirilari sifir maliyetle keser. Tek basina yetmez; ajan promptlari
kullanici girdisini "guvenilmeyen veri" olarak isaretler ve eylemler ayrica
insan onayindan gecer.
"""
from __future__ import annotations

from typing import Any

INJECTION_PATTERNS = (
    "ignore previous", "ignore all previous", "disregard previous",
    "forget previous", "system prompt", "jailbreak", "developer mode",
    "onceki talimat", "önceki talimat", "talimatlari unut", "talimatları unut",
    "kurallari yoksay", "kuralları yoksay", "kurallari gormezden",
    "kuralları görmezden", "sen artik", "sen artık", "yeni rolun", "yeni rolün",
    "onay gerektirme", "onaysiz calistir", "onaysız çalıştır",
)


def find_injection(text: str) -> list[str]:
    """Metindeki bariz injection kaliplarini dondurur."""
    lowered = (text or "").casefold()
    return [p for p in INJECTION_PATTERNS if p in lowered]


def guard_node(state: dict[str, Any]) -> dict[str, Any]:
    """Talep metninde injection varsa grafi durdurur (route='blocked').

    Ek dosya (sozlesme) ayri ele alinir: orada bulunan injection talebi
    engellemez, Sozlesme Analizcisi'nde KRITIK bulgu olarak insana yukseltilir.
    """
    request_hits = find_injection(state.get("request_text", ""))
    attachment_hits = find_injection(state.get("attachment_text", ""))

    if request_hits:
        if state.get("zone") == "external":
            # Dis kanalda tespitin ayrintisi saldirgana gosterilmez.
            from app.agent.customer_support import HANDOFF_MESSAGE

            return {
                "route": "blocked", "status": "blocked", "injection_hits": request_hits,
                "answer": HANDOFF_MESSAGE,
                "trace": [f"guard: dis kanalda injection -> engellendi ({', '.join(request_hits)})"],
            }
        return {
            "route": "blocked",
            "status": "blocked",
            "injection_hits": request_hits,
            "answer": (
                "Talep metni, sistem kurallarini gecersiz kilmaya calisan ifadeler iceriyor "
                f"({', '.join(request_hits)}). Guvenlik geregi islenmedi."
            ),
            "trace": [f"guard: injection tespit edildi -> engellendi ({', '.join(request_hits)})"],
        }
    return {
        "injection_hits": attachment_hits,
        "trace": [
            "guard: temiz"
            if not attachment_hits
            else f"guard: EK DOSYADA injection kalibi ({', '.join(attachment_hits)}) - insana yukseltilecek"
        ],
    }
