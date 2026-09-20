"""FastAPI uygulamasi: kurumsal hafiza yonetimi + otonom karar/odeme ucu."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.agent.graph import process_request
from app.chain.wallet import wallet_status
from app.config import get_settings
from app.db import init_db, list_operations, spent_last_24h
from app.rag.store import collection_stats, ingest_document, reset_collection, retrieve
from app.schemas import OperationResponse, RequestPayload

settings = get_settings()
ALLOWED_SUFFIXES = {".pdf", ".txt", ".md"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Otonom Kurumsal Finans Ajani",
    description="RAG tabanli kurumsal hafiza + otonom karar motoru + Solana odeme katmani",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "model": settings.model_id,
        "memory": collection_stats(),
        "dry_run": settings.dry_run,
    }


# --------------------------------------------------------------------------
# Modul 1 - Kurumsal hafiza
# --------------------------------------------------------------------------
@app.post("/memory/upload")
async def upload_rules(files: list[UploadFile] = File(...)) -> dict:
    """Sirket kurallarini (PDF/TXT/MD) vektor veritabanina isler."""
    results = []
    for f in files:
        suffix = "." + (f.filename or "").rsplit(".", 1)[-1].lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(400, f"Desteklenmeyen dosya turu: {f.filename}")
        data = await f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Dosya cok buyuk: {f.filename}")
        results.append(ingest_document(data, f.filename or "belge"))
    return {"ingested": results, "memory": collection_stats()}


@app.get("/memory/stats")
async def memory_stats() -> dict:
    return collection_stats()


@app.get("/memory/search")
async def memory_search(q: str, k: int = 5) -> dict:
    """Retrieval katmanini tek basina test etmek icin."""
    return {"query": q, "results": [c.model_dump() for c in retrieve(q, k)]}


@app.delete("/memory")
async def clear_memory() -> dict:
    reset_collection()
    return {"cleared": True, "memory": collection_stats()}


# --------------------------------------------------------------------------
# Modul 2 + 3 - Otonom karar ve odeme
# --------------------------------------------------------------------------
@app.post("/agent/request", response_model=OperationResponse)
async def agent_request(payload: RequestPayload) -> OperationResponse:
    """Serbest metin talebi al, karar ver, onayliysa Solana'da ode, logla."""
    return await process_request(payload.text, payload.requester)


@app.get("/operations")
async def operations(limit: int = 50) -> dict:
    rows = list_operations(limit)
    return {
        "count": len(rows),
        "spent_24h_usdc": spent_last_24h("USDC"),
        "items": [
            {
                "operation_id": r.id,
                "created_at": r.created_at,
                "requester": r.requester,
                "request_text": r.request_text,
                "decision": r.decision,
                "reasoning": r.reasoning,
                "cited_rules": r.cited_rules,
                "confidence": r.confidence,
                "injection_detected": r.injection_detected,
                "policy_passed": r.policy_passed,
                "policy_violations": r.policy_violations,
                "recipient_wallet": r.recipient_wallet,
                "amount": r.amount,
                "currency": r.currency,
                "tx_hash": r.tx_hash,
                "tx_confirmed": r.tx_confirmed,
                "tx_simulated": r.tx_simulated,
                "tx_error": r.tx_error,
            }
            for r in rows
        ],
    }


@app.get("/wallet")
async def wallet() -> dict:
    """Ajanin operasyon cuzdani ozeti. Gizli anahtar hicbir kosulda donmez."""
    status = await wallet_status()
    status["limits"] = {
        "max_single_payment_usdc": settings.max_single_payment_usdc,
        "max_daily_payment_usdc": settings.max_daily_payment_usdc,
        "spent_24h_usdc": spent_last_24h("USDC"),
        "allowlisted_wallets": len(settings.allowlist),
    }
    return status
