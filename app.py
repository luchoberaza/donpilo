from __future__ import annotations

import os
import sqlite3
import re
from datetime import datetime, date
from io import BytesIO
from typing import Dict, Any, List, Optional

from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from jinja2 import TemplateNotFound


APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "pos.db")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "donpilo-secret-key")

# La contraseña de administración no se versiona: vive en secretos_local.py
# (está en .gitignore) o en la variable de entorno DONPILO_ADMIN_PASSWORD.
try:
    from secretos_local import ADMIN_PASSWORD
except ImportError:
    ADMIN_PASSWORD = os.environ.get("DONPILO_ADMIN_PASSWORD", "")


@app.context_processor
def _password_para_el_modal() -> Dict[str, Any]:
    # El modal de confirmación de base.html valida la contraseña antes de enviar.
    return {"admin_password_modal": ADMIN_PASSWORD}


# =========================
# Helpers
# =========================
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def cents_to_money(cents: int) -> str:
    return f"{(cents or 0) / 100:.2f}"


def parse_money_to_cents(s: str) -> Optional[int]:
    if s is None:
        return None
    s = str(s).strip().replace(",", ".")
    if s == "":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v < 0:
        return None
    return int(round(v * 100))


def has_column(con: sqlite3.Connection, table: str, col: str) -> bool:
    cur = con.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return any(r["name"] == col for r in cur.fetchall())


def _check_admin_password() -> bool:
    # Sin contraseña configurada no se habilita ninguna acción de administración.
    if not ADMIN_PASSWORD:
        return False
    return (request.form.get("admin_password") or "").strip() == ADMIN_PASSWORD


def _reportlab_available() -> bool:
    try:
        import reportlab  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


# =========================
# DB init + migrations
# =========================
def ensure_db():
    con = db()
    cur = con.cursor()

    # products (la DB vieja puede no tener category)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            unit_type TEXT NOT NULL CHECK(unit_type IN ('kg','unit')),
            price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
            category TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)

    # sales
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            day TEXT NOT NULL,
            total_cents INTEGER NOT NULL,
            payment_method TEXT NOT NULL CHECK(payment_method IN ('POS','CASH')),
            cash_given_cents INTEGER,
            change_cents INTEGER,
            note TEXT
        )
    """)

    # sale_items
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('kg','unit')),
            value TEXT NOT NULL,
            qty_label TEXT NOT NULL,
            line_cents INTEGER NOT NULL,
            FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE
        )
    """)

    # Migrations SEGURAS (si tu DB es vieja)
    # 1) sales.day
    if not has_column(con, "sales", "day"):
        try:
            cur.execute("ALTER TABLE sales ADD COLUMN day TEXT")
            cur.execute("UPDATE sales SET day = substr(created_at,1,10) WHERE day IS NULL OR day=''")
        except Exception:
            pass

    # 2) products.category (este es el error que vos tenés)
    if not has_column(con, "products", "category"):
        try:
            cur.execute("ALTER TABLE products ADD COLUMN category TEXT DEFAULT ''")
        except Exception:
            pass

    # índice único por nombre (case-insensitive)
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_name_nocase ON products(name COLLATE NOCASE)")
    except Exception:
        pass

    con.commit()
    con.close()


@app.before_request
def _ensure_db():
    ensure_db()


# =========================
# KG helpers (para PDF/resumen)
# =========================
def _qty_label_to_grams(qty_label: str) -> int:
    """Convierte distintos formatos de cantidad a gramos.

    Soporta:
      - '750g' / '750 g'
      - '1kg' / '1,5kg' / '1.5kg'
      - '1 kg 500 g'  (formato viejo)
      - '1kg500g'     (formato viejo compacto)
    """
    if not qty_label:
        return 0

    raw = str(qty_label).strip().lower()
    if not raw:
        return 0

    # Formato viejo: "1 kg 500 g"
    m = re.match(r"^\s*(\d+)\s*kg\s*(\d+)\s*g\s*$", raw)
    if m:
        return int(m.group(1)) * 1000 + int(m.group(2))

    compact = raw.replace(" ", "")

    # Formato viejo compacto: "1kg500g"
    m = re.match(r"^(\d+)kg(\d+)g$", compact)
    if m:
        return int(m.group(1)) * 1000 + int(m.group(2))

    # Importante: chequear 'kg' ANTES que 'g' (porque 'kg' también termina en 'g')
    if compact.endswith("kg"):
        n = compact[:-2]
        # Soporta coma decimal y puntos como separador de miles
        if "," in n:
            n = n.replace(".", "")
            n = n.replace(",", ".")
        try:
            kg = float(n)
            return int(round(kg * 1000))
        except Exception:
            return 0

    if compact.endswith("g"):
        n = compact[:-1].replace(",", ".")
        try:
            g = float(n)
            return int(round(g))
        except Exception:
            return 0

    return 0


def _kg_text_from_grams(grams: int) -> str:
    """Formatea gramos a texto (visual, global).

    - < 1000g  -> '750g'
    - >= 1000g -> '1kg', '1,5kg', '2,25kg' (coma decimal, sin ceros al final)
    """
    try:
        g = int(grams or 0)
    except Exception:
        g = 0

    if g <= 0:
        return "-"

    if g < 1000:
        return f"{g}g"

    kg_val = g / 1000.0
    s = f"{kg_val:.3f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{s}kg"


@app.template_filter("grams_pretty")
def grams_pretty_filter(grams):
    return _kg_text_from_grams(int(grams or 0))


@app.template_filter("qty_pretty")
def qty_pretty_filter(qty_label):
    s = str(qty_label or "").strip()
    if not s:
        return "-"
    g = _qty_label_to_grams(s)
    if g > 0:
        return _kg_text_from_grams(g)
    return s


# =========================
# Reporte del día (para inicio + PDF)
# =========================
def build_today_report(day: str) -> Dict[str, Any]:
    con = db()
    cur = con.cursor()

    cur.execute("SELECT COALESCE(SUM(total_cents),0) AS t FROM sales WHERE day=?", (day,))
    total_today = int(cur.fetchone()["t"])

    cur.execute("""
        SELECT si.product_name,
               SUM(si.line_cents) AS money_cents,
               SUM(CASE WHEN si.mode='unit' THEN CAST(si.value AS INTEGER) ELSE 0 END) AS units_sold
        FROM sale_items si
        JOIN sales s ON s.id = si.sale_id
        WHERE s.day = ?
        GROUP BY si.product_name
        ORDER BY money_cents DESC
    """, (day,))
    rows = cur.fetchall()

    summary: List[Dict[str, Any]] = []
    for r in rows:
        pname = r["product_name"]
        money_cents = int(r["money_cents"] or 0)
        units_sold = int(r["units_sold"] or 0)

        cur.execute("""
            SELECT qty_label
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            WHERE s.day=? AND si.product_name=? AND si.mode='kg'
        """, (day, pname))
        grams_total = 0
        for x in cur.fetchall():
            grams_total += _qty_label_to_grams(x["qty_label"] or "")

        summary.append({
            "product_name": pname,
            "money_cents": money_cents,
            "units_sold": units_sold,
            "grams_sold": grams_total
        })

    con.close()
    return {"day": day, "total_today": total_today, "summary": summary}


# =========================
# PDF (ReportLab import lazy)
# =========================
def generate_close_day_pdf(report: Dict[str, Any]) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
    except ModuleNotFoundError as e:
        raise RuntimeError("Falta instalar 'reportlab'. Ejecutá: python -m pip install reportlab") from e

    def register_pdf_fonts() -> Dict[str, str]:
        fonts_dir = os.path.join(APP_DIR, "static", "fonts")
        pop_reg = os.path.join(fonts_dir, "Poppins-Regular.ttf")
        pop_bold = os.path.join(fonts_dir, "Poppins-Bold.ttf")
        pop_extra = os.path.join(fonts_dir, "Poppins-ExtraBold.ttf")

        family = {"regular": "Helvetica", "bold": "Helvetica-Bold", "extra": "Helvetica-Bold"}
        try:
            if os.path.exists(pop_reg) and os.path.exists(pop_bold):
                pdfmetrics.registerFont(TTFont("Poppins", pop_reg))
                pdfmetrics.registerFont(TTFont("Poppins-Bold", pop_bold))
                family["regular"] = "Poppins"
                family["bold"] = "Poppins-Bold"
                family["extra"] = "Poppins-Bold"
                if os.path.exists(pop_extra):
                    pdfmetrics.registerFont(TTFont("Poppins-ExtraBold", pop_extra))
                    family["extra"] = "Poppins-ExtraBold"
        except Exception:
            pass
        return family

    fonts = register_pdf_fonts()

    day = report["day"]
    total_cents = int(report["total_today"])
    summary: List[Dict[str, Any]] = report["summary"]

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    bg = colors.HexColor("#efe6db")
    card = colors.HexColor("#f7efe6")
    stroke = colors.HexColor("#d6bfa9")
    text = colors.HexColor("#2f2218")
    muted = colors.HexColor("#5a3e2b")
    brand = colors.HexColor("#7a4b2a")

    c.setFillColor(bg)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    margin = 18 * mm
    x0, y0, x1, y1 = margin, margin, W - margin, H - margin

    c.setFillColor(card)
    c.setStrokeColor(stroke)
    c.setLineWidth(1)
    c.roundRect(x0, y0, x1 - x0, y1 - y0, 10, fill=1, stroke=1)

    header_h = 28 * mm
    c.setFillColor(colors.HexColor("#efe2d3"))
    c.roundRect(x0, y1 - header_h, x1 - x0, header_h, 10, fill=1, stroke=0)

    logo_path = os.path.join(APP_DIR, "static", "logo.png")
    logo_size = 18 * mm
    if os.path.exists(logo_path):
        try:
            c.drawImage(
                logo_path,
                x1 - logo_size - 12*mm,
                y1 - header_h + (header_h - logo_size)/2,
                width=logo_size, height=logo_size, mask="auto"
            )
        except Exception:
            pass

    c.setFillColor(text)
    c.setFont(fonts["extra"], 20)
    c.drawString(x0 + 12*mm, y1 - 14*mm, "DonPilo")

    c.setFillColor(muted)
    c.setFont(fonts["bold"], 10)
    c.drawString(x0 + 12*mm, y1 - 20*mm, "PANADERÍA")

    c.setFillColor(text)
    c.setFont(fonts["bold"], 12)
    c.drawRightString(
        x1 - (logo_size + 16*mm if os.path.exists(logo_path) else 12*mm),
        y1 - 14*mm,
        f"Cierre de día — {day}"
    )

    total_box_y_top = y1 - header_h - 10*mm
    total_box_h = 34*mm
    c.setFillColor(colors.white)
    c.setStrokeColor(stroke)
    c.roundRect(x0 + 12*mm, total_box_y_top - total_box_h, (x1-x0) - 24*mm, total_box_h, 10, fill=1, stroke=1)

    c.setFillColor(muted)
    c.setFont(fonts["bold"], 11)
    c.drawString(x0 + 18*mm, total_box_y_top - 12*mm, "TOTAL VENDIDO HOY")

    c.setFillColor(text)
    c.setFont(fonts["extra"], 34)
    c.drawRightString(x1 - 18*mm, total_box_y_top - 24*mm, f"${cents_to_money(total_cents)}")

    table_y_top = total_box_y_top - total_box_h - 12*mm
    table_x = x0 + 12*mm
    table_w = (x1-x0) - 24*mm

    header_row_h = 10*mm
    c.setFillColor(brand)
    c.roundRect(table_x, table_y_top - header_row_h, table_w, header_row_h, 8, fill=1, stroke=0)

    col_product = table_w * 0.52
    col_units = table_w * 0.16
    col_kg = table_w * 0.16
    col_money = table_w * 0.16

    c.setFillColor(colors.white)
    c.setFont(fonts["bold"], 10)
    c.drawString(table_x + 8, table_y_top - 7*mm, "PRODUCTO")
    c.drawRightString(table_x + col_product + col_units - 8, table_y_top - 7*mm, "UNID.")
    c.drawRightString(table_x + col_product + col_units + col_kg - 8, table_y_top - 7*mm, "KG")
    c.drawRightString(table_x + table_w - 8, table_y_top - 7*mm, "$")

    row_h = 10*mm
    y = table_y_top - header_row_h

    if not summary:
        c.setFillColor(colors.white)
        c.setStrokeColor(stroke)
        c.roundRect(table_x, y - row_h, table_w, row_h, 8, fill=1, stroke=1)
        c.setFillColor(muted)
        c.setFont(fonts["bold"], 10)
        c.drawString(table_x + 10, y - 7*mm, "No hubo ventas registradas hoy.")
        y -= row_h
    else:
        for idx, it in enumerate(summary):
            pname = str(it["product_name"])
            units = int(it.get("units_sold") or 0)
            grams = int(it.get("grams_sold") or 0)
            money = int(it.get("money_cents") or 0)

            fill = colors.HexColor("#ffffff") if idx % 2 == 0 else colors.HexColor("#fffaf3")
            c.setFillColor(fill)
            c.setStrokeColor(stroke)
            c.roundRect(table_x, y - row_h, table_w, row_h, 8, fill=1, stroke=1)

            c.setFillColor(text)
            c.setFont(fonts["bold"], 10)
            c.drawString(table_x + 8, y - 7*mm, (pname[:48] + "…") if len(pname) > 49 else pname)

            c.setFont(fonts["regular"], 10)
            unit_txt = "-" if units == 0 else str(units)
            kg_txt = _kg_text_from_grams(grams)

            c.drawRightString(table_x + col_product + col_units - 8, y - 7*mm, unit_txt)
            c.drawRightString(table_x + col_product + col_units + col_kg - 8, y - 7*mm, kg_txt)
            c.setFont(fonts["bold"], 10)
            c.drawRightString(table_x + table_w - 8, y - 7*mm, f"${cents_to_money(money)}")

            y -= row_h

    c.setFillColor(muted)
    c.setFont(fonts["regular"], 9)
    c.drawString(x0 + 12*mm, y0 + 8*mm, f"Generado: {now_iso()}")

    c.save()
    return buf.getvalue()


# =========================
# Routes
# =========================
@app.get("/")
def index():
    day = today_str()
    report = build_today_report(day)
    return render_template(
        "index.html",
        day=report["day"],
        total_today=report["total_today"],
        summary=report["summary"],
        cents_to_money=cents_to_money
    )


@app.get("/sale/new")
def sale_new():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT id, name, unit_type, price_cents FROM products ORDER BY name COLLATE NOCASE ASC")
    products = cur.fetchall()
    con.close()
    return render_template("sale_new.html", products=products, cents_to_money=cents_to_money)


@app.post("/sale/create")
def sale_create():
    product_ids = request.form.getlist("product_id[]")
    modes = request.form.getlist("mode[]")
    values = request.form.getlist("value[]")

    if not product_ids or len(product_ids) != len(modes) or len(modes) != len(values):
        flash("No hay productos en la venta.", "error")
        return redirect(url_for("sale_new"))

    payment_method = (request.form.get("payment_method") or "POS").strip().upper()
    if payment_method not in ("POS", "CASH"):
        payment_method = "POS"

    cash_given_raw = request.form.get("cash_given", "")
    note = (request.form.get("note") or "").strip()

    con = db()
    cur = con.cursor()

    items = []
    total_cents = 0

    for pid, mode, val in zip(product_ids, modes, values):
        try:
            pid_i = int(pid)
        except ValueError:
            continue

        cur.execute("SELECT id, name, unit_type, price_cents FROM products WHERE id=?", (pid_i,))
        p = cur.fetchone()
        if not p:
            continue

        name = p["name"]
        unit_type = p["unit_type"]
        price_cents = int(p["price_cents"])
        mode = (mode or "").strip().lower()
        val = (val or "").strip()

        if unit_type == "unit":
            try:
                units = int(float(val.replace(",", ".")))
            except ValueError:
                units = 0
            if units <= 0:
                continue
            line_cents = price_cents * units
            qty_label = f"{units}"
            items.append((pid_i, name, "unit", str(units), qty_label, line_cents))
            total_cents += line_cents
        else:
            money_cents = parse_money_to_cents(val)
            if money_cents is None or money_cents <= 0 or price_cents <= 0:
                continue

            kg = money_cents / price_cents
            grams = int(round(kg * 1000))

            if grams < 1000:
                qty_label = f"{grams}g"
            else:
                kg_val = grams / 1000.0
                s = f"{kg_val:.3f}".rstrip("0").rstrip(".").replace(".", ",")
                qty_label = f"{s}kg"

            line_cents = money_cents
            items.append((pid_i, name, "kg", val, qty_label, line_cents))
            total_cents += line_cents

    if not items:
        con.close()
        flash("No se pudo guardar la venta. Revisá importes/cantidades.", "error")
        return redirect(url_for("sale_new"))

    cash_given_cents = None
    change_cents = None
    if payment_method == "CASH":
        cash_given_cents = parse_money_to_cents(cash_given_raw)
        if cash_given_cents is None:
            con.close()
            flash("En efectivo tenés que ingresar cuánto te pagó el cliente.", "error")
            return redirect(url_for("sale_new"))
        change_cents = cash_given_cents - total_cents

    created = now_iso()
    day = today_str()

    cur.execute("""
        INSERT INTO sales (created_at, day, total_cents, payment_method, cash_given_cents, change_cents, note)
        VALUES (?,?,?,?,?,?,?)
    """, (created, day, total_cents, payment_method, cash_given_cents, change_cents, note))
    sale_id = cur.lastrowid

    cur.executemany("""
        INSERT INTO sale_items (sale_id, product_id, product_name, mode, value, qty_label, line_cents)
        VALUES (?,?,?,?,?,?,?)
    """, [(sale_id, pid, pname, mmode, v, q, lc) for (pid, pname, mmode, v, q, lc) in items])

    con.commit()
    con.close()

    flash("Venta guardada.", "ok")
    return redirect(url_for("sales_today"))


@app.get("/sales/today")
def sales_today():
    con = db()
    cur = con.cursor()
    day = today_str()

    cur.execute("""
        SELECT id, created_at, total_cents, payment_method, cash_given_cents, change_cents, note
        FROM sales
        WHERE day=?
        ORDER BY id DESC
    """, (day,))
    sales = cur.fetchall()

    details = {}
    for s in sales:
        cur.execute("""
            SELECT product_name, qty_label, line_cents
            FROM sale_items
            WHERE sale_id=?
            ORDER BY id ASC
        """, (s["id"],))
        details[s["id"]] = cur.fetchall()

    cur.execute("SELECT COALESCE(SUM(total_cents),0) AS t FROM sales WHERE day=?", (day,))
    total_today = int(cur.fetchone()["t"])

    con.close()
    return render_template(
        "sales_today.html",
        day=day,
        sales=sales,
        details=details,
        total_today=total_today,
        cents_to_money=cents_to_money
    )


@app.post("/sales/delete/<int:sale_id>")
def sales_delete(sale_id: int):
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM sale_items WHERE sale_id=?", (sale_id,))
    cur.execute("DELETE FROM sales WHERE id=?", (sale_id,))
    con.commit()
    con.close()
    flash("Venta eliminada.", "ok")
    return redirect(url_for("sales_today"))


@app.post("/sales/clear_today")
def sales_clear_today():
    if not _check_admin_password():
        flash("Contraseña incorrecta.", "error")
        return redirect(url_for("sales_today"))

    con = db()
    cur = con.cursor()
    day = today_str()

    cur.execute("SELECT id FROM sales WHERE day=?", (day,))
    ids = [r["id"] for r in cur.fetchall()]
    for sid in ids:
        cur.execute("DELETE FROM sale_items WHERE sale_id=?", (sid,))
    cur.execute("DELETE FROM sales WHERE day=?", (day,))

    con.commit()
    con.close()
    flash("Se vaciaron las ventas de hoy.", "ok")
    return redirect(url_for("sales_today"))


@app.post("/day/close")
def close_day():
    if not _check_admin_password():
        flash("Contraseña incorrecta.", "error")
        return redirect(url_for("index"))

    if not _reportlab_available():
        flash("Para generar el PDF instalá ReportLab: python -m pip install reportlab", "error")
        return redirect(url_for("index"))

    day = today_str()
    report = build_today_report(day)

    try:
        pdf_bytes = generate_close_day_pdf(report)
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))

    # Vaciar ventas del día después del PDF
    con = db()
    cur = con.cursor()
    cur.execute("SELECT id FROM sales WHERE day=?", (day,))
    ids = [r["id"] for r in cur.fetchall()]
    for sid in ids:
        cur.execute("DELETE FROM sale_items WHERE sale_id=?", (sid,))
    cur.execute("DELETE FROM sales WHERE day=?", (day,))
    con.commit()
    con.close()

    filename = f"DonPilo_Cierre_{day}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf"
    )


# =========================
# Admin catálogo
# =========================
@app.get("/admin/products")
def admin_products():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT id, name, unit_type, price_cents FROM products ORDER BY name COLLATE NOCASE ASC")
    products = cur.fetchall()
    con.close()

    # fallback por si tu template se llama admin_stock.html
    try:
        return render_template("admin_products.html", products=products, cents_to_money=cents_to_money)
    except TemplateNotFound:
        return render_template("admin_stock.html", products=products, cents_to_money=cents_to_money)


@app.post("/admin/products/create")
def admin_products_create():
    name = (request.form.get("name") or "").strip()
    unit_type = (request.form.get("unit_type") or "").strip().lower()
    price_str = (request.form.get("price") or "").strip()

    name = " ".join(name.split())

    if not name:
        flash("Nombre requerido.", "error")
        return redirect(url_for("admin_products"))

    if unit_type not in ("kg", "unit"):
        flash("Tipo inválido.", "error")
        return redirect(url_for("admin_products"))

    price_cents = parse_money_to_cents(price_str)
    if price_cents is None or price_cents < 0:
        flash("Precio inválido.", "error")
        return redirect(url_for("admin_products"))

    if unit_type == "kg" and price_cents == 0:
        flash("Para productos por kg el precio debe ser mayor a 0.", "error")
        return redirect(url_for("admin_products"))

    con = db()
    cur = con.cursor()

    cur.execute("SELECT 1 FROM products WHERE name = ? COLLATE NOCASE", (name,))
    if cur.fetchone():
        con.close()
        flash("Ya existe un producto con ese nombre.", "error")
        return redirect(url_for("admin_products"))

    try:
        # doble-safe: insert con o sin category
        if has_column(con, "products", "category"):
            cur.execute("""
                INSERT INTO products (name, unit_type, price_cents, category, created_at)
                VALUES (?,?,?,?,?)
            """, (name, unit_type, price_cents, "", now_iso()))
        else:
            cur.execute("""
                INSERT INTO products (name, unit_type, price_cents, created_at)
                VALUES (?,?,?,?)
            """, (name, unit_type, price_cents, now_iso()))

        con.commit()
        con.close()
        flash("Producto agregado.", "ok")
        return redirect(url_for("admin_products"))

    except sqlite3.IntegrityError:
        con.rollback()
        con.close()
        flash("No se pudo agregar: ya existe o hay un dato inválido.", "error")
        return redirect(url_for("admin_products"))

    except sqlite3.OperationalError as e:
        con.rollback()
        con.close()
        flash(f"Error de base de datos: {e}", "error")
        return redirect(url_for("admin_products"))


@app.post("/admin/products/delete/<int:product_id>")
def admin_product_delete(product_id: int):
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM products WHERE id=?", (product_id,))
    con.commit()
    con.close()
    flash("Producto eliminado.", "ok")
    return redirect(url_for("admin_products"))


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug)
