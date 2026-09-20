"""Ornek kural dokumanini kurumsal hafizaya yukler.

Kullanim:  python -m scripts.seed [dosya ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402
from app.rag.store import collection_stats, ingest_path  # noqa: E402

DEFAULT = Path(__file__).resolve().parent.parent / "data" / "sirket_harcama_politikasi.txt"


def main() -> None:
    init_db()
    paths = [Path(p) for p in sys.argv[1:]] or [DEFAULT]
    for p in paths:
        if not p.exists():
            print(f"[atlandi] bulunamadi: {p}")
            continue
        result = ingest_path(p)
        print(f"[ok] {result['filename']}: {result['chunks']} parca / {result['characters']} karakter")
    print(f"Hafiza durumu: {collection_stats()}")


if __name__ == "__main__":
    main()
