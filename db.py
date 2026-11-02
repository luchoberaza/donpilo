import sqlite3
from contextlib import contextmanager

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,
    price_cents INTEGER NOT NULL DEFAULT 0,
    unit_type TEXT NOT NULL DEFAULT 'unit', -- 'unit' o 'kg'
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    total_cents INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    payment_method TEXT NOT NULL DEFAULT 'POS', -- 'POS' o 'CASH'
    cash_given_cents INTEGER,
    change_cents INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sales_created_at ON sales(created_at);

CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL,
    product_id INTEGER,
    product_name_snapshot TEXT NOT NULL,
    unit_type_snapshot TEXT NOT NULL DEFAULT 'unit',
    unit_price_cents_snapshot INTEGER NOT NULL,
    qty_value_snapshot REAL NOT NULL DEFAULT 1,
    line_total_cents INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sale_items_sale_id ON sale_items(sale_id);
"""

def _dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

@contextmanager
def get_conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)

def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return r is not None

def init_db(db_path: str):
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

        # Migraciones (por si hay DB vieja)
        if not _has_column(conn, "products", "unit_type"):
            conn.execute("ALTER TABLE products ADD COLUMN unit_type TEXT NOT NULL DEFAULT 'unit'")

        if not _has_column(conn, "sales", "payment_method"):
            conn.execute("ALTER TABLE sales ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'POS'")
        if not _has_column(conn, "sales", "cash_given_cents"):
            conn.execute("ALTER TABLE sales ADD COLUMN cash_given_cents INTEGER")
        if not _has_column(conn, "sales", "change_cents"):
            conn.execute("ALTER TABLE sales ADD COLUMN change_cents INTEGER")

        if not _has_column(conn, "sale_items", "unit_type_snapshot"):
            conn.execute("ALTER TABLE sale_items ADD COLUMN unit_type_snapshot TEXT NOT NULL DEFAULT 'unit'")
        if not _has_column(conn, "sale_items", "qty_value_snapshot"):
            conn.execute("ALTER TABLE sale_items ADD COLUMN qty_value_snapshot REAL NOT NULL DEFAULT 1")

        # Fix filas viejas (NULL)
        conn.execute("UPDATE sales SET payment_method='POS' WHERE payment_method IS NULL")
        conn.execute("UPDATE sales SET cash_given_cents=NULL WHERE payment_method='POS'")
        conn.execute("UPDATE sales SET change_cents=NULL WHERE payment_method='POS'")

        # Sacamos stock si existía (no se usa)
        if _has_table(conn, "stock"):
            conn.execute("DROP TABLE stock")

        # UNIQUE case-insensitive en nombre (si tenés duplicados previos puede fallar y no rompe)
        try:
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_products_name_nocase
                ON products(LOWER(TRIM(name)))
            """)
        except Exception:
            pass
