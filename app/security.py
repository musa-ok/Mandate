"""Kanal bazli guven bolgeleri ve kimlik dogrulama.

HAVA BOSLUGU MODELI
-------------------
Her talep bir KAYNAKTAN (source) gelir; her kaynak bir GUVEN BOLGESINE (zone) aittir:

    internal  -> tum uzman ajanlar (onay kapisi arkasinda)
    external  -> YALNIZCA musteri destek ajani (public FAQ, salt okunur)

Kaynak, istemcinin BEYANIYLA belirlenmez. "source": "internal_slack" yazmak bir saldirgani
ic bolgeye sokmamali. Bu yuzden:

  * Ic kaynak, yalnizca o kaynaga ait anahtarla (X-Source-Key) kanitlanabilir.
  * Kimligi dogrulanmis bir ic entegrasyon bir DIS kaynak beyan edebilir (orn. destek
    e-posta kutusunu okuyan entegrasyon: external_email). Bu guvenli yondedir: yetki DUSER.
  * Dis kanal (POST /public/support) anahtarsizdir ve yalnizca dis kaynak kabul eder.
  * Taninmayan bir kaynak dis bolge sayilir (fail-closed).
"""
from __future__ import annotations

import secrets
from typing import Literal

from fastapi import Header, HTTPException

from app.config import get_settings

Zone = Literal["internal", "external"]

SOURCES: dict[str, Zone] = {
    "internal_panel": "internal",
    "internal_slack": "internal",
    "internal_teams": "internal",
    "internal_api": "internal",
    "external_web": "external",
    "external_email": "external",
    "external_whatsapp": "external",
}
EXTERNAL_SOURCES = tuple(s for s, z in SOURCES.items() if z == "external")
INTERNAL_SOURCES = tuple(s for s, z in SOURCES.items() if z == "internal")
MIN_KEY_LENGTH = 16


def zone_of(source: str | None) -> Zone:
    """Bilinmeyen / bos kaynak -> external (fail-closed)."""
    return SOURCES.get(source or "", "external")


def _source_keys() -> dict[str, str]:
    """INTERNAL_SOURCE_KEYS'i {anahtar: kaynak} olarak cozer. Zayif/gecersiz girdiler yok sayilir."""
    out: dict[str, str] = {}
    for item in (get_settings().internal_source_keys or "").split(","):
        source, sep, key = item.strip().partition(":")
        source, key = source.strip(), key.strip()
        if sep and zone_of(source) == "internal" and len(key) >= MIN_KEY_LENGTH:
            out[key] = source
    return out


def source_for_key(key: str | None) -> str | None:
    """Anahtarin ait oldugu ic kaynak. Sabit zamanli karsilastirma."""
    if not key:
        return None
    match = None
    for known, source in _source_keys().items():
        if secrets.compare_digest(key.encode(), known.encode()):
            match = source
    return match


def configured_internal_sources() -> list[str]:
    return sorted(set(_source_keys().values()))


def require_internal(x_source_key: str | None = Header(default=None)) -> str:
    """Ic uclari korur; kimligi dogrulanmis kaynagi dondurur.

    Anahtar tanimli degil -> 503 (acik birakilmaz); baslik yok -> 401; yanlis -> 403.
    """
    if not _source_keys():
        raise HTTPException(503, "Ic uclar devre disi: INTERNAL_SOURCE_KEYS tanimli degil.")
    if not x_source_key:
        raise HTTPException(401, "X-Source-Key basligi gerekli.", headers={"WWW-Authenticate": "X-Source-Key"})
    source = source_for_key(x_source_key)
    if source is None:
        raise HTTPException(403, "Gecersiz kaynak anahtari.")
    return source


def resolve_declared_source(authenticated: str, declared: str | None) -> str:
    """Ic cagiranin beyan ettigi kaynagi dogrular.

    * Beyan yok             -> anahtarin kaynagi
    * Kendi kaynagi         -> kabul
    * Herhangi bir dis kaynak -> kabul (yetki duser)
    * Baska bir IC kaynak   -> 403 (anahtar o kaynagi kanitlamiyor)
    """
    if not declared or declared == authenticated:
        return authenticated
    if declared not in SOURCES:
        raise HTTPException(422, f"Bilinmeyen kaynak: {declared!r}")
    if zone_of(declared) == "external":
        return declared
    raise HTTPException(403, f"Bu anahtar '{declared}' kaynagini temsil edemez (anahtarin kaynagi: {authenticated}).")
