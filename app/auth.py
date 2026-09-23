"""Kimlik: istegi KIM yapiyor ve NE yapmaya yetkili.

IKI MOD (AUTH_MODE)
-------------------
keys (varsayilan, demo)
    Ic uclar X-Source-Key, onay/yonetim uclari X-Admin-Key ister. Yonetici anahtari
    TUM onaylayici rollerini tasir: kisi ayrimi ve onay matrisi fiilen devre disidir.

oidc (uretim)
    Kisiler Entra ID / Okta / Google Workspace ile giris yapar. Roller IdP'deki grup/rol
    bilgisinden OIDC_ROLE_MAP ile turetilir; onay yetkisi config/approval_policy.json'dan gelir.
      * Panel: sunucu tarafli Authorization Code + PKCE. Token tarayiciya HIC inmez; tarayici
        yalnizca imzali, HttpOnly bir oturum cerezi tasir. Cerezle gelen yazma isteklerinde
        X-CSRF-Token zorunludur.
      * API istemcileri: Authorization: Bearer <IdP token'i> (imza JWKS ile dogrulanir).
      * Slack/Teams botlari gibi entegrasyonlar X-Source-Key ile devam eder; talep acabilir,
        ONAYLAYAMAZ. X-Admin-Key bu modda kabul EDILMEZ.

Her iki modda da kaynak (internal_panel, internal_api, ...) kimlik bilgisinden turetilir;
hava boslugu kurallari (app/security.py) degismez.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import rbac
from app.config import get_settings
from app.security import require_internal

Method = Literal["source_key", "admin_key", "oidc_session", "oidc_bearer"]

SESSION_COOKIE = "mandate_session"
LOGIN_COOKIE = "mandate_login"
CSRF_HEADER = "X-CSRF-Token"
MIN_ADMIN_KEY_LENGTH = 16
MIN_SESSION_SECRET_LENGTH = 32
LOGIN_TTL_SECONDS = 600
# Cikista IdP'ye "bu oturumu kapat" ipucu olarak verilir. Cerez 4 KB'yi asmasin diye yalnizca
# makul boydaki id_token saklanir; daha buyukse IdP cikisi onay sayfasiyla (client_id) yapilir.
MAX_ID_TOKEN_IN_SESSION = 2000
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Simetrik (HS*) ve "none" bilincli olarak YOK: imza IdP'nin acik anahtariyla dogrulanmali.
ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    name: str
    roles: frozenset[str]
    permissions: frozenset[str]
    source: str
    method: Method
    csrf: str = ""

    @property
    def is_person(self) -> bool:
        """Kimligi IdP tarafindan dogrulanmis gercek bir kisi mi?"""
        return self.method in ("oidc_session", "oidc_bearer")

    @property
    def display(self) -> str:
        return self.email or self.name or self.subject

    def public(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "email": self.email,
            "name": self.name,
            "roles": sorted(self.roles),
            "permissions": sorted(self.permissions),
            "source": self.source,
            "method": self.method,
            "is_person": self.is_person,
        }


def integration_principal(source: str) -> Principal:
    return Principal(
        subject=f"integration:{source}", email="", name=source, roles=frozenset(),
        permissions=rbac.INTEGRATION_PERMISSIONS, source=source, method="source_key",
    )


# Demo (keys) modunun yonetici anahtari: tum roller. Kisi kimligi yoktur; dort goz
# kontrolu kisi karsilastirmasi yapamaz, inceleyen adi istekte beyan edilir.
ADMIN_KEY_PRINCIPAL = Principal(
    subject="admin-key", email="", name="Yonetici anahtari", roles=frozenset(rbac.ROLES),
    permissions=rbac.permissions_for(frozenset(rbac.ROLES)), source="internal_panel", method="admin_key",
)
# keys modunda bu izinler X-Admin-Key, digerleri X-Source-Key ister
_ADMIN_KEY_PERMISSIONS = frozenset({rbac.APPROVALS_DECIDE, rbac.MEMORY_WRITE})


class AuthError(Exception):
    """Token / IdP dogrulamasi basarisiz (istemciye ayrinti verilmez)."""


# ==========================================================================
# keys modu
# ==========================================================================
def admin_auth_state() -> str:
    """'enabled' | 'missing' | 'weak' | 'disabled' (oidc modunda anahtar kullanilmaz)."""
    settings = get_settings()
    if settings.auth_mode != "keys":
        return "disabled"
    key = (settings.admin_api_key or "").strip()
    if not key:
        return "missing"
    if len(key) < MIN_ADMIN_KEY_LENGTH:
        return "weak"
    return "enabled"


def require_admin_key(x_admin_key: str | None) -> None:
    """FAIL-CLOSED: anahtar tanimli degil/zayif -> 503; baslik yok -> 401; yanlis -> 403.
    Karsilastirma sabit zamanlidir."""
    if admin_auth_state() != "enabled":
        raise HTTPException(
            503,
            "Onay uclari devre disi: ADMIN_API_KEY tanimli degil veya en az "
            f"{MIN_ADMIN_KEY_LENGTH} karakter degil.",
        )
    if not x_admin_key:
        raise HTTPException(401, "X-Admin-Key basligi gerekli.", headers={"WWW-Authenticate": "X-Admin-Key"})
    expected = (get_settings().admin_api_key or "").strip()
    if not secrets.compare_digest(x_admin_key.encode(), expected.encode()):
        raise HTTPException(403, "Gecersiz admin anahtari.")


# ==========================================================================
# Imzali cerezler (oturum + giris durumu)
# ==========================================================================
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict[str, Any], secret: str, purpose: str) -> str:
    """`purpose` imzaya katilir: giris cerezi oturum cerezi yerine kullanilamaz."""
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    mac = hmac.new(secret.encode(), f"{purpose}.{body}".encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unsign(token: str | None, secret: str, purpose: str) -> dict[str, Any] | None:
    """Imza gecersiz veya suresi dolmussa None."""
    if not token or "." not in token or not secret:
        return None
    body, mac = token.rsplit(".", 1)
    expected = _b64(hmac.new(secret.encode(), f"{purpose}.{body}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or float(payload.get("exp", 0)) < time.time():
        return None
    return payload


# ==========================================================================
# oidc modu: yapilandirma
# ==========================================================================
def parse_role_map(raw: str) -> dict[str, str]:
    """'Grup-Adi:rol,kisi@acme.com:rol' -> {'grup-adi': 'rol', ...}. Anahtarlar kucuk harf.
    Grup adinda ':' olabilir; ayrac SONDAKI ':'dir. Bilinmeyen rol -> ValueError."""
    out: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, role = item.rpartition(":")
        key, role = key.strip().lower(), role.strip()
        if not sep or not key:
            raise ValueError(f"OIDC_ROLE_MAP girdisi 'ad:rol' biciminde degil: {item!r}")
        if role not in rbac.ROLES:
            raise ValueError(f"OIDC_ROLE_MAP bilinmeyen rol: {role!r} (gecerli: {', '.join(rbac.ROLES)})")
        out[key] = role
    return out


def _csv(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_dev_issuer(issuer: str) -> bool:
    """Yalnizca bu makinedeki gelistirme IdP'si (orn. Keycloak sso-dev) http kullanabilir."""
    url = urlparse(issuer)
    return url.scheme == "http" and url.hostname in LOOPBACK_HOSTS


def oidc_problems() -> list[str]:
    """OIDC yapilandirmasindaki eksikler. Bos degilse kimlik gerektiren uclar 503 doner."""
    s = get_settings()
    problems = []
    if not (s.oidc_issuer.startswith("https://") or is_dev_issuer(s.oidc_issuer)):
        problems.append("OIDC_ISSUER https:// ile baslamali (http yalnizca localhost / 127.0.0.1 icin)")
    if not s.oidc_client_id:
        problems.append("OIDC_CLIENT_ID tanimli degil")
    if not s.oidc_client_secret:
        problems.append("OIDC_CLIENT_SECRET tanimli degil")
    if len(s.session_secret) < MIN_SESSION_SECRET_LENGTH:
        problems.append(f"SESSION_SECRET en az {MIN_SESSION_SECRET_LENGTH} karakter olmali")
    try:
        parse_role_map(s.oidc_role_map)
    except ValueError as exc:
        problems.append(str(exc))
    unknown = [r for r in _csv(s.oidc_default_roles) if r not in rbac.ROLES]
    if unknown:
        problems.append(f"OIDC_DEFAULT_ROLES bilinmeyen rol: {unknown}")
    return problems


def _require_oidc_ready() -> None:
    if get_settings().auth_mode != "oidc":
        raise HTTPException(404, "SSO kapali (AUTH_MODE=keys).")
    problems = oidc_problems()
    if problems:
        raise HTTPException(503, "SSO yapilandirmasi eksik: " + "; ".join(problems))


# ==========================================================================
# oidc modu: IdP istemcisi
# ==========================================================================
class OIDCProvider:
    """Discovery + JWKS onbellegi + token dogrulama + kod degisimi."""

    METADATA_TTL = 3600
    JWKS_TTL = 3600
    JWKS_MIN_REFRESH = 60  # bilinmeyen kid ile IdP'yi bogmamak icin

    def __init__(
        self, issuer: str, transport: httpx.AsyncBaseTransport | None = None, discovery_url: str = ""
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.transport = transport
        self.discovery_url = discovery_url or f"{self.issuer}/.well-known/openid-configuration"
        # http uc noktalarina yalnizca yerel gelistirme IdP'sinde izin verilir
        self.allowed_schemes = ("https://", "http://") if is_dev_issuer(self.issuer) else ("https://",)
        self._meta: dict[str, Any] | None = None
        self._meta_at = 0.0
        self._keys: dict[str | None, jwt.PyJWK] = {}
        self._keys_at = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0, transport=self.transport)

    async def metadata(self) -> dict[str, Any]:
        if self._meta and time.time() - self._meta_at < self.METADATA_TTL:
            return self._meta
        async with self._client() as client:
            res = await client.get(self.discovery_url)
        if res.status_code != 200:
            raise AuthError(f"discovery HTTP {res.status_code}")
        meta = res.json()
        # Spoof'a karsi: belge, yapilandirilan issuer'a ait olmali (OIDC Discovery 4.3)
        if str(meta.get("issuer", "")).rstrip("/") != self.issuer:
            raise AuthError("discovery issuer uyusmuyor")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not str(meta.get(key, "")).startswith(self.allowed_schemes):
                raise AuthError(f"discovery: {key} https degil")
        self._meta, self._meta_at = meta, time.time()
        return meta

    async def _signing_keys(self, force: bool = False) -> dict[str | None, jwt.PyJWK]:
        now = time.time()
        fresh = now - self._keys_at < self.JWKS_TTL
        if self._keys and (fresh and not force or now - self._keys_at < self.JWKS_MIN_REFRESH):
            return self._keys
        meta = await self.metadata()
        async with self._client() as client:
            res = await client.get(meta["jwks_uri"])
        if res.status_code != 200:
            raise AuthError(f"JWKS HTTP {res.status_code}")
        try:
            keyset = jwt.PyJWKSet.from_dict(res.json())
        except jwt.PyJWKSetError as exc:
            raise AuthError("JWKS kullanilabilir anahtar icermiyor") from exc
        self._keys = {k.key_id: k for k in keyset.keys}
        self._keys_at = now
        return self._keys

    async def verify(self, token: str, audience: str, nonce: str | None = None) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("token bicimi gecersiz") from exc
        alg = header.get("alg")
        if alg not in ALLOWED_ALGS:
            raise AuthError(f"izin verilmeyen algoritma: {alg!r}")
        kid = header.get("kid")
        keys = await self._signing_keys()
        if kid not in keys:  # IdP anahtar dondurmus olabilir
            keys = await self._signing_keys(force=True)
        key = keys.get(kid)
        if key is None:
            raise AuthError("imza anahtari bulunamadi")
        meta = await self.metadata()
        try:
            claims = jwt.decode(
                token, key.key, algorithms=[alg], audience=audience, issuer=meta["issuer"], leeway=60,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"token reddedildi: {type(exc).__name__}") from exc
        if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise AuthError("nonce uyusmuyor")
        return claims

    async def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
        s = get_settings()
        meta = await self.metadata()
        data = {
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "code_verifier": verifier,
        }
        auth = None
        if s.oidc_token_auth_method == "client_secret_basic":
            auth = httpx.BasicAuth(s.oidc_client_id, s.oidc_client_secret)
        else:
            data |= {"client_id": s.oidc_client_id, "client_secret": s.oidc_client_secret}
        async with self._client() as client:
            res = await client.post(meta["token_endpoint"], data=data, auth=auth, headers={"Accept": "application/json"})
        if res.status_code != 200:
            raise AuthError(f"kod degisimi HTTP {res.status_code}")
        return res.json()


# Testler sahte IdP baglamak icin degistirir.
_transport: httpx.AsyncBaseTransport | None = None
_provider: OIDCProvider | None = None


def get_provider() -> OIDCProvider:
    global _provider
    s = get_settings()
    issuer = s.oidc_issuer.rstrip("/")
    wanted = OIDCProvider(issuer, _transport, s.oidc_discovery_url).discovery_url
    if (_provider is None or _provider.issuer != issuer or _provider.transport is not _transport
            or _provider.discovery_url != wanted):
        _provider = OIDCProvider(issuer, _transport, s.oidc_discovery_url)
    return _provider


def roles_from_claims(claims: dict[str, Any], email: str) -> frozenset[str]:
    s = get_settings()
    raw = claims.get(s.oidc_roles_claim) or []
    values = [raw] if isinstance(raw, str) else [str(v) for v in raw if isinstance(v, (str, int))]
    keys = {v.strip().lower() for v in values} | {email}
    role_map = parse_role_map(s.oidc_role_map)
    return frozenset(_csv(s.oidc_default_roles)) | {role_map[k] for k in keys if k in role_map}


def principal_from_claims(claims: dict[str, Any], method: Method, source: str) -> Principal:
    s = get_settings()
    email = str(claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "").strip().lower()
    if "@" not in email:
        raise HTTPException(403, "Kimlik bilgisinde e-posta yok; IdP'de 'email' claim'ini acin.")
    if claims.get("email_verified") is False:
        raise HTTPException(403, "E-posta adresi IdP tarafindan dogrulanmamis.")
    domains = {d.lower() for d in _csv(s.oidc_allowed_domains)}
    if domains and email.rsplit("@", 1)[1] not in domains:
        raise HTTPException(403, "Bu alan adindaki hesaplar Mandate'e giremez.")
    roles = roles_from_claims(claims, email)
    return Principal(
        subject=str(claims["sub"]), email=email, name=str(claims.get("name") or email)[:120],
        roles=roles, permissions=rbac.permissions_for(roles), source=source, method=method,
    )


async def _oidc_principal(request: Request, x_source_key: str | None, authorization: str | None) -> Principal:
    s = get_settings()
    problems = oidc_problems()
    if problems:
        raise HTTPException(503, "SSO yapilandirmasi eksik: " + "; ".join(problems))

    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(401, "Authorization: Bearer <token> bekleniyor.", headers={"WWW-Authenticate": "Bearer"})
        try:
            claims = await get_provider().verify(token.strip(), audience=s.oidc_audience or s.oidc_client_id)
        except AuthError:
            raise HTTPException(401, "Gecersiz veya suresi dolmus token.", headers={"WWW-Authenticate": "Bearer"})
        except httpx.HTTPError:
            raise HTTPException(503, "Kimlik saglayicisina ulasilamadi.")
        return principal_from_claims(claims, "oidc_bearer", "internal_api")

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        session = unsign(cookie, s.session_secret, "session")
        if session is None:
            raise HTTPException(401, "Oturum gecersiz veya suresi doldu; yeniden giris yapin.")
        if request.method not in SAFE_METHODS and not hmac.compare_digest(
            request.headers.get(CSRF_HEADER, ""), str(session.get("csrf", ""))
        ):
            raise HTTPException(403, f"{CSRF_HEADER} basligi eksik veya hatali.")
        roles = frozenset(r for r in session.get("roles", []) if r in rbac.ROLES)
        return Principal(
            subject=str(session["sub"]), email=str(session["email"]), name=str(session.get("name", "")),
            roles=roles, permissions=rbac.permissions_for(roles), source="internal_panel",
            method="oidc_session", csrf=str(session.get("csrf", "")),
        )

    if x_source_key:
        return integration_principal(require_internal(x_source_key))

    raise HTTPException(401, "Giris gerekli.", headers={"WWW-Authenticate": "Bearer"})


# ==========================================================================
# FastAPI bagimliliklari
# ==========================================================================
def require(permission: str):
    """Ucun gerektirdigi izni kontrol eden bagimlilik; cagiran Principal'i dondurur."""

    async def dependency(
        request: Request,
        x_source_key: str | None = Header(default=None),
        x_admin_key: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> Principal:
        if get_settings().auth_mode == "keys":
            if permission in _ADMIN_KEY_PERMISSIONS:
                require_admin_key(x_admin_key)
                principal = ADMIN_KEY_PRINCIPAL
            else:
                principal = integration_principal(require_internal(x_source_key))
        else:
            principal = await _oidc_principal(request, x_source_key, authorization)
        if permission not in principal.permissions:
            raise HTTPException(403, f"Bu islem icin yetkiniz yok ({permission}).")
        return principal

    return dependency


# ==========================================================================
# Giris / cikis uclari
# ==========================================================================
router = APIRouter(prefix="/auth", tags=["auth"])


def _safe_next(target: str | None) -> str:
    """Acik yonlendirmeye karsi: yalnizca ayni site icindeki goreli yollar."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def _redirect_uri(request: Request) -> str:
    return get_settings().oidc_redirect_uri or str(request.url_for("auth_callback"))


@router.get("/config")
async def auth_config() -> dict[str, Any]:
    """Herkese acik: panel hangi giris ekranini gosterecegini buradan ogrenir."""
    s = get_settings()
    return {"mode": s.auth_mode, "ready": s.auth_mode == "keys" or not oidc_problems()}


@router.get("/login")
async def login(request: Request, next: str = "/") -> RedirectResponse:
    _require_oidc_ready()
    s = get_settings()
    try:
        meta = await get_provider().metadata()
    except (AuthError, httpx.HTTPError):
        raise HTTPException(503, "Kimlik saglayicisina ulasilamadi.")
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {
        "response_type": "code", "client_id": s.oidc_client_id, "redirect_uri": _redirect_uri(request),
        "scope": s.oidc_scopes, "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    response = RedirectResponse(f"{meta['authorization_endpoint']}?{urlencode(params)}", status_code=302)
    pending = {"state": state, "nonce": nonce, "verifier": verifier, "next": _safe_next(next),
               "exp": time.time() + LOGIN_TTL_SECONDS}
    response.set_cookie(
        LOGIN_COOKIE, sign(pending, s.session_secret, "login"), max_age=LOGIN_TTL_SECONDS,
        httponly=True, secure=s.session_cookie_secure, samesite="lax", path="/auth",
    )
    return response


@router.get("/callback", name="auth_callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    _require_oidc_ready()
    s = get_settings()
    pending = unsign(request.cookies.get(LOGIN_COOKIE), s.session_secret, "login")
    if pending is None:
        raise HTTPException(400, "Giris oturumu bulunamadi veya suresi doldu; yeniden deneyin.")
    if error:
        raise HTTPException(401, f"Kimlik saglayicisi girisi reddetti ({error[:60]}).")
    if not code or not state or not hmac.compare_digest(state, str(pending["state"])):
        raise HTTPException(400, "Gecersiz giris durumu (state).")
    provider = get_provider()
    try:
        tokens = await provider.exchange_code(code, _redirect_uri(request), str(pending["verifier"]))
        claims = await provider.verify(str(tokens.get("id_token") or ""), s.oidc_client_id, nonce=str(pending["nonce"]))
    except AuthError:
        raise HTTPException(401, "Kimlik dogrulanamadi.")
    except httpx.HTTPError:
        raise HTTPException(503, "Kimlik saglayicisina ulasilamadi.")
    principal = principal_from_claims(claims, "oidc_session", "internal_panel")

    ttl = s.session_ttl_minutes * 60
    session = {
        "sub": principal.subject, "email": principal.email, "name": principal.name,
        "roles": sorted(principal.roles), "csrf": secrets.token_urlsafe(24), "exp": time.time() + ttl,
    }
    id_token = str(tokens.get("id_token") or "")
    if len(id_token) <= MAX_ID_TOKEN_IN_SESSION:
        session["idt"] = id_token
    response = RedirectResponse(str(pending["next"]), status_code=303)
    response.set_cookie(
        SESSION_COOKIE, sign(session, s.session_secret, "session"), max_age=ttl,
        httponly=True, secure=s.session_cookie_secure, samesite="lax", path="/",
    )
    response.delete_cookie(LOGIN_COOKIE, path="/auth")
    return response


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Mandate oturumunu kapatir ve tarayicinin gitmesi gereken adresi dondurur.

    IdP bir cikis ucu (end_session_endpoint) sunuyorsa adres odur: IdP oturumu da kapanir,
    boylece "Giris yap" baska bir hesapla giris yapmaya izin verir. Sunmuyorsa (orn. Google)
    yalnizca Mandate oturumu kapanir.
    """
    s = get_settings()
    target = "/"
    if s.auth_mode == "oidc" and not oidc_problems():
        session = unsign(request.cookies.get(SESSION_COOKIE), s.session_secret, "session") or {}
        try:
            meta = await get_provider().metadata()
        except (AuthError, httpx.HTTPError):
            meta = {}
        end_session = str(meta.get("end_session_endpoint") or "")
        if end_session.startswith(get_provider().allowed_schemes):
            params = {"client_id": s.oidc_client_id, "post_logout_redirect_uri": str(request.base_url)}
            if session.get("idt"):
                params["id_token_hint"] = str(session["idt"])
            target = f"{end_session}?{urlencode(params)}"
    response = JSONResponse({"redirect": target})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me")
async def me(principal: Principal = Depends(require(rbac.SYSTEM_READ))) -> dict[str, Any]:
    return {**principal.public(), "csrf_token": principal.csrf, "role_labels": {r: rbac.ROLES[r] for r in principal.roles}}
