"""Demo verisini yukler: bilgi tabani belgeleri + ornek satis veritabani.

Kullanim:
    python -m scripts.seed            # eksikleri yukler
    python -m scripts.seed --reset    # once tum kurumsal hafizayi siler

NOT: Dosya tabanli Qdrant tek sureclidir. Sunucu calisirken bu betik kilide takilir;
sunucu aciksa belgeleri `POST /memory/upload?domain=...` ile yukleyin.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATA_DIR, get_settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.rag.store import collection_stats, ingest_path, reset_collection  # noqa: E402
from app.tools.finance_db import ensure_finance_db  # noqa: E402
from app.tools.sales_db import ensure_sales_db  # noqa: E402

KNOWLEDGE = [
    (DATA_DIR / "it_hr_politikasi.txt", "it_hr"),
    (DATA_DIR / "kirmizi_cizgiler.txt", "red_lines"),
    (DATA_DIR / "satin_alma_sartnamesi.txt", "procurement"),
    (DATA_DIR / "public_faq.txt", "public_faq"),  # AYRI koleksiyon, musteriye acik
]


def main() -> None:
    init_db()
    if "--reset" in sys.argv:
        reset_collection()
        print("[reset] kurumsal hafiza temizlendi")

    for path, domain in KNOWLEDGE:
        r = ingest_path(path, domain)
        print(f"[ok] {domain:9} <- {r['filename']}: {r['chunks']} parca / {r['characters']} karakter")

    db = ensure_sales_db(get_settings().sales_db_path)
    print(f"[ok] satis veritabani: {db}")
    print(f"[ok] butce veritabani: {ensure_finance_db(get_settings().finance_db_path)}")
    print(f"Hafiza: {collection_stats()}")


if __name__ == "__main__":
    main()
