"""Veri Analisti'nin veritabani: ornek satis verisi + GUVENLI salt-okunur sorgu calistirici.

Text-to-SQL'de tehlike: model bir SQL uretir ve biz onu calistiririz. Model
kandirilabilir (prompt injection), hata yapabilir veya zararli sorgu uretebilir.
Bu yuzden guvenlik SQL'i "dogru yazmis olmasina" degil, ALTYAPININ zorlamasina dayanir:

  1. Baglanti dosya duzeyinde SALT-OKUNUR acilir (mode=ro).
  2. SQLite authorizer yalnizca SELECT/READ/FUNCTION'a izin verir; sadece
     beyaz listedeki TABLOLAR okunabilir (sqlite_master dahil hicbir sey degil).
  3. Tek ifade, zaman asimi (progress handler) ve satir tavani.
Regex onkontrolu yalnizca dostca bir hata mesaji icindir; gercek koruma 1-3'tur.
"""
from __future__ import annotations

import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ALLOWED_TABLES = frozenset({"products", "customers", "sales"})
_SELECT_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_DATE_FN_RE = re.compile(r"\b(date|datetime|julianday|strftime)\s*\(([^()]*)\)", re.IGNORECASE)
_LITERAL_ARGS_RE = re.compile(r"\s*(?:(?:'[^']*'|-?\d+(?:\.\d+)?)\s*(?:,\s*|$))+")


class SQLGuardError(ValueError):
    """Sorgu guvenlik kurallarina takildi ya da calistirilamadi."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool

    @property
    def row_count(self) -> int:
        return len(self.rows)


# --------------------------------------------------------------------------
# Ornek veri
# --------------------------------------------------------------------------
_PRODUCTS = [
    ("Kurumsal CRM Lisansi", "Yazilim", 1200.0),
    ("Analitik Paneli Lisansi", "Yazilim", 800.0),
    ("Guvenlik Paketi Lisansi", "Yazilim", 650.0),
    ("Dizustu Bilgisayar", "Donanim", 1450.0),
    ("Sunucu Rafi", "Donanim", 4200.0),
    ("Ag Anahtari", "Donanim", 900.0),
    ("Entegrasyon Danismanligi", "Danismanlik", 2500.0),
    ("Egitim Paketi", "Danismanlik", 1100.0),
    ("Guvenlik Denetimi", "Danismanlik", 3200.0),
]
_CUSTOMERS = [
    ("Atlas Lojistik", "Marmara", "Kurumsal"),
    ("Bora Tekstil", "Ege", "KOBI"),
    ("Cinar Gida", "Ic Anadolu", "KOBI"),
    ("Deniz Enerji", "Akdeniz", "Kurumsal"),
    ("Efes Otomotiv", "Ege", "Kurumsal"),
    ("Firat Insaat", "Ic Anadolu", "KOBI"),
    ("Gunes Saglik", "Marmara", "Kurumsal"),
    ("Hitit Yazilim", "Ic Anadolu", "KOBI"),
    ("Ipek Perakende", "Marmara", "KOBI"),
    ("Kuzey Madencilik", "Akdeniz", "Kurumsal"),
    ("Lale Turizm", "Akdeniz", "KOBI"),
    ("Mavi Bankacilik", "Marmara", "Kurumsal"),
]
_SALESPEOPLE = ["Ayse Kaya", "Mehmet Demir", "Zeynep Arslan", "Can Ozturk"]

_SCHEMA = """
CREATE TABLE products  (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, unit_price REAL NOT NULL);
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL, segment TEXT NOT NULL);
CREATE TABLE sales (
    id INTEGER PRIMARY KEY,
    sale_date TEXT NOT NULL,          -- ISO 8601: YYYY-MM-DD
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    salesperson TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    discount_pct REAL NOT NULL,       -- 0-100
    total REAL NOT NULL               -- quantity * unit_price * (1 - discount_pct/100)
);
CREATE INDEX idx_sales_date ON sales(sale_date);
"""


def ensure_sales_db(path: str | Path, days: int = 270, seed: int = 42) -> Path:
    """Veritabani yoksa deterministik ornek satis verisiyle olusturur.

    Tarihler BUGUNE gore uretilir; boylece "gecen ay" gibi gorelilik ifadelerinin
    her zaman verisi olur.
    """
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    today = date.today()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO products (name, category, unit_price) VALUES (?,?,?)", _PRODUCTS
        )
        conn.executemany(
            "INSERT INTO customers (name, region, segment) VALUES (?,?,?)", _CUSTOMERS
        )
        rows = []
        for _ in range(900):
            d = today - timedelta(days=rng.randint(0, days))
            product_id = rng.randint(1, len(_PRODUCTS))
            price = _PRODUCTS[product_id - 1][2]
            qty = rng.randint(1, 8) if price < 2000 else rng.randint(1, 3)
            disc = rng.choice([0, 0, 0, 5, 10, 15])
            rows.append(
                (
                    d.isoformat(),
                    rng.randint(1, len(_CUSTOMERS)),
                    product_id,
                    rng.choice(_SALESPEOPLE),
                    qty,
                    float(disc),
                    round(qty * price * (1 - disc / 100), 2),
                )
            )
        conn.executemany(
            "INSERT INTO sales (sale_date, customer_id, product_id, salesperson, quantity, discount_pct, total)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


# --------------------------------------------------------------------------
# Sema aciklamasi (LLM'e verilir)
# --------------------------------------------------------------------------
def describe_schema(path: str | Path, max_distinct: int = 15) -> str:
    """Tablolarin DDL'ini + dusuk kardinaliteli kolonlarin GERCEK degerlerini dondurur.

    Gercek degerler (orn. region = Marmara/Ege/...) modelin filtreyi dogru
    yazmasini saglar; onlar olmadan "Marmara" yerine "marmara" gibi tahminler yapar.
    """
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        lines: list[str] = []
        for (name, ddl) in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            lines.append(ddl.strip() + ";")
            text_cols = [
                r[1] for r in conn.execute(f"PRAGMA table_info({name})") if r[2].upper() == "TEXT"
            ]
            for col in text_cols:
                values = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT DISTINCT {col} FROM {name} ORDER BY {col} LIMIT {max_distinct + 1}"
                    )
                ]
                if col != "sale_date" and len(values) <= max_distinct:
                    lines.append(f"-- {name}.{col} degerleri: {', '.join(map(str, values))}")
        lo, hi = conn.execute("SELECT MIN(sale_date), MAX(sale_date) FROM sales").fetchone()
        lines.append(f"-- sales.sale_date araligi: {lo} .. {hi}")
        return "\n".join(lines)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Guvenli calistirici
# --------------------------------------------------------------------------
def _authorizer(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if arg1 in ALLOWED_TABLES else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if (arg2 or "").lower() == "load_extension" else sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY  # INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA/... hepsi


def _check_date_expressions(conn: sqlite3.Connection, sql: str) -> None:
    """Sabit tarih ifadelerini calistirmadan ONCE degerlendirir; NULL donen varsa reddeder.

    Neden: SQLite gecersiz bir tarih degistiricisinde (orn. 'end of month' - boyle bir
    degistirici YOK) hata vermez, sessizce NULL doner. `sale_date < NULL` her zaman
    yanlistir, sorgu 0 satir doner ve ajan "veri yok" der: sessizce YANLIS cevap.
    Canli testte gorulen hata budur. Yalnizca kolon icermeyen (sabit) ifadeler denetlenir.
    """
    for match in _DATE_FN_RE.finditer(sql):
        if not _LITERAL_ARGS_RE.fullmatch(match.group(2)):
            continue  # kolon referansi iceriyor; statik degerlendirilemez
        try:
            value = conn.execute(f"SELECT {match.group(0)}").fetchone()[0]
        except sqlite3.Error:
            continue
        if value is None:
            raise SQLGuardError(
                f"Gecersiz tarih ifadesi: {match.group(0)} NULL donuyor (gecersiz degistirici). "
                "Gecerli SQLite degistiricileri: 'start of month', 'start of year', "
                "'+N days', '-N months', '+N years'. 'end of month' diye bir degistirici YOKTUR."
            )


def run_readonly_query(
    sql: str, path: str | Path, max_rows: int = 200, timeout_seconds: float = 5.0
) -> QueryResult:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise SQLGuardError("Bos sorgu.")
    if not _SELECT_RE.match(sql):
        raise SQLGuardError("Yalnizca SELECT sorgularina izin verilir.")

    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + timeout_seconds
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        _check_date_expressions(conn, sql)
        try:
            cur = conn.execute(sql)
            columns = [d[0] for d in cur.description or []]
            fetched = cur.fetchmany(max_rows + 1)
        except sqlite3.ProgrammingError as exc:  # "tek seferde bir ifade"
            raise SQLGuardError(f"Tek bir SQL ifadesine izin verilir: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            msg = str(exc)
            if "not authorized" in msg or "prohibited" in msg:
                raise SQLGuardError("Sorgu yetkisiz bir tabloya/islemi kullaniyor.") from exc
            if "interrupted" in msg:
                raise SQLGuardError(f"Sorgu {timeout_seconds:g} sn zaman asimina ugradi.") from exc
            raise SQLGuardError(f"SQL hatasi: {msg}") from exc
    finally:
        conn.close()

    truncated = len(fetched) > max_rows
    rows = [[v.decode(errors="replace") if isinstance(v, bytes) else v for v in row] for row in fetched[:max_rows]]
    return QueryResult(columns=columns, rows=rows, truncated=truncated)
