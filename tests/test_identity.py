"""Kimlik (SSO / OIDC), rol bazli erisim ve onay matrisi.

Gercek bir IdP yerine sahte bir OIDC saglayicisi (FakeIdP) kullanilir: discovery, JWKS,
yetkilendirme kodu + PKCE ve RS256 imzali id_token gercek protokoldeki gibi uretilir.
Uygulama tarafinda HICBIR dogrulama atlanmaz (imza, issuer, audience, nonce, state, PKCE).
"""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth, rbac
from app.config import get_settings
from app.schemas import ExpenseRequest, RouteDecision
from app.llm.base import ToolCall, ToolChatResult
from app.tools import it_tools
from tests.conftest import ADMIN_HEADERS, PANEL_HEADERS, SLACK_HEADERS

ISSUER = "https://idp.acme.test"
CLIENT_ID = "mandate-panel"
BASE = "https://mandate.acme.test"

USERS = {
    "ali": {"email": "ali@acme.com", "name": "Ali Calisan", "groups": []},
    "ayse": {"email": "ayse@acme.com", "name": "Ayse Finans", "groups": ["grp-finance"]},
    "cem": {"email": "cem@acme.com", "name": "Cem CFO", "groups": ["grp-cfo", "grp-finance"]},
    "ilker": {"email": "ilker@acme.com", "name": "Ilker IT", "groups": ["grp-it"]},
    "deniz": {"email": "deniz@acme.com", "name": "Deniz Denetci", "groups": ["grp-audit"]},
    "sys": {"email": "sys@acme.com", "name": "Sistem Yoneticisi", "groups": ["grp-admin"]},
    "yabanci": {"email": "x@rakip.com", "name": "Disaridan", "groups": ["grp-cfo"]},
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeIdP:
    """Minimal ama protokole sadik bir OIDC saglayicisi (httpx MockTransport)."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.codes: dict[str, dict] = {}
        self.token_requests: list[dict] = []
        self.nonce_override: str | None = None  # saldiri: baska bir girise ait token

    def jwk(self) -> dict:
        pub = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**pub, "kid": self.kid, "use": "sig", "alg": "RS256"}

    def mint(self, user: str, *, aud=CLIENT_ID, iss=ISSUER, nonce=None, exp_in=300, key=None, alg="RS256", **extra) -> str:
        u = USERS[user]
        now = int(time.time())
        claims = {"iss": iss, "aud": aud, "sub": f"sub-{user}", "email": u["email"], "name": u["name"],
                  "groups": u["groups"], "iat": now, "exp": now + exp_in, **extra}
        if nonce is not None:
            claims["nonce"] = nonce
        return jwt.encode(claims, key or self.key, algorithm=alg, headers={"kid": self.kid})

    def authorize(self, login_location: str, user: str) -> tuple[str, str]:
        """Kullanicinin IdP'de giris yaptigini taklit eder: (code, state) dondurur."""
        q = {k: v[0] for k, v in parse_qs(urlparse(login_location).query).items()}
        assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID
        assert q["code_challenge_method"] == "S256" and "openid" in q["scope"]
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"user": user, "nonce": q["nonce"], "challenge": q["code_challenge"],
                            "redirect_uri": q["redirect_uri"]}
        return code, q["state"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/keys",
                "end_session_endpoint": f"{ISSUER}/logout",
            })
        if url == f"{ISSUER}/keys":
            return httpx.Response(200, json={"keys": [self.jwk()]})
        if url == f"{ISSUER}/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            grant = self.codes.pop(form.get("code", ""), None)  # kod tek kullanimlik
            if (grant is None or form.get("client_secret") != "idp-client-secret"
                    or form.get("redirect_uri") != grant["redirect_uri"]
                    or _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != grant["challenge"]):
                return httpx.Response(400, json={"error": "invalid_grant"})
            nonce = self.nonce_override or grant["nonce"]
            return httpx.Response(200, json={"id_token": self.mint(grant["user"], nonce=nonce),
                                              "access_token": "opaque", "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
def idp(monkeypatch) -> FakeIdP:
    fake = FakeIdP()
    s = get_settings()
    for name, value in {
        "auth_mode": "oidc", "oidc_issuer": ISSUER, "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": "idp-client-secret", "oidc_roles_claim": "groups",
        "oidc_role_map": "grp-finance:finance_manager,grp-cfo:cfo,grp-it:it_admin,grp-audit:auditor,grp-admin:admin",
        "oidc_allowed_domains": "acme.com", "session_secret": "s" * 40,
        "oidc_redirect_uri": f"{BASE}/auth/callback",
    }.items():
        monkeypatch.setattr(s, name, value)
    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(auth, "_provider", None)
    return fake


@pytest.fixture
def app_(llm, seeded_memory):
    """TEK istemci: graf ve checkpoint kilidi bu istemcinin event loop'una baglidir.
    Farkli kullanicilar ayri cerez tasiyan UserSession'larla temsil edilir."""
    from app.main import app

    llm.on(RouteDecision, lambda s, u: RouteDecision(agent=llm.route, reasoning="t", confidence=0.9))
    llm.route = "it_ops"
    llm.tool_handler = lambda s, u, t: ToolChatResult(tool_calls=[ToolCall("reset_password", {"email": "ali@acme.com"})])
    with TestClient(app, base_url=BASE) as c:
        yield c


def fresh(c: TestClient) -> TestClient:
    """Cerezsiz yeni bir tarayici gibi davranir."""
    c.cookies.clear()
    return c


class UserSession:
    """Bir kullanicinin tarayicisi: oturum cerezi + CSRF token'i."""

    def __init__(self, c: TestClient, cookie: str, csrf: str, cookie_names: set[str]) -> None:
        self.c, self.cookie, self.csrf, self.cookie_names = c, cookie, csrf, cookie_names

    def _call(self, method, url, headers=None, **kw):
        fresh(self.c)
        h = {"Cookie": f"{auth.SESSION_COOKIE}={self.cookie}"}
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        return self.c.request(method, url, headers={**h, **(headers or {})}, **kw)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)


def login(c: TestClient, idp: FakeIdP, user: str) -> UserSession:
    """Tarayicidaki gercek akis: /auth/login -> IdP -> /auth/callback -> oturum cerezi."""
    fresh(c)
    r = c.get("/auth/login", params={"next": "/#approvals"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(f"{ISSUER}/authorize?")
    code, state = idp.authorize(r.headers["location"], user)
    r = c.get("/auth/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/#approvals"
    names = set(c.cookies.keys())
    cookie = c.cookies[auth.SESSION_COOKIE]
    session = UserSession(c, cookie, "", names)
    session.csrf = session.get("/auth/me").json()["csrf_token"]
    return session


def ask(c: TestClient, text="Sifremi sifirlar misin", **body):
    return c.post("/agent/request", json={"text": text, **body})


# ==========================================================================
# Onay matrisi (saf mantik)
# ==========================================================================
def expense_action(amount, over_budget=False):
    return ({"kind": "expense_approval", "arguments": {"department": "IT", "amount": amount, "category": "x"}},
            {"over_budget": over_budget})


@pytest.mark.parametrize("amount,over_budget,rule,roles", [
    (45_000, False, "harcama", {"finance_manager", "cfo"}),
    (50_000, False, "harcama", {"finance_manager", "cfo"}),  # sinir dahil
    (50_000.01, False, "harcama-buyuk", {"cfo"}),
    (12_000, True, "harcama-butce-asimi", {"cfo"}),  # kucuk ama butce asimi
])
def test_default_policy_for_expenses(amount, over_budget, rule, roles):
    r = rbac.get_policy().rule_for(*expense_action(amount, over_budget))
    assert r.id == rule and r.roles == roles


def test_default_policy_covers_every_action():
    p = rbac.get_policy()
    assert p.rule_for({"kind": "tool_call", "tool": "grant_access"}, {}).roles == {"it_admin"}
    assert p.rule_for({"kind": "contract_signoff", "details": {"risk_level": "critical"}}, {}).roles == {"legal"}
    assert p.rule_for({"kind": "vendor_approval"}, {}).roles == {"procurement_manager"}
    assert p.rule_for({"kind": "bilinmeyen"}, {}) is None  # kimse onaylayamaz
    # Tutar kodun hesapladigi alandan gelir; metin/bool tutar sayilmaz -> buyuk tutar kurali eslesmez
    assert p.rule_for({"kind": "expense_approval", "arguments": {"amount": "999999"}}, {}).id == "harcama"


BASE_RULES = [{"id": a, "action": a, "roles": ["cfo"]} for a in rbac.ACTION_KINDS]


@pytest.mark.parametrize("bad,message", [
    ([{"id": "x", "action": "tool_call", "roles": ["admin"]}], "onay veremeyen"),  # gorev ayriligi
    ([{"id": "x", "action": "tool_call", "roles": ["employee"]}], "onay veremeyen"),
    ([{"id": "x", "action": "tool_call", "roles": ["patron"]}], "onay veremeyen"),
    ([{"id": "x", "action": "tool_call", "roles": []}], "roles"),
    ([{"id": "x", "action": "odeme", "roles": ["cfo"]}], "bilinmeyen eylem"),
    ([{"id": "x", "action": "tool_call", "roles": ["cfo"], "when": {"amount_gte": 5}}], "bilinmeyen kosul"),
    ([{"id": "x", "action": "tool_call", "roles": ["cfo"], "when": {"amount_gt": "5"}}], "sayi"),
    ([{"id": "tool_call", "action": "tool_call", "roles": ["cfo"]}], "tekrarlanan"),
])
def test_invalid_policy_is_rejected(bad, message):
    with pytest.raises(rbac.PolicyError, match=message):
        rbac.ApprovalPolicy.from_dict({"rules": bad + BASE_RULES})


def test_policy_must_cover_every_action_kind():
    with pytest.raises(rbac.PolicyError, match="kimse onaylayamaz"):
        rbac.ApprovalPolicy.from_dict({"rules": BASE_RULES[:-1]})


def test_admin_role_cannot_approve_anything():
    assert rbac.APPROVALS_DECIDE not in rbac.ROLE_PERMISSIONS["admin"]
    assert rbac.APPROVALS_DECIDE not in rbac.INTEGRATION_PERMISSIONS


# ==========================================================================
# Imzali cerezler ve rol eslemesi
# ==========================================================================
def test_signed_cookie_rejects_tampering_expiry_and_purpose_swap():
    secret = "k" * 40
    token = auth.sign({"email": "ali@acme.com", "roles": ["employee"], "exp": time.time() + 60}, secret, "session")
    assert auth.unsign(token, secret, "session")["email"] == "ali@acme.com"
    body, mac = token.rsplit(".", 1)
    forged = auth._b64(json.dumps({"email": "ali@acme.com", "roles": ["cfo"], "exp": time.time() + 60}).encode())
    assert auth.unsign(f"{forged}.{mac}", secret, "session") is None  # rol yukseltme
    assert auth.unsign(token, "b" * 40, "session") is None
    assert auth.unsign(token, secret, "login") is None  # giris cerezi oturum yerine gecemez
    old = auth.sign({"exp": time.time() - 1}, secret, "session")
    assert auth.unsign(old, secret, "session") is None
    assert auth.unsign("bozuk", secret, "session") is None and auth.unsign(None, secret, "session") is None


def test_role_map_parsing():
    assert auth.parse_role_map(" CN=Finans:Ekip:finance_manager , cfo@acme.com:cfo ,") == {
        "cn=finans:ekip": "finance_manager", "cfo@acme.com": "cfo"}
    with pytest.raises(ValueError, match="bilinmeyen rol"):
        auth.parse_role_map("grp:patron")
    with pytest.raises(ValueError):
        auth.parse_role_map("sadece-grup")


# ==========================================================================
# SSO akisi
# ==========================================================================
def test_login_flow_creates_session_with_mapped_roles(app_, idp):
    c = login(app_, idp, "cem")
    me = c.get("/auth/me").json()
    assert me["email"] == "cem@acme.com" and me["is_person"] and me["method"] == "oidc_session"
    assert set(me["roles"]) == {"employee", "cfo", "finance_manager"}
    assert rbac.APPROVALS_DECIDE in me["permissions"]
    # token tarayiciya inmez: yalnizca HttpOnly oturum cerezi
    assert c.cookie_names == {auth.SESSION_COOKIE}
    # PKCE ve istemci sirri IdP'ye gitti
    sent = idp.token_requests[-1]
    assert sent["code_verifier"] and sent["client_secret"] == "idp-client-secret"


def test_login_rejects_bad_state_replayed_code_and_foreign_domain(app_, idp):
    c = fresh(app_)
    loc = c.get("/auth/login", follow_redirects=False).headers["location"]
    code, state = idp.authorize(loc, "ali")
    assert c.get("/auth/callback", params={"code": code, "state": "baska"}, follow_redirects=False).status_code == 400
    assert c.get("/auth/callback", params={"code": code, "state": state}, follow_redirects=False).status_code == 303
    # ayni kod ikinci kez kullanilamaz (IdP reddeder)
    c2 = fresh(app_)
    loc2 = c2.get("/auth/login", follow_redirects=False).headers["location"]
    _, state2 = idp.authorize(loc2, "ali")
    assert c2.get("/auth/callback", params={"code": code, "state": state2}, follow_redirects=False).status_code == 401
    # giris cerezi olmadan callback (baska tarayicidan) -> 400
    assert fresh(app_).get(
        "/auth/callback", params={"code": "x", "state": state}, follow_redirects=False).status_code == 400
    # izin verilmeyen alan adi (grubu cfo olsa bile)
    c3 = fresh(app_)
    loc3 = c3.get("/auth/login", follow_redirects=False).headers["location"]
    code3, state3 = idp.authorize(loc3, "yabanci")
    assert c3.get("/auth/callback", params={"code": code3, "state": state3}, follow_redirects=False).status_code == 403
    assert auth.SESSION_COOKIE not in c3.cookies


def test_login_rejects_id_token_from_another_login(app_, idp):
    """Token yerine koyma: IdP'den gelen id_token bu girisin nonce'unu tasimiyorsa oturum acilmaz."""
    idp.nonce_override = "baska-bir-girisin-nonce-degeri"
    c = fresh(app_)
    loc = c.get("/auth/login", follow_redirects=False).headers["location"]
    code, state = idp.authorize(loc, "cem")
    r = c.get("/auth/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert r.status_code == 401 and auth.SESSION_COOKIE not in c.cookies


@pytest.mark.parametrize("target,expected", [
    ("/#logs", "/#logs"), ("https://kotu.site", "/"), ("//kotu.site", "/"), ("/\\kotu.site", "/"), ("", "/"),
])
def test_login_next_cannot_redirect_off_site(app_, idp, target, expected):
    c = fresh(app_)
    loc = c.get("/auth/login", params={"next": target}, follow_redirects=False).headers["location"]
    code, state = idp.authorize(loc, "ali")
    assert c.get("/auth/callback", params={"code": code, "state": state}, follow_redirects=False).headers["location"] == expected


def test_forged_or_foreign_tokens_are_rejected(app_, idp):
    c = fresh(app_)
    ok = c.get("/auth/me", headers={"Authorization": f"Bearer {idp.mint('ali')}"})
    assert ok.status_code == 200 and ok.json()["method"] == "oidc_bearer" and ok.json()["source"] == "internal_api"
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = {
        "baska anahtar": idp.mint("ali", key=other_key),
        "baska audience": idp.mint("ali", aud="baska-uygulama"),
        "baska issuer": idp.mint("ali", iss="https://kotu.idp"),
        "suresi dolmus": idp.mint("ali", exp_in=-3600),
        "HS256 (anahtar karistirma)": jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "email": "ali@acme.com",
                                                   "iat": int(time.time()), "exp": int(time.time()) + 60},
                                                  "sir", algorithm="HS256", headers={"kid": "k1"}),
        "alg none": jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x"}, None, algorithm="none"),
        "cop": "abc.def.ghi",
    }
    for label, token in bad.items():
        r = c.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, label
    assert c.get("/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_admin_key_is_not_accepted_in_sso_mode(app_, idp):
    c = fresh(app_)
    assert c.get("/approvals", headers=ADMIN_HEADERS).status_code == 401
    assert c.delete("/memory", headers=ADMIN_HEADERS).status_code == 401
    assert c.get("/runs").status_code == 401


def test_incomplete_sso_config_fails_closed(app_, idp, monkeypatch):
    monkeypatch.setattr(get_settings(), "session_secret", "kisa")
    c = fresh(app_)
    assert c.get("/auth/login", follow_redirects=False).status_code == 503
    assert c.get("/auth/me", headers={"Authorization": f"Bearer {idp.mint('cem')}"}).status_code == 503
    assert c.get("/auth/config").json() == {"mode": "oidc", "ready": False}


def test_cookie_requests_need_csrf_header(app_, idp):
    c = login(app_, idp, "ali")
    token, c.csrf = c.csrf, ""
    assert ask(c).status_code == 403  # capraz site formu bu basligi koyamaz
    r = c.post("/agent/request", json={"text": "Sifremi sifirlar misin"}, headers={"X-CSRF-Token": "yanlis"})
    assert r.status_code == 403
    assert c.get("/auth/me").status_code == 200  # okuma istekleri CSRF istemez
    r = c.post("/agent/request", json={"text": "Sifremi sifirlar misin"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 202
    out = c.post("/auth/logout")
    assert out.status_code == 200
    assert f'{auth.SESSION_COOKIE}=""' in out.headers["set-cookie"] and "Max-Age=0" in out.headers["set-cookie"]


# ==========================================================================
# Rol bazli yetki uctan uca
# ==========================================================================
def test_requester_comes_from_identity_not_body(app_, idp):
    ali = login(app_, idp, "ali")
    run = ask(ali, requester="cem@acme.com").json()  # baskasi adina talep acamaz
    assert run["requester"] == "ali@acme.com" and run["source"] == "internal_panel"
    assert run["approval_policy"] == {"rule": "it-islem", "roles": ["it_admin"],
                                      "description": "Sifre sifirlama ve yazilim erisimi: IT yoneticisi"}


def test_it_action_only_it_admin_can_decide_and_identity_is_recorded(app_, idp):
    ali = login(app_, idp, "ali")
    rid = ask(ali).json()["run_id"]
    for who in ("ali", "cem", "sys", "deniz"):  # calisan, CFO, sistem yoneticisi, denetci
        c = login(app_, idp, who)
        r = c.post(f"/approvals/{rid}/decision", json={"approved": True})
        assert r.status_code == 403, who
    cem_queue = login(app_, idp, "cem").get("/approvals").json()
    assert rid not in [x["run_id"] for x in cem_queue]  # CFO'nun kuyrugunda IT islemi gorunmez
    assert rid not in it_tools._EXECUTED

    ilker = login(app_, idp, "ilker")
    assert rid in [x["run_id"] for x in ilker.get("/approvals").json()]
    done = ilker.post(f"/approvals/{rid}/decision", json={"approved": True, "reviewer": "uydurma@acme.com"})
    assert done.status_code == 200, done.text
    a = done.json()["approval"]
    assert a["reviewer"] == "ilker@acme.com"  # beyan edilen isim yok sayildi
    assert a["reviewer_id"] == "sub-ilker" and a["reviewer_roles"] == ["it_admin"]
    assert a["policy_rule"] == "it-islem" and a["auth_method"] == "oidc_session"
    assert rid in it_tools._EXECUTED
    assert any("kural: it-islem" in t for t in done.json()["trace"])


def test_nobody_can_approve_their_own_request(app_, idp, llm):
    llm.tool_handler = lambda s, u, t: ToolChatResult(tool_calls=[ToolCall("reset_password", {"email": "ilker@acme.com"})])
    ilker = login(app_, idp, "ilker")  # IT yoneticisi kendi sifresi icin talep aciyor
    run = ask(ilker).json()
    assert run["status"] == "awaiting_approval", run
    rid = run["run_id"]
    r = ilker.post(f"/approvals/{rid}/decision", json={"approved": True})
    assert r.status_code == 403 and "dort goz" in r.json()["detail"]
    assert rid not in [x["run_id"] for x in ilker.get("/approvals").json()]


@pytest.fixture
def finance(llm):
    def setup(dept, amount):
        llm.route = "finance"
        llm.on(ExpenseRequest, lambda s, u: ExpenseRequest(
            intent="expense_approval", department=dept, amount=amount, currency="TRY",
            category="Dijital reklam", description="Kampanya"))
    return setup


def test_expense_matrix_finance_manager_vs_cfo(app_, idp, finance):
    ali, ayse, cem = (login(app_, idp, u) for u in ("ali", "ayse", "cem"))

    finance("Pazarlama", 45000)
    mid = ask(ali, "Pazarlama icin 45.000 TL reklam harcamasini onayla").json()
    assert mid["status"] == "awaiting_approval" and mid["approval_policy"]["rule"] == "harcama"

    finance("Ar-Ge", 120000)
    big = ask(ali, "Ar-Ge icin 120.000 TL harcama onayla").json()
    assert big["approval_policy"] == {"rule": "harcama-buyuk", "roles": ["cfo"],
                                      "description": "50.000 TL ustu harcama yalnizca CFO onayiyla"}

    queue = {x["run_id"] for x in ayse.get("/approvals").json()}
    assert mid["run_id"] in queue and big["run_id"] not in queue  # kuyrukta yalnizca yetkili oldugu
    assert ayse.post(f"/approvals/{big['run_id']}/decision", json={"approved": True}).status_code == 403
    assert ayse.post(f"/approvals/{big['run_id']}/decision", json={"approved": False}).status_code == 403  # red de yetki ister

    r = ayse.post(f"/approvals/{mid['run_id']}/decision", json={"approved": True})
    assert r.status_code == 200 and r.json()["action_result"]["approver"] == "ayse@acme.com"
    r = cem.post(f"/approvals/{big['run_id']}/decision", json={"approved": True, "comment": "butce plani dahilinde"})
    assert r.status_code == 200 and r.json()["approval"]["reviewer_roles"] == ["cfo"]


def test_employees_see_only_their_own_runs(app_, idp):
    ali, ilker, deniz, sys_ = (login(app_, idp, u) for u in ("ali", "ilker", "deniz", "sys"))
    mine = ask(ali).json()["run_id"]
    theirs = ask(ilker).json()["run_id"]
    ali_runs = {r["run_id"] for r in ali.get("/runs?limit=500").json()}
    assert mine in ali_runs and theirs not in ali_runs
    assert all(r["requester"] == "ali@acme.com" for r in ali.get("/runs?limit=500").json())
    assert ali.get(f"/runs/{theirs}").status_code == 404  # varligi da sizmaz
    assert ali.get(f"/runs/{mine}").status_code == 200
    assert {mine, theirs} <= {r["run_id"] for r in deniz.get("/runs?limit=500").json()}  # denetci hepsini gorur
    assert deniz.post(f"/approvals/{theirs}/decision", json={"approved": True}).status_code == 403
    # sistem yoneticisi (admin + temel employee) baskalarinin is kayitlarini okuyamaz
    assert mine not in {r["run_id"] for r in sys_.get("/runs?limit=500").json()}
    assert ali.get("/approvals").status_code == 403


def test_knowledge_base_writes_need_admin_role(app_, idp):
    files = {"files": ("not.txt", b"Deneme politika metni. " * 5, "text/plain")}
    cem = login(app_, idp, "cem")
    assert cem.post("/memory/upload?domain=it_hr", files=files).status_code == 403
    assert cem.delete("/memory?domain=it_hr").status_code == 403
    sys_ = login(app_, idp, "sys")
    assert sys_.get("/memory/stats").status_code == 200


def test_integrations_keep_working_but_cannot_approve(app_, idp):
    c = fresh(app_)
    r = c.post("/agent/request", json={"text": "Sifremi sifirlar misin", "requester": "ali@acme.com"}, headers=SLACK_HEADERS)
    assert r.status_code == 202 and r.json()["source"] == "internal_slack" and r.json()["requester"] == "ali@acme.com"
    rid = r.json()["run_id"]
    assert c.post(f"/approvals/{rid}/decision", json={"approved": True, "reviewer": "x"}, headers=SLACK_HEADERS).status_code == 403
    assert c.get("/runs", headers=PANEL_HEADERS).status_code == 200


def test_keys_mode_admin_key_still_decides_everything(app_):
    """Demo modu degismedi: yonetici anahtari her kurala uyar; karar kaydinda anahtar yontemi gorunur."""
    c = fresh(app_)
    rid = c.post("/agent/request", json={"text": "Sifremi sifirlar misin", "requester": "ali@acme.com"},
                 headers=PANEL_HEADERS).json()["run_id"]
    assert c.post(f"/approvals/{rid}/decision", json={"approved": True}, headers=ADMIN_HEADERS).status_code == 422
    r = c.post(f"/approvals/{rid}/decision", json={"approved": True, "reviewer": "mudur@acme.com"}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["approval"]["auth_method"] == "admin_key" and r.json()["approval"]["reviewer"] == "mudur@acme.com"
    assert c.get("/auth/config").json() == {"mode": "keys", "ready": True}
    assert c.get("/auth/login", follow_redirects=False).status_code == 404


# ==========================================================================
# Yerel gelistirme IdP'si (Keycloak sso-dev): http yalnizca localhost icin
# ==========================================================================
@pytest.mark.parametrize("issuer,allowed", [
    ("http://localhost:8080/realms/mandate", True),
    ("http://127.0.0.1:8080/realms/mandate", True),
    ("https://login.microsoftonline.com/t/v2.0", True),
    ("http://keycloak:8080/realms/mandate", False),  # ag uzerinden http: token dinlenebilir
    ("http://idp.acme.com/realms/mandate", False),
    ("http://localhost.kotu.site/realms/x", False),
    ("ftp://localhost/x", False),
])
def test_http_issuer_only_on_this_machine(monkeypatch, issuer, allowed):
    s = get_settings()
    for name, value in {"auth_mode": "oidc", "oidc_issuer": issuer, "oidc_client_id": "c",
                        "oidc_client_secret": "x", "session_secret": "s" * 40}.items():
        monkeypatch.setattr(s, name, value)
    issuer_problem = any("OIDC_ISSUER" in p for p in auth.oidc_problems())
    assert issuer_problem is not allowed


def _meta_transport(meta: dict, seen: list):
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=meta)
    return httpx.MockTransport(handler)


def test_discovery_url_is_used_but_issuer_must_still_match():
    """Docker: uygulama Keycloak'a ic adresten ulasir; belge yine tarayicinin gordugu issuer'i tasimali."""
    import asyncio

    public, internal = "http://localhost:8080/realms/mandate", "http://keycloak:8080/realms/mandate"
    meta = {"issuer": public, "authorization_endpoint": f"{public}/auth",
            "token_endpoint": f"{internal}/token", "jwks_uri": f"{internal}/certs"}
    seen = []
    p = auth.OIDCProvider(public, _meta_transport(meta, seen), f"{internal}/.well-known/openid-configuration")
    assert asyncio.run(p.metadata())["token_endpoint"] == f"{internal}/token"
    assert seen == [f"{internal}/.well-known/openid-configuration"]

    # ic adresteki belge baska bir issuer iddia ediyorsa reddedilir
    spoof = auth.OIDCProvider(public, _meta_transport({**meta, "issuer": internal}, []), f"{internal}/.well-known/openid-configuration")
    with pytest.raises(auth.AuthError, match="issuer"):
        asyncio.run(spoof.metadata())


def test_https_issuer_never_accepts_http_endpoints():
    import asyncio

    meta = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": "http://idp.acme.test/token", "jwks_uri": f"{ISSUER}/keys"}
    p = auth.OIDCProvider(ISSUER, _meta_transport(meta, []))
    with pytest.raises(auth.AuthError, match="token_endpoint"):
        asyncio.run(p.metadata())


def test_badge_counts_only_what_this_person_can_decide(app_, idp, finance):
    """Rozet herkesin bekleyen islemlerini degil, kisinin karar verebileceklerini sayar."""
    ilker = login(app_, idp, "ilker")
    ask(ilker)  # ilker'in kendi acdigi IT talebi: kendisi onaylayamaz
    finance("Ar-Ge", 120000)
    ask(ilker, "Ar-Ge icin 120.000 TL harcama onayla")  # yalnizca CFO
    s = ilker.get("/system/status").json()
    assert s["pending_approvals"] == len(ilker.get("/approvals").json())
    assert s["pending_approvals_total"] >= 2
    cem = login(app_, idp, "cem")
    assert cem.get("/system/status").json()["pending_approvals"] == len(cem.get("/approvals").json()) >= 1
    assert login(app_, idp, "ali").get("/system/status").json()["pending_approvals"] == 0


def test_logout_also_ends_idp_session_so_another_user_can_sign_in(app_, idp):
    """Cikis IdP oturumunu da kapatmazsa 'Giris yap' ayni hesabi sifre sormadan acar."""
    c = login(app_, idp, "ilker")
    r = c.post("/auth/logout")
    assert r.status_code == 200
    target = urlparse(r.json()["redirect"])
    q = {k: v[0] for k, v in parse_qs(target.query).items()}
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{ISSUER}/logout"
    assert q["client_id"] == CLIENT_ID and q["post_logout_redirect_uri"] == f"{BASE}/"
    # ipucu, bu oturumun IdP'den aldigi gercek id_token'dir
    hint = jwt.decode(q["id_token_hint"], options={"verify_signature": False})
    assert hint["email"] == "ilker@acme.com" and hint["aud"] == CLIENT_ID


def test_logout_without_idp_logout_endpoint_is_local_only(app_, idp, monkeypatch):
    original = idp.handler

    def no_end_session(request):
        res = original(request)
        if str(request.url).endswith("/.well-known/openid-configuration"):
            meta = res.json()
            meta.pop("end_session_endpoint")
            return httpx.Response(200, json=meta)
        return res

    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(no_end_session))
    monkeypatch.setattr(auth, "_provider", None)
    c = login(app_, idp, "ali")
    assert c.post("/auth/logout").json() == {"redirect": "/"}
