"""Merkezi konfigurasyon. Tum sirlar ortam degiskenlerinden okunur."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
DATA_DIR = BASE_DIR / "data"
STORAGE_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Hibrit LLM: tek bayrakla bulut <-> yerel ---------------------------
    # true  -> tum ajanlar yerel Ollama'yi kullanir; veri makineden CIKMAZ.
    # false -> Google Gemini (bulut).
    use_local_llm: bool = False

    gemini_api_key: str | None = None
    model_id: str = "gemini-2.5-flash"
    # Gemini 2.5'te "dusunme" tokenlari da bu sinira dahildir; dusuk tutulursa
    # cikti yarida kesilebilir.
    max_output_tokens: int = 8192
    thinking_budget: int = -1  # -1 dinamik, 0 kapali

    # --- Dinamik Bilissel Yonlendirme: ajan basina model override ---------
    # cloud   -> USE_LOCAL_LLM ne olursa olsun Gemini (bu ajanin verisi buluta GIDER)
    # local   -> her zaman Ollama
    # default -> genel USE_LOCAL_LLM ayarini izle
    # Sozlesme analizi, yerel 7B modelde yavas ve isabetsiz oldugu icin varsayilan: cloud.
    contract_analyst_llm: Literal["cloud", "local", "default"] = "cloud"
    # Olculen (2 teklif, 20 madde): Gemini 20/20 ~10 sn; yerel 7B 13/20 ~95 sn -> varsayilan cloud
    procurement_llm: Literal["cloud", "local", "default"] = "cloud"
    finance_llm: Literal["cloud", "local", "default"] = "default"
    customer_support_llm: Literal["cloud", "local", "default"] = "default"

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    # Ollama varsayilan baglam penceresi kucuktur ve tasmada SESSIZCE kirpar.
    ollama_num_ctx: int = 8192
    ollama_timeout: float = 300.0

    # --- Vektor DB (RAG) ----------------------------------------------------
    qdrant_url: str | None = None  # None -> yerel dosya tabanli Qdrant
    qdrant_api_key: str | None = None
    qdrant_path: str = str(STORAGE_DIR / "qdrant")
    qdrant_collection: str = "corporate_memory"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    rag_top_k: int = 5
    chunk_size: int = 900
    chunk_overlap: int = 150

    # --- Uygulama veritabanlari --------------------------------------------
    # Calisma / denetim kaydi. Uretim: postgresql+psycopg://kullanici:sifre@host:5432/mandate
    database_url: str = f"sqlite:///{STORAGE_DIR / 'runs.db'}"
    # LangGraph durumu (bekleyen onaylar). Bos -> checkpoint_db_path'teki SQLite dosyasi.
    # Uretim: postgresql://kullanici:sifre@host:5432/mandate  (surucu eki OLMADAN)
    checkpoint_database_url: str = ""
    checkpoint_db_path: str = str(STORAGE_DIR / "checkpoints.db")
    sales_db_path: str = str(DATA_DIR / "sales.db")  # Veri Analisti'nin sorguladigi DB

    # --- Veri Analisti (Text-to-SQL) guvenlik sinirlari ---------------------
    sql_max_rows: int = 200
    sql_timeout_seconds: float = 5.0

    # --- Sozlesme Analizcisi ------------------------------------------------
    # Bu seviyeye KADAR (dahil) risk insan onayi olmadan otomatik gecer.
    # none|low|medium|high|critical.  "low" -> medium ve uzeri insana gider.
    contract_auto_clear_max_risk: str = "low"
    # Kirmizi cizgi seti bu karakter sayisini asmiyorsa HER bolume tamami verilir
    # (kacirilan kural olmasin); asarsa bolum basina semantik arama yapilir.
    contract_full_context_chars: int = 8000
    contract_max_chars: int = 200_000
    contract_max_chunks: int = 80

    # --- Finans ajani -------------------------------------------------------
    finance_db_path: str = str(DATA_DIR / "finance.db")
    # Bu tutara (TL, dahil) kadar ve butce yetiyorsa harcama insansiz onaylanir;
    # ustu veya butce asimi -> ZORUNLU insan onayi.
    finance_auto_approve_limit: float = 10_000.0

    # --- Satin alma ajani ---------------------------------------------------
    procurement_max_chars: int = 40_000

    # --- Web arayuzu --------------------------------------------------------
    # Arayuz ayni kokenden (FastAPI) sunulur; CORS gerekmez. Ayri bir alan adindan
    # erisilecekse virgulle ayrilmis kokenler: "https://panel.acme.com,https://..."
    cors_origins: str = ""

    # --- Kanal bazli yonlendirme / hava boslugu ----------------------------
    # Ic entegrasyonlarin kimlik bilgileri: "kaynak:anahtar" ciftleri, virgulle.
    #   INTERNAL_SOURCE_KEYS=internal_panel:<anahtar>,internal_slack:<anahtar>
    # Kaynagi istemcinin BEYANI degil, sunulan anahtar belirler.
    internal_source_keys: str = ""

    # --- Public FAQ (musteri destek RAG) ------------------------------------
    # Ic hafizadan FIZIKSEL OLARAK AYRI koleksiyon.
    public_faq_collection: str = "public_faq"
    public_faq_top_k: int = 4
    # SSS bu boyuta (karakter) kadarsa TAMAMI modele verilir: kucuk bir SSS'de aramanin
    # bir maddeyi kacirma riski yoktur. Olcum: mevcut embedding modeli "kac gun icinde iade"
    # sorusunda iade maddesini ilk 4'te bile bulamadi.
    public_faq_full_context_chars: int = 6000
    # Buyuk SSS'de (hibrit arama) en iyi skor bunun altindaysa model cevap uretmez -> canli destek.
    public_faq_min_score: float = 0.30
    # extractive: musteriye SSS maddesinin cevabi KELIMESI KELIMESINE gider; model yalnizca
    #   hangi maddenin cevap oldugunu secer. Halusinasyon yapisal olarak imkansiz. (varsayilan)
    # generative: model cevabi kendi yazar (daha dogal). Kod kontrolleri telefon/e-posta/sayi
    #   uydurmasini yakalar ama ANLAM BOZULMASINI yakalayamaz: canli testte yerel model
    #   "7 gun icinde ucretsiz" kuralini "7 gunden sonra ucretli" diye yazdi.
    support_reply_mode: Literal["extractive", "generative"] = "extractive"

    # --- Insan onayi / yonetim ---------------------------------------------
    # YALNIZCA AUTH_MODE=keys: onay ve yonetim uclarini korur. Bos -> uclar kapali (503).
    admin_api_key: str | None = None

    # --- Kimlik (SSO) ve rol bazli erisim ----------------------------------
    # keys: paylasilan anahtarlar (demo; tek yonetici anahtari tum onaylari verir)
    # oidc: Entra ID / Okta / Google Workspace ile kisi bazli giris + onay matrisi
    auth_mode: Literal["keys", "oidc"] = "keys"
    oidc_issuer: str = ""  # orn. https://login.microsoftonline.com/<tenant>/v2.0
    # Istege bagli: discovery belgesinin SUNUCUDAN erisilen adresi. Tarayicinin gordugu issuer
    # ile uygulamanin IdP'ye ulastigi adres farkliysa (orn. Docker icinde http://keycloak:8080).
    # Belgedeki issuer yine OIDC_ISSUER ile birebir ayni olmali.
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # API istemcilerinin Bearer token'lari icin beklenen audience. Bos -> client_id.
    oidc_audience: str = ""
    oidc_scopes: str = "openid email profile"
    # IdP'deki uygulamanin istemci dogrulama yontemi (Okta varsayilani basic'tir)
    oidc_token_auth_method: Literal["client_secret_post", "client_secret_basic"] = "client_secret_post"
    # Rol/grup bilgisinin geldigi claim: Entra app rolleri "roles", Okta "groups".
    oidc_roles_claim: str = "roles"
    # IdP rol/grup adi (veya e-posta) -> Mandate rolu: "Finans-Yonetici:finance_manager,cfo@acme.com:cfo"
    oidc_role_map: str = ""
    # Girisi yapan HERKESE verilen roller (virgulle).
    oidc_default_roles: str = "employee"
    # Bos degilse yalnizca bu alan adlarindaki e-postalar girebilir: "acme.com,acme.com.tr"
    oidc_allowed_domains: str = ""
    # IdP'ye kayitli geri donus adresi. Bos -> istegin kendi adresinden /auth/callback.
    oidc_redirect_uri: str = ""
    # Panel oturum cerezini imzalar. AUTH_MODE=oidc iken en az 32 karakter ZORUNLU.
    session_secret: str = ""
    session_ttl_minutes: int = 480
    # Cerez yalnizca HTTPS'te gonderilsin. Yerel http denemesi icin false.
    session_cookie_secure: bool = True
    # Hangi rolun hangi eylemi hangi kosulda onaylayabilecegi
    approval_policy_path: str = str(BASE_DIR / "config" / "approval_policy.json")

    # --- Sirlar: HashiCorp Vault (KV v2) ------------------------------------
    # VAULT_ADDR bossa sirlar ortam degiskenlerinden okunur. Doluysa asagidaki sir alanlari
    # Vault'tan gelir ve ortamdakileri EZER; Vault'a ulasilamazsa uygulama BASLAMAZ.
    vault_addr: str = ""
    vault_token: str = ""
    vault_role_id: str = ""  # AppRole (token yerine)
    vault_secret_id: str = ""
    vault_namespace: str = ""  # Vault Enterprise / HCP
    vault_kv_mount: str = "secret"
    vault_secret_path: str = "mandate"


def build_settings() -> Settings:
    settings = Settings()
    if settings.vault_addr:
        from app.vault import load_secrets

        # init argumanlari ortam degiskenlerinden onceliklidir -> Vault degerleri kazanir
        settings = Settings(**load_secrets(settings))
    return settings


@lru_cache
def get_settings() -> Settings:
    return build_settings()
