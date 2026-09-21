"""Finans ajaninin veritabani: departman butceleri + harcama kayitlari (ornek veri).

Ajan bu veritabanini yalnizca OKUR (butce durumu). Tek yazma islemi
`record_expense`'tir ve yalnizca (a) limit alti otomatik onayda ya da (b) insan
onayindan sonra cagrilir. `run_id` UNIQUE oldugundan ayni harcama iki kez yazilamaz.
Tum sorgular parametreli; LLM ciktisi hicbir zaman SQL metnine girmez.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# (departman, yillik butce TL, yil basindan beri kullanim orani)
_BUDGETS = [
    ("IT", 2_400_000, 0.62),
    ("Pazarlama", 1_800_000, 0.71),
    ("Satis", 1_200_000, 0.48),
    ("Insan Kaynaklari", 600_000, 0.55),
    ("Hukuk", 450_000, 0.96),  # neredeyse dolu: "butce asimi" senaryosu icin
    ("Operasyon", 1_500_000, 0.67),
    ("Ar-Ge", 3_000_000, 0.41),
]
_CATEGORIES = {
    "IT": ["Yazilim lisansi", "Donanim", "Bulut altyapisi"],
    "Pazarlama": ["Dijital reklam", "Etkinlik", "Ajans hizmeti"],
    "Satis": ["Seyahat", "Musteri agirlama", "CRM"],
    "Insan Kaynaklari": ["Egitim", "Ise alim", "Calisan etkinligi"],
    "Hukuk": ["Dis hukuk danismanligi", "Tescil ucreti", "Noter"],
    "Operasyon": ["Lojistik", "Bakim onarim", "Tedarik"],
    "Ar-Ge": ["Prototip", "Test ekipmani", "Arastirma lisansi"],
}

_SCHEMA = """
CREATE TABLE budgets (
    department TEXT PRIMARY KEY,
    fiscal_year INTEGER NOT NULL,
    annual_budget REAL NOT NULL
);
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY,
    department TEXT NOT NULL REFERENCES budgets(department),
    amount REAL NOT NULL CHECK (amount > 0),
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,             -- approved | auto_approved
    approver TEXT NOT NULL,
    run_id TEXT UNIQUE,               -- idempotency: ayni talep iki kez yazilamaz
    created_at TEXT NOT NULL
);
"""


_STATUS_TR = {"approved": "yonetici onayli", "auto_approved": "otomatik onayli"}


def _tl(v: float) -> str:
    """Turkce para bicimi: 45.000,00 TL"""
    return f"{v:,.2f} TL".replace(",", "X").replace(".", ",").replace("X", ".")


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_finance_db(path: str | Path, seed: int = 7) -> Path:
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    today = date.today()
    year_start = date(today.year, 1, 1)
    days = max((today - year_start).days, 1)

    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        for dept, budget, used in _BUDGETS:
            conn.execute(
                "INSERT INTO budgets VALUES (?,?,?)", (dept, today.year, float(budget))
            )
            target, spent = budget * used, 0.0
            while spent < target:
                amount = round(min(rng.uniform(2_000, budget * 0.06), target - spent), 2)
                if amount < 1:
                    break
                conn.execute(
                    "INSERT INTO expenses (department, amount, category, description, status, approver, run_id, created_at)"
                    " VALUES (?,?,?,?,?,?,NULL,?)",
                    (
                        dept, amount, rng.choice(_CATEGORIES[dept]), "Gecmis harcama (ornek veri)",
                        "approved", "finans@acme.com",
                        (year_start + timedelta(days=rng.randint(0, days))).isoformat(),
                    ),
                )
                spent += amount
        conn.commit()
    finally:
        conn.close()
    return path


def list_departments(path: str | Path) -> list[str]:
    with _connect(path) as conn:
        return [r["department"] for r in conn.execute("SELECT department FROM budgets ORDER BY department")]


def budget_status(path: str | Path, department: str | None = None) -> list[dict[str, Any]]:
    """Departman(lar)in yillik butce, harcanan, kalan ve kullanim oranini dondurur."""
    sql = """
        SELECT b.department, b.fiscal_year, b.annual_budget,
               COALESCE(SUM(e.amount), 0) AS spent
        FROM budgets b LEFT JOIN expenses e ON e.department = b.department
        {where}
        GROUP BY b.department ORDER BY b.department
    """
    with _connect(path) as conn:
        if department:
            rows = conn.execute(sql.format(where="WHERE b.department = ?"), (department,)).fetchall()
        else:
            rows = conn.execute(sql.format(where="")).fetchall()
    out = []
    for r in rows:
        spent = round(r["spent"], 2)
        out.append(
            {
                "department": r["department"],
                "fiscal_year": r["fiscal_year"],
                "annual_budget": r["annual_budget"],
                "spent": spent,
                "remaining": round(r["annual_budget"] - spent, 2),
                "utilization": round(spent / r["annual_budget"], 4) if r["annual_budget"] else 0.0,
            }
        )
    return out


def record_expense(
    path: str | Path,
    *,
    run_id: str,
    department: str,
    amount: float,
    category: str,
    description: str,
    status: str,
    approver: str,
) -> dict[str, Any]:
    """Harcamayi kaydeder. Ayni run_id tekrar gelirse yeni kayit ACMAZ (idempotent)."""
    with _connect(path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO expenses (department, amount, category, description, status, approver, run_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (department, amount, category, description[:500], status, approver, run_id, date.today().isoformat()),
        )
        replayed = cur.rowcount == 0
        row = conn.execute("SELECT * FROM expenses WHERE run_id = ?", (run_id,)).fetchone()
    remaining = budget_status(path, department)[0]["remaining"]
    return {
        "ok": True,
        "expense_id": row["id"],
        "department": row["department"],
        "amount": row["amount"],
        "status": row["status"],
        "approver": row["approver"],
        "remaining_after": remaining,
        "replayed": replayed,
        "message": f"{row['department']} butcesine {_tl(row['amount'])} harcama kaydedildi "
                   f"({_STATUS_TR.get(row['status'], row['status'])}).",
    }
