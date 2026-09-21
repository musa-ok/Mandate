"""IT & Ops ajaninin araclari (SAHTE / mock).

Gercek bir kimlik sistemine (Okta, Entra ID, Google Admin) baglanmaz; imzalar ve
davranis gercekle ayni tutuldu, boylece mock'u gercek istemciyle degistirmek
yalnizca bu dosyada olur.

Kritik nokta: araclari LLM CALISTIRMAZ. LLM yalnizca bir cagri ONERIR; cagriyi
`execute_tool` insan onayindan sonra graf calistirir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.llm.base import ToolSpec

# Mock "yazilim katalogu". Gercekte IT varlik envanterinden gelir.
SOFTWARE_CATALOG = {
    "jira": "Jira",
    "github": "GitHub",
    "figma": "Figma",
    "salesforce": "Salesforce",
    "slack": "Slack",
    "google workspace": "Google Workspace",
    "tableau": "Tableau",
    "notion": "Notion",
}

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class ToolArgError(ValueError):
    """Arac argumani gecersiz. Insan onayina SUNULMADAN once yakalanir."""


# --------------------------------------------------------------------------
# Mock fonksiyonlar (istenen imzalarla)
# --------------------------------------------------------------------------
def reset_password(email: str) -> dict[str, Any]:
    """Kullanicinin sifresini sifirlar (MOCK). Gercek sifre asla donmez."""
    return {
        "ok": True,
        "mock": True,
        "email": email,
        "message": f"{email} icin sifre sifirlama baglantisi gonderildi (SAHTE islem).",
    }


def grant_access(software: str, user: str) -> dict[str, Any]:
    """Kullaniciya bir yazilima erisim verir (MOCK)."""
    return {
        "ok": True,
        "mock": True,
        "software": software,
        "user": user,
        "message": f"{user} kullanicisina {software} erisimi verildi (SAHTE islem).",
    }


# --------------------------------------------------------------------------
# Arac kaydi
# --------------------------------------------------------------------------
class ResetPasswordArgs(BaseModel):
    email: str = Field(description="Sifresi sifirlanacak hesabin e-posta adresi")


class GrantAccessArgs(BaseModel):
    software: str = Field(description="Erisim verilecek yazilim adi (orn. Jira, GitHub, Figma)")
    user: str = Field(description="Erisim verilecek kullanicinin e-posta adresi")


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[..., dict[str, Any]]
    title: Callable[[dict[str, Any]], str]  # onay kartinda gorunen insan-okunur baslik


TOOLS: dict[str, ToolDef] = {
    "reset_password": ToolDef(
        name="reset_password",
        description="Bir kullanicinin hesap sifresini sifirlar. Kritik eylem: insan onayi gerektirir.",
        args_model=ResetPasswordArgs,
        fn=reset_password,
        title=lambda a: f"Sifre sifirlama: {a['email']}",
    ),
    "grant_access": ToolDef(
        name="grant_access",
        description="Bir kullaniciya bir yazilima erisim verir. Kritik eylem: insan onayi gerektirir.",
        args_model=GrantAccessArgs,
        fn=grant_access,
        title=lambda a: f"Erisim yetkisi: {a['software']} -> {a['user']}",
    ),
}


def tool_specs() -> list[ToolSpec]:
    """Araclari LLM'e tanitilacak JSON Schema bicimine cevirir."""
    return [
        ToolSpec(t.name, t.description, _clean_schema(t.args_model.model_json_schema()))
        for t in TOOLS.values()
    ]


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic'in 'title' gurultusunu atar; modele giden sema sade kalsin."""
    schema = {k: v for k, v in schema.items() if k != "title"}
    schema["properties"] = {
        name: {k: v for k, v in prop.items() if k != "title"}
        for name, prop in schema.get("properties", {}).items()
    }
    return schema


def validate_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Arac cagrisini KOD ile dogrular ve normallestirir.

    LLM'in urettigi argumanlara guvenilmez: bos, uydurma veya bicimsiz olabilir
    (ozellikle kucuk yerel modellerde). Bir insan gecersiz bir eylemi onaylamasin diye
    dogrulama, onay istenmeden ONCE yapilir.
    """
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolArgError(f"Bilinmeyen arac: {name!r}")
    try:
        args = tool.args_model.model_validate(arguments).model_dump()
    except Exception as exc:  # noqa: BLE001
        raise ToolArgError(f"{name} icin eksik/gecersiz arguman: {exc}") from exc

    def _email(value: str, label: str) -> str:
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ToolArgError(f"{label} gecerli bir e-posta adresi degil: {value!r}")
        return value

    if name == "reset_password":
        args["email"] = _email(args["email"], "email")
    elif name == "grant_access":
        args["user"] = _email(args["user"], "user")
        canonical = SOFTWARE_CATALOG.get(args["software"].strip().lower())
        if canonical is None:
            known = ", ".join(SOFTWARE_CATALOG.values())
            raise ToolArgError(f"{args['software']!r} yazilim katalogunda yok. Bilinenler: {known}")
        args["software"] = canonical
    return args


def ungrounded_args(name: str, args: dict[str, Any], request_text: str, requester: str) -> list[str]:
    """Talepte DAYANAGI olmayan argumanlari dondurur (uydurma eylem tespiti).

    Model bir bilgi sorusunu yanlislikla eyleme cevirebilir (canli testte: "izin hakkim
    kac gun?" -> grant_access(Slack)). Kural: her arguman talep metninde gecmeli;
    e-posta alanlari icin talep sahibinin kendi adresi de gecerli dayanaktir
    (kimlik, modelden degil sistemden gelir).
    """
    text = (request_text or "").casefold()
    requester = (requester or "").strip().casefold()
    missing = []
    for key, value in args.items():
        v = str(value).casefold()
        if key in ("email", "user"):
            if v not in text and v != requester:
                missing.append(f"{key}={value}")
        elif key == "software":
            aliases = {v} | {k for k, canon in SOFTWARE_CATALOG.items() if canon.casefold() == v}
            if not any(a in text for a in aliases):
                missing.append(f"{key}={value}")
        elif v not in text:
            missing.append(f"{key}={value}")
    return missing


# --------------------------------------------------------------------------
# Yurutme
# --------------------------------------------------------------------------
_EXECUTED: dict[str, dict[str, Any]] = {}  # idempotency_key -> sonuc (mock)


async def execute_tool(name: str, arguments: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    """Onaylanmis araci calistirir.

    `idempotency_key`: graf yeniden denenirse ayni eylem IKI KEZ uygulanmasin diye.
    Gercek bir kimlik sisteminde bu anahtar istege eklenir; mock burada onbellekler.
    """
    if idempotency_key in _EXECUTED:
        return _EXECUTED[idempotency_key] | {"replayed": True}
    args = validate_call(name, arguments)  # savunma: yurutme aninda da dogrula
    result = TOOLS[name].fn(**args)
    _EXECUTED[idempotency_key] = result
    return result
