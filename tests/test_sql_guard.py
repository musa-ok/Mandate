"""Text-to-SQL guvenligi. Regex'i ATLATAN saldirilar da denenir: gercek koruma
regex degil, salt-okunur baglanti + SQLite authorizer'dir."""
import sqlite3
import time

import pytest

from app.config import get_settings
from app.tools.sales_db import SQLGuardError, ensure_sales_db, run_readonly_query


@pytest.fixture(scope="module")
def db():
    return ensure_sales_db(get_settings().sales_db_path)


def count(db):
    return sqlite3.connect(db).execute("SELECT COUNT(*) FROM sales").fetchone()[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM sales",
        "select region, count(*) from customers group by region;",
        "WITH m AS (SELECT strftime('%Y-%m', sale_date) ay, SUM(total) t FROM sales GROUP BY 1) SELECT * FROM m",
        "SELECT p.name, SUM(s.quantity) FROM sales s JOIN products p ON p.id = s.product_id GROUP BY 1",
    ],
)
def test_legitimate_queries_work(db, sql):
    assert run_readonly_query(sql, db).row_count >= 1


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE sales",
        "DELETE FROM sales",
        "UPDATE sales SET total = 0",
        "INSERT INTO products(name,category,unit_price) VALUES('x','y',1)",
        "PRAGMA table_info(sales)",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "SELECT 1; DROP TABLE sales",
        "SELECT * FROM sales; DELETE FROM sales",
        "",
        "SELEKT 1",
    ],
)
def test_blocked_by_front_door(db, sql):
    before = count(db)
    with pytest.raises(SQLGuardError):
        run_readonly_query(sql, db)
    assert count(db) == before


@pytest.mark.parametrize(
    "sql",
    [
        # 'WITH' ile basladiklari icin regex'ten GECER; yalniz altyapi katmani durdurabilir:
        "WITH x AS (SELECT 1) DELETE FROM sales",
        "WITH x AS (SELECT 1) UPDATE sales SET total = 0",
        "WITH x AS (SELECT 1) INSERT INTO products(name,category,unit_price) VALUES('h','h',1)",
        # okuma tarafi: yetkisiz tablolar
        "SELECT * FROM sqlite_master",
        "SELECT name FROM sqlite_schema",
        "SELECT * FROM users",
        "SELECT load_extension('x')",
    ],
)
def test_blocked_by_infrastructure_even_if_regex_is_bypassed(db, sql):
    before = count(db)
    with pytest.raises(SQLGuardError):
        run_readonly_query(sql, db)
    assert count(db) == before, "veri DEGISMEMELI"


def test_runaway_query_is_stopped_by_timeout(db):
    t = time.monotonic()
    with pytest.raises(SQLGuardError, match="zaman as"):
        run_readonly_query("SELECT COUNT(*) FROM sales a, sales b, sales c, sales d", db, timeout_seconds=1)
    assert time.monotonic() - t < 5


def test_row_cap_truncates(db):
    r = run_readonly_query("SELECT * FROM sales", db, max_rows=10)
    assert r.row_count == 10 and r.truncated is True


@pytest.mark.parametrize(
    "sql",
    [
        # canli testte qwen2.5'in urettigi sorgu: 'end of month' gecersiz -> NULL -> 0 satir
        "SELECT COUNT(*) FROM sales WHERE sale_date >= date('now','start of month') AND sale_date < date('now','end of month')",
        "SELECT COUNT(*) FROM sales WHERE sale_date > datetime('now','next week')",
    ],
)
def test_invalid_date_modifier_is_rejected_not_silently_empty(db, sql):
    with pytest.raises(SQLGuardError, match="Gecersiz tarih ifadesi"):
        run_readonly_query(sql, db)


def test_valid_date_expressions_still_work(db):
    r = run_readonly_query(
        "SELECT COUNT(*) FROM sales WHERE sale_date >= date('now','start of year') "
        "AND sale_date < date('now','start of month','-1 month','+10 days') "
        "AND strftime('%Y', sale_date) = strftime('%Y','now')",
        db,
    )
    assert r.rows[0][0] > 0
