"""Mazag API — chat, orders, tracking.

Run locally:  uvicorn main:app --reload --port 8010
Production:   systemd unit (see README.md)
"""

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from pydantic import BaseModel, field_validator

load_dotenv()

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DB_PATH = os.environ.get("DB_PATH", "mazag.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # set -> Postgres (Neon/Render); empty -> SQLite locally
IS_PG = bool(DATABASE_URL)
if IS_PG:
    import psycopg
    from psycopg.rows import dict_row
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
MAX_HISTORY = 12  # turns kept per chat session

gemini = genai.Client(api_key=GEMINI_API_KEY)

app = FastAPI(title="Mazag API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# ---------------------------------------------------------------- catalog
# Prices are authoritative HERE, never trusted from the client.
CATALOG = {
    "sabahi":  {"name": "Sabahi صباحي",   "price": 290,  "kind": "bag"},
    "nesma":   {"name": "Nesma نسمة",     "price": 380,  "kind": "bag"},
    "asmar":   {"name": "Asmar أسمر",     "price": 320,  "kind": "bag"},
    "mazbout": {"name": "Mazbout مظبوط",  "price": 300,  "kind": "bag"},
    "taaruf":  {"name": "El Ta'aruf التعارف", "price": 550,  "kind": "package", "segment": "explorer"},
    "shahri":  {"name": "Shahri شهري",        "price": 560,  "kind": "package", "segment": "daily"},
    "beit":    {"name": "Beit Mazag مزاج البيت", "price": 2290, "kind": "package", "segment": "household"},
}

SYSTEM_PROMPT = """You are "Mazag assistant" (مساعد مزاج), the helpful assistant of Mazag (مزاج), \
an Egyptian specialty coffee brand in Cairo. Tagline: لكل مزاج بُنّه — every mood has its bean.

LANGUAGE: Mirror the customer. Egyptian Arabic (عامية مصرية, warm and friendly) if they write Arabic, \
English if they write English. Keep replies SHORT — 1-3 sentences unless asked for detail.

BLENDS (250g bags):
- Sabahi صباحي — EGP 290. Breakfast blend, medium roast, chocolate/hazelnut. Brazil+Colombia. For espresso, french press. Mood: بدري وصاحي.
- Nesma نسمة — EGP 380. Single origin Ethiopia Yirgacheffe, light roast, floral/citrus. For V60/filter. Mood: رايق.
- Asmar أسمر — EGP 320. Espresso blend, dark roast, cocoa/caramel, thick crema. Brazil+India. Mood: محتاج تركيز.
- Mazbout مظبوط — EGP 300. Turkish grind with cardamom (بن محوّج), dark. For kanaka. Mood: كلاسيكي.

PACKAGES (the main products — always prefer recommending a package):
- El Ta'aruf التعارف — EGP 550 one-time. 4x125g taster of all blends + brew guide. For people new to specialty coffee.
- Shahri شهري — EGP 560/month. 2x250g monthly, customer picks blends, pause/swap anytime. Saves ~7%. Most popular. For daily drinkers.
- Beit Mazag مزاج البيت — EGP 2290 for 3 months. 3x250g monthly, one payment, priority delivery. Saves ~12%. For households/heavy drinkers.
- Lel Sherka للشركات — custom pricing. 2kg+/month for offices, invoiced, machine guidance. If asked: collect company name, monthly volume, and contact, and say the team will reach out within a day.

CONSUMPTION RULE OF THUMB: one cup uses ~15g (espresso/filter) or ~8g (turkish). A 250g bag ≈ 16 cups filter or ≈ 30 cups turkish. Use this to recommend the right package.

ORDERING & DELIVERY:
- Orders go through the website checkout (name, phone, area, address). Payment: cash on delivery. Fawry code coming soon.
- Delivery: Cairo & Giza, 2-4 working days, roasted fresh weekly. Order confirmed by WhatsApp message before dispatch.
- Tracking: /track page with phone + order code (MZ-XXXX).

RULES:
- NEVER invent prices, discounts, products, or policies not listed above. If unsure, say you'll check and suggest WhatsApp.
- If someone wants to order, guide them to the checkout on the page (or the package button) — you don't take payment in chat.
- Stay on topic: coffee, Mazag products, orders. Politely redirect anything else.
"""

# ---------------------------------------------------------------- db
def Q(sql: str) -> str:
    """Translate '?' placeholders to '%s' when running on Postgres."""
    return sql.replace("?", "%s") if IS_PG else sql


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    phone TEXT PRIMARY KEY,
    name TEXT, area TEXT, address TEXT,
    segment TEXT,
    orders_count INTEGER DEFAULT 0,
    total_spent INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id {pk},
    code TEXT UNIQUE,
    phone TEXT REFERENCES customers(phone),
    items TEXT, notes TEXT,
    total INTEGER,
    payment_method TEXT DEFAULT 'cod',
    status TEXT DEFAULT 'pending',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    history TEXT,
    updated_at TEXT
);
"""


def init_db():
    schema = SCHEMA.format(pk="SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT")
    with db() as con:
        if IS_PG:
            con.execute(schema)
        else:
            con.executescript(schema)


@contextmanager
def db():
    if IS_PG:
        con = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    else:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- rate limit (simple, per-IP)
_hits: dict[str, list[float]] = {}

def rate_limit(ip: str, limit: int = 30, window: int = 60):
    t = time.time()
    _hits[ip] = [h for h in _hits.get(ip, []) if t - h < window]
    if len(_hits[ip]) >= limit:
        raise HTTPException(429, "Too many requests")
    _hits[ip].append(t)


# ---------------------------------------------------------------- models
PHONE_RE = re.compile(r"^01[0125][0-9]{8}$")

class OrderItem(BaseModel):
    sku: str
    qty: int

    @field_validator("sku")
    @classmethod
    def sku_exists(cls, v):
        if v not in CATALOG:
            raise ValueError("unknown sku")
        return v

    @field_validator("qty")
    @classmethod
    def qty_sane(cls, v):
        if not 1 <= v <= 20:
            raise ValueError("qty out of range")
        return v


class Customer(BaseModel):
    name: str
    phone: str
    area: str
    address: str

    @field_validator("phone")
    @classmethod
    def phone_valid(cls, v):
        v = re.sub(r"[\s-]", "", v)
        if not PHONE_RE.match(v):
            raise ValueError("invalid Egyptian phone")
        return v


class OrderIn(BaseModel):
    customer: Customer
    items: list[OrderItem]
    notes: str = ""
    payment_method: str = "cod"
    source: str = "web"


class ChatIn(BaseModel):
    message: str
    session_id: str = ""
    phone: str = ""  # optional: known customer phone for personalization


# ---------------------------------------------------------------- helpers
def derive_segment(items: list[OrderItem]) -> str | None:
    for it in items:
        seg = CATALOG[it.sku].get("segment")
        if seg:
            return seg
    return None


# ---------------------------------------------------------------- routes
@app.on_event("startup")
def _startup():
    init_db()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/orders")
def create_order(order: OrderIn, request: Request):
    rate_limit(request.client.host, limit=10)

    total = sum(CATALOG[it.sku]["price"] * it.qty for it in order.items)
    if total <= 0:
        raise HTTPException(400, "empty order")

    seg = derive_segment(order.items)
    c = order.customer
    with db() as con:
        con.execute(
            Q("""INSERT INTO customers (phone, name, area, address, segment, orders_count, total_spent, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                 name = excluded.name, area = excluded.area, address = excluded.address,
                 segment = COALESCE(excluded.segment, customers.segment),
                 orders_count = customers.orders_count + 1,
                 total_spent = customers.total_spent + excluded.total_spent,
                 updated_at = excluded.updated_at"""),
            (c.phone, c.name, c.area, c.address, seg, total, now(), now()),
        )
        row = con.execute(
            Q("""INSERT INTO orders (code, phone, items, notes, total, payment_method, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id"""),
            ("TMP", c.phone, json.dumps([it.model_dump() for it in order.items]),
             order.notes, total, order.payment_method, now()),
        ).fetchone()
        oid = row["id"]
        code = f"MZ-{1000 + oid}"
        con.execute(Q("UPDATE orders SET code = ? WHERE id = ?"), (code, oid))

    # TODO phase 2: send WhatsApp confirmation message here (status pending -> confirmed on reply)
    return {"order_code": code, "total": total, "status": "pending"}


@app.get("/api/orders/track")
def track(phone: str, code: str, request: Request):
    rate_limit(request.client.host, limit=20)
    phone = re.sub(r"[\s-]", "", phone)
    with db() as con:
        row = con.execute(
            Q("SELECT status, created_at FROM orders WHERE phone = ? AND code = ?"),
            (phone, code.upper().strip()),
        ).fetchone()
    if not row:
        raise HTTPException(404, "order not found")
    return {"status": row["status"], "eta": "2-4 working days from confirmation"}


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    rate_limit(request.client.host, limit=20)
    msg = body.message.strip()
    if not msg or len(msg) > 1000:
        raise HTTPException(400, "bad message")

    session_id = body.session_id or str(uuid.uuid4())
    customer_context = ""
    phone = re.sub(r"[\s-]", "", body.phone or "")
    if PHONE_RE.match(phone):
        with db() as con:
            cust = con.execute(
                Q("SELECT name, segment, orders_count, total_spent FROM customers WHERE phone = ?"),
                (phone,),
            ).fetchone()
            last = con.execute(
                Q("SELECT items, status, code FROM orders WHERE phone = ? ORDER BY id DESC LIMIT 1"),
                (phone,),
            ).fetchone()
        if cust:
            items_txt = ""
            if last:
                skus = [f'{CATALOG[i["sku"]]["name"]} x{i["qty"]}' for i in json.loads(last["items"]) if i["sku"] in CATALOG]
                items_txt = f' Last order {last["code"]} ({last["status"]}): {", ".join(skus)}.'
            customer_context = (
                f'\n\nRETURNING CUSTOMER: {cust["name"]} — segment: {cust["segment"] or "unknown"}, '
                f'{cust["orders_count"]} order(s), EGP {cust["total_spent"]} total.{items_txt} '
                "Greet them by first name once, reference their history naturally when relevant "
                "(e.g. offer to reorder the same), and never repeat their personal data back unnecessarily."
            )

    with db() as con:
        row = con.execute(
            Q("SELECT history FROM chat_sessions WHERE session_id = ?"), (session_id,)
        ).fetchone()
    history = json.loads(row["history"]) if row else []

    contents = [
        {"role": h["role"], "parts": [{"text": h["text"]}]} for h in history
    ] + [{"role": "user", "parts": [{"text": msg}]}]

    resp = gemini.models.generate_content(
        model=MODEL,
        contents=contents,
        config={"system_instruction": SYSTEM_PROMPT + customer_context, "max_output_tokens": 1500},
    )
    answer = (resp.text or "").strip() or "معلش، حصلت مشكلة صغيرة — جرب تاني."

    history = (history + [
        {"role": "user", "text": msg},
        {"role": "model", "text": answer},
    ])[-MAX_HISTORY * 2:]
    with db() as con:
        con.execute(
            Q("""INSERT INTO chat_sessions (session_id, history, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET history = excluded.history, updated_at = excluded.updated_at"""),
            (session_id, json.dumps(history, ensure_ascii=False), now()),
        )

    return {"reply": answer, "session_id": session_id}
