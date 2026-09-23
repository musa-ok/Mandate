"""Sirlari HashiCorp Vault'tan (KV v2) okur.

Vault tanimliysa (VAULT_ADDR) asagidaki alanlar ortam degiskenlerinden DEGIL Vault'tan gelir.
Anahtar adlari ortam degiskeni bicimindedir (GEMINI_API_KEY) veya kucuk harf (gemini_api_key).

FAIL-CLOSED: Vault tanimli ama ulasilamiyor / kimlik reddediliyor / sir yoksa uygulama
baslamaz. Sessizce ortamdaki (muhtemelen bos veya eski) degerlere donmek, uretimde
yanlis anahtarla calismak demektir.

Kimlik: VAULT_TOKEN (orn. Vault Agent'in yazdigi token) veya AppRole
(VAULT_ROLE_ID + VAULT_SECRET_ID).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

# Vault'tan gelebilecek alanlar. Diger ayarlar (model adi, limitler...) sir degildir;
# Vault'tan gelirse yok sayilir ki davranis ayarlari gizli bir yerden degismesin.
SECRET_FIELDS = frozenset({
    "gemini_api_key",
    "admin_api_key",
    "internal_source_keys",
    "oidc_client_secret",
    "session_secret",
    "database_url",
    "checkpoint_database_url",
    "qdrant_api_key",
})
TIMEOUT = 10.0

# Testler sahte bir Vault baglamak icin degistirir.
_transport: httpx.BaseTransport | None = None


class VaultError(RuntimeError):
    pass


def _client(settings: Settings) -> httpx.Client:
    headers = {"X-Vault-Namespace": settings.vault_namespace} if settings.vault_namespace else {}
    return httpx.Client(
        base_url=settings.vault_addr.rstrip("/"), headers=headers, timeout=TIMEOUT, transport=_transport
    )


def _token(client: httpx.Client, settings: Settings) -> str:
    if settings.vault_token:
        return settings.vault_token
    if settings.vault_role_id and settings.vault_secret_id:
        res = client.post(
            "/v1/auth/approle/login",
            json={"role_id": settings.vault_role_id, "secret_id": settings.vault_secret_id},
        )
        if res.status_code != 200:
            raise VaultError(f"Vault AppRole girisi reddedildi (HTTP {res.status_code}).")
        return res.json()["auth"]["client_token"]
    raise VaultError("VAULT_ADDR tanimli ama kimlik yok: VAULT_TOKEN veya VAULT_ROLE_ID + VAULT_SECRET_ID gerekli.")


def load_secrets(settings: Settings) -> dict[str, Any]:
    """Vault'taki sir alanlarini {alan_adi: deger} olarak dondurur."""
    path = f"/v1/{settings.vault_kv_mount.strip('/')}/data/{settings.vault_secret_path.strip('/')}"
    try:
        with _client(settings) as client:
            res = client.get(path, headers={"X-Vault-Token": _token(client, settings)})
    except httpx.HTTPError as exc:
        raise VaultError(f"Vault'a ulasilamadi ({settings.vault_addr}): {type(exc).__name__}") from exc

    if res.status_code in (401, 403):
        raise VaultError(f"Vault erisimi reddedildi: {path} (HTTP {res.status_code}).")
    if res.status_code == 404:
        raise VaultError(f"Vault'ta sir bulunamadi: {path}")
    if res.status_code != 200:
        raise VaultError(f"Vault hatasi: {path} (HTTP {res.status_code}).")

    raw = (res.json().get("data") or {}).get("data") or {}
    secrets: dict[str, Any] = {}
    ignored: list[str] = []
    for key, value in raw.items():
        name = key.lower()
        if name in SECRET_FIELDS:
            secrets[name] = value
        else:
            ignored.append(key)
    if ignored:
        # Degerler ASLA loglanmaz, yalnizca anahtar adlari
        log.warning("Vault'taki su anahtarlar sir alani degil, yok sayildi: %s", ", ".join(sorted(ignored)))
    log.info("Vault'tan %d sir yuklendi (%s)", len(secrets), path)
    return secrets
