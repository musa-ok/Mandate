"""Qdrant tabanli kurumsal hafiza: ingestion + semantik retrieval.

Ic koleksiyonda (corporate_memory) uc BILGI ALANI (domain), payload filtresiyle ayrilir:
  * it_hr     -> IT / Ik politikalari      (Agent A okur)
  * red_lines -> Sozlesme "kirmizi cizgileri" (Agent C okur)
  * procurement -> Satin alma sartnameleri (Agent E okur)
Ve AYRI bir koleksiyonda dis alan:
  * public_faq -> Musteriye acik SSS / iade politikasi (yalnizca Musteri Destek okur)
Ajanlar yalnizca kendi alanini gorur; bir ajanin baska alanin verisine
erismesi retrieval katmaninda yapisal olarak engellenir.

Embedding icin FastEmbed kullanilir (yerel ONNX, ek API anahtari gerekmez).
QDRANT_URL verilmezse yerel dosya tabanli Qdrant ile calisir -> demo tek komutla kalkar.
"""
from __future__ import annotations

import atexit
import hashlib
import re
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from fastembed import TextEmbedding
from qdrant_client import QdrantClient, models

from app.config import get_settings
from app.schemas import RetrievedChunk

settings = get_settings()
_client: QdrantClient | None = None
_embedder: TextEmbedding | None = None

# Ic alanlar: tek koleksiyonda (corporate_memory), payload filtresiyle ayrilir.
INTERNAL_DOMAINS = ("it_hr", "red_lines", "procurement")
# Dis alan: FIZIKSEL OLARAK AYRI koleksiyon (public_faq). Musteri destek ajani YALNIZCA
# buraya erisir. Ayri koleksiyon sayesinde bir filtre hatasi ic belgeleri musteriye
# gosteremez - sorgu ic koleksiyona hic dokunmaz.
PUBLIC_DOMAIN = "public_faq"
DOMAINS = INTERNAL_DOMAINS + (PUBLIC_DOMAIN,)


def collection_for(domain: str) -> str:
    return settings.public_faq_collection if domain == PUBLIC_DOMAIN else settings.qdrant_collection


# Alan basina parcalama: kirmizi cizgiler birbirinden bagimsiz, kisa KURALLARDIR;
# her biri kendi parcasi olmali ki arama tek bir kurala isabet etsin.
# (chunk_size, overlap) - None = genel ayar
DOMAIN_CHUNKING: dict[str, tuple[int | None, int | None]] = {
    "it_hr": (None, None),
    "red_lines": (450, 0),
    "procurement": (450, 0),  # sartname maddeleri de bagimsiz kurallardir
    "public_faq": (None, None),  # soru-cevap bazli bolunur: bkz. split_faq
}

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


def _ensure_collection(client: QdrantClient, name: str | None = None) -> None:
    """Verilen (ya da her iki) koleksiyonu yoksa olusturur."""
    names = [name] if name else [settings.qdrant_collection, settings.public_faq_collection]
    for n in names:
        if client.collection_exists(n):
            continue
        client.create_collection(
            collection_name=n,
            vectors_config=models.VectorParams(size=_vector_size(), distance=models.Distance.COSINE),
        )
        try:  # sunucu modunda filtre hizi icin; yerel modda etkisiz (uyari bastirilir)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                client.create_payload_index(n, "domain", models.PayloadSchemaType.KEYWORD)
        except Exception:  # noqa: BLE001
            pass


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


_FAQ_ITEM = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)


def split_faq(text: str) -> list[Chunk]:
    """SSS belgesini SORU-CEVAP basina bir parca olacak sekilde boler.

    Her parca tek bir soruyu ve cevabini tasir; boylece arama tek bir cevaba isabet
    eder ve ajanin "hangi parcayi kullandim" beyani anlamli olur. Numarali madde
    yoksa genel parcalamaya duser.
    """
    starts = [m.start() for m in _FAQ_ITEM.finditer(text)]
    if len(starts) < 2:
        return chunk_text(text, chunk_size=600, overlap=0)
    items = [text[a:b].strip() for a, b in zip(starts, starts[1:] + [len(text)])]
    return [Chunk(text=re.sub(r"\s*\n\s*", " ", it), index=i) for i, it in enumerate(items) if it]


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
def _check_domain(domain: str) -> str:
    if domain not in DOMAINS:
        raise ValueError(f"gecersiz domain: {domain!r} (gecerli: {', '.join(DOMAINS)})")
    return domain


def _domain_filter(domain: str) -> models.Filter:
    return models.Filter(
        must=[models.FieldCondition(key="domain", match=models.MatchValue(value=domain))]
    )


def ingest_document(data: bytes, filename: str, domain: str) -> dict:
    """Bir dokumani parcalayip vektor veritabanina yazar.

    Ayni (domain, dosya adi) tekrar yuklenirse ayni id'ler uretildigi icin kayit
    uzerine yazilir (upsert) - kopya chunk olusmaz.
    """
    _check_domain(domain)
    text = extract_text(data, filename)
    if domain == PUBLIC_DOMAIN:
        chunks = split_faq(text)
    else:
        size, overlap = DOMAIN_CHUNKING[domain]
        chunks = chunk_text(text, chunk_size=size, overlap=overlap)
    if not chunks:
        return {"filename": filename, "domain": domain, "chunks": 0, "characters": 0}

    namespace = uuid.UUID(hashlib.md5(f"{domain}:{filename}".encode()).hexdigest())
    vectors = embed_documents([c.text for c in chunks])

    points = [
        models.PointStruct(
            id=str(uuid.uuid5(namespace, str(c.index))),
            vector=vector,
            payload={
                "text": c.text,
                "source": filename,
                "chunk_index": c.index,
                "domain": domain,
            },
        )
        for c, vector in zip(chunks, vectors)
    ]
    get_client().upsert(collection_name=collection_for(domain), points=points)
    return {"filename": filename, "domain": domain, "chunks": len(chunks), "characters": len(text)}


def ingest_path(path: str | Path, domain: str) -> dict:
    p = Path(path)
    return ingest_document(p.read_bytes(), p.name, domain)


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------
def retrieve(query: str, domain: str, top_k: int | None = None) -> list[RetrievedChunk]:
    """Sorguyu semantik olarak tarar; YALNIZCA istenen alandaki parcalari dondurur."""
    _check_domain(domain)
    k = top_k or settings.rag_top_k
    try:
        hits = get_client().query_points(
            collection_name=collection_for(domain),
            query=embed_query(query),
            query_filter=_domain_filter(domain),
            limit=k,
            with_payload=True,
        ).points
    except Exception:  # noqa: BLE001 - hafiza erisilemezse ajan bagimsiz cevap vermemeli
        return []
    return [
        RetrievedChunk(
            text=(h.payload or {}).get("text", ""),
            source=(h.payload or {}).get("source", "bilinmiyor"),
            domain=(h.payload or {}).get("domain", domain),
            score=float(h.score),
        )
        for h in hits
    ]


_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = {"ve", "ile", "bir", "bu", "icin", "için", "mi", "mı", "mu", "mü", "ne", "nasil", "nasıl",
         "kac", "kaç", "var", "yok", "the", "and"}


_TR_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def _stems(text: str) -> set[str]:
    """Turkce eklere ve karaktersiz yazima dayanikli kaba kok: ilk 5 harf.

    "kargom"/"kargoya" -> "kargo"; "gün"/"gun" -> "gun" (musteriler cogu zaman Turkce
    karakter kullanmadan yazar).
    """
    folded = text.replace("İ", "i").replace("I", "ı").casefold().translate(_TR_FOLD)
    return {w[:5] for w in _WORD.findall(folded) if len(w) >= 3 and w not in _STOP}


def _lexical(query: str, doc: str) -> float:
    q = _stems(query)
    return len(q & _stems(doc)) / len(q) if q else 0.0


def retrieve_public(query: str, top_k: int | None = None) -> tuple[list[RetrievedChunk], str]:
    """Musteri destek ajaninin KULLANABILECEGI TEK arama fonksiyonu.

    Koleksiyon ve alan sabittir (public_faq); cagiran taraf baska bir alan secemez.
    Donus: (parcalar, mod)
      * "full"   : SSS kucuk -> tamami, hibrit skora gore sirali
      * "hybrid" : SSS buyuk -> anlamsal + anahtar kelime skoruyla ilk k parca
    Skor = 0.5 * anlamsal + 0.5 * anahtar kelime ortusmesi.
    """
    k = top_k or settings.public_faq_top_k
    everything = fetch_all(PUBLIC_DOMAIN)
    if not everything:
        return [], "empty"
    full = sum(len(c.text) for c in everything) <= settings.public_faq_full_context_chars
    pool = everything if full else retrieve(query, PUBLIC_DOMAIN, max(k * 5, 20))
    dense = {c.text: c.score for c in retrieve(query, PUBLIC_DOMAIN, len(pool))}
    scored = [
        c.model_copy(update={"score": round(0.5 * dense.get(c.text, 0.0) + 0.5 * _lexical(query, c.text), 4)})
        for c in pool
    ]
    scored.sort(key=lambda c: c.score, reverse=True)
    return (scored, "full") if full else (scored[:k], "hybrid")


def retrieve_many(queries: list[str], domain: str, top_k: int | None = None) -> list[RetrievedChunk]:
    """Birden cok sorgu icin arar, ayni parcayi tekilleyip en yuksek skoru tutar.

    Yerel Qdrant tek sureclidir; bu yuzden sorgular ardisik calistirilir.
    """
    best: dict[str, RetrievedChunk] = {}
    for q in queries:
        for chunk in retrieve(q, domain, top_k):
            key = f"{chunk.source}:{chunk.text[:80]}"
            if key not in best or chunk.score > best[key].score:
                best[key] = chunk
    return sorted(best.values(), key=lambda c: c.score, reverse=True)


def fetch_all(domain: str) -> list[RetrievedChunk]:
    """Bir alandaki TUM parcalari (skorsuz) dondurur; kaynak ve sirayla dizili.

    Kucuk, birbirinden bagimsiz kural setlerinde (kirmizi cizgiler) semantik arama bir
    kurali kacirabilir; kacirilan kural = kacirilan risk. Set kucukse hepsi verilir.
    """
    _check_domain(domain)
    client = get_client()
    out: list[tuple[str, int, RetrievedChunk]] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_for(domain),
            scroll_filter=_domain_filter(domain),
            limit=256,
            with_payload=True,
            offset=offset,
        )
        for pt in points:
            pl = pt.payload or {}
            out.append(
                (
                    pl.get("source", ""),
                    int(pl.get("chunk_index", 0)),
                    RetrievedChunk(
                        text=pl.get("text", ""), source=pl.get("source", ""), domain=domain, score=1.0
                    ),
                )
            )
        if offset is None:
            break
    return [c for _, _, c in sorted(out, key=lambda t: (t[0], t[1]))]


def build_context_block(chunks: list[RetrievedChunk]) -> str:
    """Retrieval sonucunu prompta gomulecek, kaynakli tek bir metne cevirir."""
    if not chunks:
        return "(Kurumsal hafizada bu konuyla ilgili dokuman bulunamadi.)"
    return "\n\n".join(
        f"[Parca {i + 1} | kaynak: {c.source} | benzerlik: {c.score:.3f}]\n{c.text}"
        for i, c in enumerate(chunks)
    )


def collection_stats() -> dict:
    """Alan basina parca sayisi (ic ve dis koleksiyonlar ayri raporlanir)."""
    stats: dict = {
        "collections": {"internal": settings.qdrant_collection, "public": settings.public_faq_collection},
        "domains": {},
    }
    client = get_client()
    for d in DOMAINS:
        try:
            n = client.count(collection_for(d), count_filter=_domain_filter(d), exact=True).count
        except Exception:  # noqa: BLE001
            n = 0
        stats["domains"][d] = n
    stats["points"] = sum(stats["domains"].values())
    return stats


def reset_collection(domain: str | None = None) -> None:
    """domain verilirse yalnizca o alani, verilmezse TUM hafizayi (iki koleksiyon) siler."""
    client = get_client()
    if domain is None:
        for n in (settings.qdrant_collection, settings.public_faq_collection):
            if client.collection_exists(n):
                client.delete_collection(n)
        _ensure_collection(client)
        return
    _check_domain(domain)
    client.delete(
        collection_for(domain),
        points_selector=models.FilterSelector(filter=_domain_filter(domain)),
    )


def close_client() -> None:
    """Yerel Qdrant dosya kilidini duzgun birakir."""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # noqa: BLE001
            pass
        _client = None


atexit.register(close_client)
