# Mandate uygulama imaji. Sirlar imaja GIRMEZ (.dockerignore); calisirken ortamdan veya Vault'tan gelir.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FASTEMBED_CACHE_PATH=/opt/models/fastembed

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Embedding modeli imaja gomulur: uygulama calisirken internete cikmadan (kapali agda) acilir.
ARG EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='${EMBEDDING_MODEL}')"

COPY app ./app
COPY config ./config
COPY data ./data
COPY scripts ./scripts

# root olmayan kullanici; yazilabilir tek yer /app/storage (ornek veritabanlari da oraya)
RUN useradd --system --uid 10001 --home-dir /app mandate \
    && mkdir -p /app/storage \
    && chown -R mandate /app/storage /opt/models
USER mandate
ENV SALES_DB_PATH=/app/storage/sales.db \
    FINANCE_DB_PATH=/app/storage/finance.db \
    CHECKPOINT_DB_PATH=/app/storage/checkpoints.db \
    DATABASE_URL=sqlite:////app/storage/runs.db \
    QDRANT_PATH=/app/storage/qdrant

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
# TLS'i onundeki ters vekil (nginx, Traefik, yuk dengeleyici) sonlandirir. Vekilin adresini
# FORWARDED_ALLOW_IPS ile verin; aksi halde X-Forwarded-* basliklarina guvenilmez.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
