"""FastAPI: Mandate - coklu ajan agi + insan onayi + kurumsal hafiza.

UC YETKI MATRISI (hava boslugu)
-------------------------------
  ACIK (kimliksiz)       GET /health (yalnizca "ok"), POST /public/support, panel dosyalari
  IC (X-Source-Key)      /agent/request*, /runs*, /system/status, /memory/stats, /memory/search
  YONETICI (X-Admin-Key) /approvals*, /memory/upload, DELETE /memory
Dis dunyaya acik TEK is ucu /public/support'tur; yalnizca musteri destek ajanina ulasir ve
yalnizca musteriye uygun, dar bir yanit doner.
"""
from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.graph import build_graph
from app.agent.customer_support import HANDOFF_MESSAGE
from app.agent.service import AgentService, RunConflict, RunNotFound, get_service, set_service
from app.config import BASE_DIR, DATA_DIR, get_settings
from app.db import init_db
from app.security import configured_internal_sources, require_internal, resolve_declared_source
from app.llm import llm_info, routing_table
from app.rag.store import DOMAINS, collection_stats, extract_text, ingest_document, reset_collection, retrieve
from app.schemas import (
    ApprovalDecisionPayload, PublicSupportRequest, PublicSupportResponse, RequestPayload, RunView, Source,
)
from app.tools.finance_db import ensure_finance_db
from app.tools.sales_db import ensure_sales_db

settings = get_settings()
WEB_DIR = BASE_DIR / "app" / "web"
# Paneldeki "ornek talep" kartlari icin indirilebilir ornek dosyalar (BEYAZ LISTE).
SAMPLE_FILES = {
    "ornek_sozlesme_riskli.txt", "ornek_sozlesme_temiz.txt",
    "ornek_teklif_uygun.txt", "ornek_teklif_riskli.txt",
}
MEMORY_SUFFIXES = {".pdf", ".txt", ".md"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    await asyncio.to_thread(ensure_sales_db, settings.sales_db_path)
    await asyncio.to_thread(ensure_finance_db, settings.finance_db_path)
    # Kalici checkpoint: bekleyen onaylar sunucu yeniden baslasa da kaybolmaz.
    async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_db_path) as saver:
        set_service(AgentService(build_graph(saver)))
        try:
            yield
        finally:
            set_service(None)


app = FastAPI(
    title="Mandate",
    description="Cok ajanli kurumsal is akisi: IT/Ops, Veri, Sozlesme, Finans, Satin Alma, Musteri Destek + insan onayi",
    version="2.0.0",
    lifespan=lifespan,
)
# Panel ayni kokenden sunulur; CORS varsayilan olarak KAPALI. Ayri bir alan adindan
# erisim gerekiyorsa CORS_ORIGINS ile acikca izin verilir ("*" kullanilmaz: admin
# anahtari tasiyan istekler her siteye acik olmamali).
_cors = [o.strip() for o in settings.cors_origins.split(",") if o.strip() and o.strip() != "*"]
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "X-Admin-Key", "X-Source-Key"],
    )


MIN_ADMIN_KEY_LENGTH = 16


def admin_auth_state() -> str:
    """'enabled' | 'missing' | 'weak'. Yalnizca 'enabled' iken onay uclari calisir."""
    key = (settings.admin_api_key or "").strip()
    if not key:
        return "missing"
    if len(key) < MIN_ADMIN_KEY_LENGTH:
        return "weak"
    return "enabled"


def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """Insan onayi ve yonetim uclarini korur. FAIL-CLOSED:

    * ADMIN_API_KEY tanimli degil / zayif -> 503: uclar hic calismaz (acik birakilmaz).
    * X-Admin-Key basligi yok             -> 401
    * X-Admin-Key yanlis                  -> 403
    Karsilastirma sabit zamanlidir (timing saldirisina karsi).
    """
    state = admin_auth_state()
    if state != "enabled":
        raise HTTPException(
            503,
            "Onay uclari devre disi: ADMIN_API_KEY tanimli degil veya en az "
            f"{MIN_ADMIN_KEY_LENGTH} karakter degil.",
        )
    if not x_admin_key:
        raise HTTPException(401, "X-Admin-Key basligi gerekli.", headers={"WWW-Authenticate": "X-Admin-Key"})
    if not secrets.compare_digest(x_admin_key.encode(), settings.admin_api_key.strip().encode()):
        raise HTTPException(403, "Gecersiz admin anahtari.")


def _domain(domain: str) -> str:
    if domain not in DOMAINS:
        raise HTTPException(400, f"Gecersiz domain. Gecerli: {', '.join(DOMAINS)}")
    return domain


def _respond(view: RunView, response: Response) -> RunView:
    if view.status == "awaiting_approval":
        response.status_code = 202  # kabul edildi, insan karari bekleniyor
    return view


# --------------------------------------------------------------------------
# Saglik
# --------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    """Herkese acik canlilik kontrolu. Yapilandirma veya sayi SIZDIRMAZ."""
    return {"status": "ok"}


@app.get("/system/status")
async def system_status(source: str = Depends(require_internal)) -> dict:
    """Panelin yan cubugu icin ayrintili durum (yalnizca ic)."""
    return {
        "status": "ok",
        "caller_source": source,
        "llm": llm_info(),
        "llm_routing": routing_table(),
        "memory": await asyncio.to_thread(collection_stats),
        "admin_auth": admin_auth_state(),
        "internal_sources": configured_internal_sources(),
        "pending_approvals": len(get_service().list("awaiting_approval", 500)),
    }


# --------------------------------------------------------------------------
# Dis kanal: musteriler (kimliksiz). YALNIZCA musteri destek ajani.
# --------------------------------------------------------------------------
@app.post("/public/support", response_model=PublicSupportResponse)
async def public_support(payload: PublicSupportRequest) -> PublicSupportResponse:
    """Musteri mesaji. Kaynak her zaman DIS bir kaynaktir (sema bunu zorlar); ic ajanlara,
    SQL'e ve ic belgelere ulasan bir yol yoktur. Yanit bilincli olarak dardir."""
    view = await get_service().submit(payload.message, payload.contact or "anonim-musteri", source=payload.source)
    answered = view.status == "completed" and view.route == "customer_support" and view.answer
    return PublicSupportResponse(
        reference=view.run_id,
        reply=view.answer if answered else HANDOFF_MESSAGE,
        handed_off=not answered,
    )


# --------------------------------------------------------------------------
# Kurumsal hafiza
# --------------------------------------------------------------------------
@app.post("/memory/upload", dependencies=[Depends(require_admin)])
async def upload_documents(
    files: list[UploadFile] = File(...),
    domain: str = Query(description="it_hr, red_lines, procurement (ic) veya public_faq (MUSTERIYE ACIK)"),
) -> dict:
    """Yonetici: bilgi tabanina yazmak ajanlarin davranisini degistirir (zehirleme riski)."""
    _domain(domain)
    results = []
    for f in files:
        suffix = "." + (f.filename or "").rsplit(".", 1)[-1].lower()
        if suffix not in MEMORY_SUFFIXES:
            raise HTTPException(400, f"Desteklenmeyen dosya turu: {f.filename}")
        data = await f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Dosya cok buyuk: {f.filename}")
        results.append(await asyncio.to_thread(ingest_document, data, f.filename or "belge", domain))
    return {"ingested": results, "memory": await asyncio.to_thread(collection_stats)}


@app.get("/memory/stats", dependencies=[Depends(require_internal)])
async def memory_stats() -> dict:
    return await asyncio.to_thread(collection_stats)


@app.get("/memory/search", dependencies=[Depends(require_internal)])
async def memory_search(q: str, domain: str, k: int = 5) -> dict:
    _domain(domain)
    chunks = await asyncio.to_thread(retrieve, q, domain, k)
    return {"query": q, "domain": domain, "results": [c.model_dump() for c in chunks]}


@app.delete("/memory", dependencies=[Depends(require_admin)])
async def clear_memory(domain: str | None = None) -> dict:
    if domain is not None:
        _domain(domain)
    await asyncio.to_thread(reset_collection, domain)
    return {"cleared": domain or "all", "memory": await asyncio.to_thread(collection_stats)}


# --------------------------------------------------------------------------
# Ajan agi (IC)
# --------------------------------------------------------------------------
@app.post("/agent/request", response_model=RunView)
async def agent_request(
    payload: RequestPayload, response: Response, caller: str = Depends(require_internal)
) -> RunView:
    """Ic talep. Kaynak X-Source-Key'den gelir; govdedeki `source` yalnizca dogrulanir.
    Kritik eylemde 202 + awaiting_approval doner."""
    source = resolve_declared_source(caller, payload.source)
    view = await get_service().submit(payload.text, payload.requester, source=source)
    return _respond(view, response)


@app.post("/agent/request/upload", response_model=RunView)
async def agent_request_with_file(
    response: Response,
    text: str = Form(min_length=3, max_length=4000),
    requester: str = Form(default="anonymous", max_length=120),
    source: Source | None = Form(default=None),
    attachment: UploadFile = File(description="Sozlesme veya tedarikci teklifi (PDF/TXT/MD)"),
    caller: str = Depends(require_internal),
) -> RunView:
    """Ek dosyali ic talep (orn. sozlesme / teklif incelemesi)."""
    resolved = resolve_declared_source(caller, source)
    name = attachment.filename or "ek"
    if "." + name.rsplit(".", 1)[-1].lower() not in MEMORY_SUFFIXES:
        raise HTTPException(400, f"Desteklenmeyen dosya turu: {name}")
    data = await attachment.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Dosya cok buyuk: {name}")

    content = await asyncio.to_thread(extract_text, data, name)
    if not content.strip():
        raise HTTPException(422, "Dosyadan metin cikarilamadi (taranmis/gorsel PDF olabilir).")
    if len(content) > settings.contract_max_chars:
        raise HTTPException(
            413, f"Metin cok uzun ({len(content)} karakter; sinir {settings.contract_max_chars})."
        )
    view = await get_service().submit(
        text, requester, attachment_name=name, attachment_text=content, source=resolved
    )
    return _respond(view, response)


# --------------------------------------------------------------------------
# Kayitlar (IC) ve insan onayi (YONETICI)
# --------------------------------------------------------------------------
@app.get("/runs", response_model=list[RunView], dependencies=[Depends(require_internal)])
async def runs(status: str | None = None, limit: int = Query(default=50, le=500)) -> list[RunView]:
    return get_service().list(status, limit)


@app.get("/runs/{run_id}", response_model=RunView, dependencies=[Depends(require_internal)])
async def run_detail(run_id: str) -> RunView:
    try:
        return get_service().view(run_id)
    except RunNotFound:
        raise HTTPException(404, "Talep bulunamadi.")


@app.get("/approvals", response_model=list[RunView], dependencies=[Depends(require_admin)])
async def pending_approvals() -> list[RunView]:
    """Insan karari bekleyen talepler (en yeni ustte)."""
    return get_service().list("awaiting_approval", 200)


@app.post("/approvals/{run_id}/decision", response_model=RunView, dependencies=[Depends(require_admin)])
async def decide(run_id: str, payload: ApprovalDecisionPayload) -> RunView:
    """Bekleyen eylemi onayla / reddet. Onayda eylem burada yurutulur."""
    try:
        return await get_service().decide(run_id, payload.approved, payload.reviewer, payload.comment)
    except RunNotFound:
        raise HTTPException(404, "Talep bulunamadi.")
    except RunConflict as exc:
        raise HTTPException(409, str(exc))


# --------------------------------------------------------------------------
# Web paneli
# --------------------------------------------------------------------------
@app.get("/samples/{name}", include_in_schema=False)
async def sample_file(name: str) -> FileResponse:
    """Yalnizca beyaz listedeki ornek dosyalar; yol gecisi (../) mumkun degil."""
    if name not in SAMPLE_FILES:
        raise HTTPException(404, "Ornek dosya bulunamadi.")
    return FileResponse(DATA_DIR / name, media_type="text/plain; charset=utf-8")


# EN SONA: API rotalarindan sonra baglanir, boylece /health vb. golgelenmez.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
