"""Qdrant tabanli kurumsal hafiza: ingestion + semantik retrieval.

Embedding icin FastEmbed kullanilir (yerel ONNX, ek API anahtari gerekmez).
QDRANT_URL verilmezse yerel dosya tabanli Qdrant ile calisir -> demo tek komutla kalkar.
"""
from __future__ import annotations

import atexit
import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastembed import TextEmbedding
from qdrant_client import QdrantClient, models

from app.config import get_settings
from app.schemas import RetrievedChunk

settings = get_settings()
_client: QdrantClient | None = None
_embedder: TextEmbedding | None = None

# e5 ailesi asimetrik egitildigi icin sorgu/pasaj onekleri ister; digerleri istemez.
_E5_PREFIXES = {"query": "query: ", "passage": "passage: "}


def get_embedder() -> TextEmbedding:
    """FastEmbed yerel ONNX modeli - ek bir embedding API anahtari gerektirmez."""
    global _embedder
    if _embedder is None:
        _embedder = TextEmbedding(model_name=settings.embedding_model)
    return _embedder


def _prefix(kind: str) -> str:
    return _E5_PREFIXES[kind] if "e5" in settings.embedding_model.lower() else ""


def embed_documents(texts: list[str]) -> list[list[float]]:
    prefix = _prefix("passage")
    return [v.tolist() for v in get_embedder().embed([prefix + t for t in texts])]


def embed_query(text: str) -> list[float]:
    prefix = _prefix("query")
    return next(iter(get_embedder().query_embed(prefix + text))).tolist()


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        if settings.qdrant_url:
            _client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        else:
            Path(settings.qdrant_path).mkdir(parents=True, exist_ok=True)
            _client = QdrantClient(path=settings.qdrant_path)
        _ensure_collection(_client)
    return _client


def _vector_size() -> int:
    for m in TextEmbedding.list_supported_models():
        if m["model"] == settings.embedding_model:
            return int(m["dim"])
    # Model listede yoksa bir kez embed edip boyutu olc.
    return len(embed_query("boyut olcumu"))


def _ensure_collection(client: QdrantClient) -> None:
    if client.collection_exists(settings.qdrant_collection):
        return
    client.create_collection(
        collection_name=settings.qdrant_collection,
        vectors_config=models.VectorParams(
            size=_vector_size(), distance=models.Distance.COSINE
        ),
    )


# --------------------------------------------------------------------------
# Metin cikarma
# --------------------------------------------------------------------------
def extract_text(data: bytes, filename: str) -> str:
    """PDF veya duz metin dosyasindan ham metni cikarir."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------
@dataclass
class Chunk:
    text: str
    index: int


def _tail_overlap(text: str, overlap: int) -> str:
    """Chunk sonundan overlap kadar metni, kelime ortasindan kesmeden alir."""
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1:] if space != -1 else tail


def chunk_text(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    """Madde sinirlarina saygi duyan, ust uste binen (overlap) chunking.

    Kurumsal kural dokumanlarinda bir maddenin ortadan bolunmesi karari bozdugu
    icin once "2.1." gibi numarali madde basliklarindan, sonra paragraflardan
    ayirip hedef boyuta kadar birlestiriyoruz.
    """
    size = chunk_size or settings.chunk_size
    lap = overlap if overlap is not None else settings.chunk_overlap

    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []

    # Numarali madde basligi (satir basinda "1." / "2.3." / "4.6.") yeni blok baslatir.
    blocks = re.split(r"\n(?=\s*\d+(?:\.\d+)*\.\s)", text)
    units: list[str] = []
    for block in blocks:
        units.extend(p.strip() for p in re.split(r"\n\s*\n", block) if p.strip())

    chunks: list[str] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            chunks.append(buffer.strip())
            buffer = _tail_overlap(buffer.strip(), lap)

    for unit in units:
        # Tek basina cok uzun birim -> cumle bazinda kir
        if len(unit) > size:
            flush()
            for sentence in re.split(r"(?<=[.!?])\s+", unit):
                if len(buffer) + len(sentence) + 1 > size and buffer:
                    flush()
                buffer = f"{buffer} {sentence}".strip()
            continue

        if len(buffer) + len(unit) + 2 > size and buffer:
            flush()
        buffer = f"{buffer}\n\n{unit}".strip()

    if buffer.strip():
        chunks.append(buffer.strip())

    # Overlap yuzunden olusabilecek birebir tekrarlari at
    seen: set[str] = set()
    unique = [c for c in chunks if not (c in seen or seen.add(c))]
    return [Chunk(text=c, index=i) for i, c in enumerate(unique)]


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
def ingest_document(data: bytes, filename: str) -> dict:
    """Bir dokumani parcalayip vektor veritabanina yazar.

    Ayni dosya tekrar yuklenirse ayni id'ler uretildigi icin kayit uzerine yazilir
    (upsert) - kopya chunk olusmaz.
    """
    text = extract_text(data, filename)
    chunks = chunk_text(text)
    if not chunks:
        return {"filename": filename, "chunks": 0, "characters": 0}

    namespace = uuid.UUID(hashlib.md5(filename.encode()).hexdigest())
    vectors = embed_documents([c.text for c in chunks])

    points = [
        models.PointStruct(
            id=str(uuid.uuid5(namespace, str(c.index))),
            vector=vector,
            payload={"text": c.text, "source": filename, "chunk_index": c.index},
        )
        for c, vector in zip(chunks, vectors)
    ]
    get_client().upsert(collection_name=settings.qdrant_collection, points=points)
    return {"filename": filename, "chunks": len(chunks), "characters": len(text)}


def ingest_path(path: str | Path) -> dict:
    p = Path(path)
    return ingest_document(p.read_bytes(), p.name)


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------
def retrieve(query: str, top_k: int | None = None) -> list[RetrievedChunk]:
    """Talebi semantik olarak tarayip ilgili kurumsal kurallari dondurur."""
    k = top_k or settings.rag_top_k
    try:
        hits = get_client().query_points(
            collection_name=settings.qdrant_collection,
            query=embed_query(query),
            limit=k,
            with_payload=True,
        ).points
    except Exception:
        return []
    return [
        RetrievedChunk(
            text=(h.payload or {}).get("text", ""),
            source=(h.payload or {}).get("source", "bilinmiyor"),
            score=float(h.score),
        )
        for h in hits
    ]


def build_context_block(chunks: list[RetrievedChunk]) -> str:
    """Retrieval sonucunu prompta gomulecek, kaynakli tek bir metne cevirir."""
    if not chunks:
        return (
            "(Kurumsal hafizada bu talebe dair kural bulunamadi. "
            "Dayanak yoksa karar RED olmalidir.)"
        )
    return "\n\n".join(
        f"[Kural {i + 1} | kaynak: {c.source} | benzerlik: {c.score:.3f}]\n{c.text}"
        for i, c in enumerate(chunks)
    )


def collection_stats() -> dict:
    client = get_client()
    try:
        info = client.get_collection(settings.qdrant_collection)
        return {"collection": settings.qdrant_collection, "points": info.points_count or 0}
    except Exception:
        return {"collection": settings.qdrant_collection, "points": 0}


def reset_collection() -> None:
    client = get_client()
    if client.collection_exists(settings.qdrant_collection):
        client.delete_collection(settings.qdrant_collection)
    _ensure_collection(client)


def close_client() -> None:
    """Yerel Qdrant dosya kilidini duzgun birakir."""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None


atexit.register(close_client)
