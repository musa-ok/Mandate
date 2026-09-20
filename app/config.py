"""Merkezi konfigurasyon. Tum sirlar ortam degiskenlerinden okunur."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
STORAGE_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM (Google Gemini) ---
    gemini_api_key: str | None = None
    model_id: str = "gemini-2.5-flash"
    max_output_tokens: int = 4096
    # -1: dinamik dusunme (model gerektigi kadar dusunur). 0: dusunme kapali.
    thinking_budget: int = -1

    # --- Vektor DB ---
    qdrant_url: str | None = None          # None -> yerel dosya tabanli Qdrant
    qdrant_api_key: str | None = None
    qdrant_path: str = str(STORAGE_DIR / "qdrant")
    qdrant_collection: str = "corporate_memory"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    rag_top_k: int = 6
    chunk_size: int = 900
    chunk_overlap: int = 150

    # --- Operasyon log DB ---
    database_url: str = f"sqlite:///{STORAGE_DIR / 'operations.db'}"

    # --- Solana ---
    solana_rpc_url: str = "https://api.devnet.solana.com"
    solana_cluster: str = "devnet"
    # Base58 gizli anahtar VEYA JSON byte dizisi. Bos ise cuzdan devre disi.
    agent_wallet_secret: str | None = None
    usdc_mint: str = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"  # devnet USDC
    usdc_decimals: int = 6

    # --- Otonom harcama guvenlik siniri (LLM'den BAGIMSIZ, kod ile zorunlu) ---
    max_single_payment_usdc: float = 1000.0
    max_daily_payment_usdc: float = 5000.0
    # Bos liste -> adres kisiti yok. Doluysa sadece bu adreslere odeme yapilir.
    allowlisted_wallets: str = ""
    dry_run: bool = True  # True ise zincire yazmaz, simule eder

    @property
    def allowlist(self) -> set[str]:
        return {a.strip() for a in self.allowlisted_wallets.split(",") if a.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
