from __future__ import annotations

import hashlib
import hmac
import asyncio
import base64
import json
import math
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import pymupdf
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DATABASE_URL = os.getenv("NEON_DATABASE_URL", "").strip()
DATA_DIR = ROOT / "data"
STORE_PATH = DATA_DIR / "store.json"
DATA_DIR.mkdir(exist_ok=True)

PRODUCTS = json.loads((ROOT / 'frontend' / 'catalog.json').read_text(encoding='utf-8'))

BASE = {
    "orders": [],
    "products": PRODUCTS,
    "policy": "Returns are accepted within 14 days of delivery for unused items in original packaging. Eligible return requests below ₹2,000 are automatically approved by the low-value policy rule; requests of ₹2,000 or more require administrator approval. Approval does not mean a refund is paid: returns must first be received and inspected. Refunds are sent to the original payment method within 5–7 business days after inspection. COD orders are refunded by bank transfer after the customer provides account details through the secure support form. Items marked final sale, personalized items, and products damaged through misuse are not eligible. Never promise a refund until eligibility is verified.",
    "approvals": [],
    "refunds": [],
    "conversations": [],
    "ai_usage": [],
    "openwa_processed": [],
}


def load_store() -> dict[str, Any]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        for key, value in BASE.items():
            data.setdefault(key, value)
        # Orders are Neon-owned. Never hydrate legacy JSON order records.
        data["orders"] = []
        data.pop("checkout_keys", None)
        data["approvals"] = []
        data["refunds"] = []
        if "Every return and refund request requires administrator approval" in data.get("policy", ""):
            data["policy"] = BASE["policy"]
        existing = {product.get("id"): product for product in data["products"]}
        for product in BASE["products"]:
            current = existing.get(product["id"])
            if current is None:
                data["products"].append(product.copy())
            else:
                current.update(product)
        save_store(data)
        return data
    except (OSError, json.JSONDecodeError):
        save_store(BASE)
        return json.loads(json.dumps(BASE))


def save_store(data: dict[str, Any]) -> None:
    temp = STORE_PATH.with_suffix(".tmp")
    persistent = {key: value for key, value in data.items() if key not in {"orders", "checkout_keys", "approvals", "refunds"}}
    persistent.update({"orders": [], "approvals": [], "refunds": []})
    temp.write_text(json.dumps(persistent, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STORE_PATH)


store = load_store()
openwa_inflight: set[str] = set()
db_pool = None


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class NeonCursor:
    def __init__(self, cursor: Any):
        self.cursor = cursor

    async def fetchone(self) -> Any:
        return await asyncio.to_thread(self.cursor.fetchone)

    async def fetchall(self) -> Any:
        return await asyncio.to_thread(self.cursor.fetchall)


class NeonConnection:
    def __init__(self, connection: Any):
        self.raw = connection

    async def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> NeonCursor:
        def run() -> Any:
            cursor = self.raw.cursor()
            cursor.execute(statement, params or ())
            return cursor
        return NeonCursor(await asyncio.to_thread(run))

    @asynccontextmanager
    async def transaction(self):
        try:
            await self.execute("BEGIN")
            yield self
            await asyncio.to_thread(self.raw.commit)
        except Exception:
            await asyncio.to_thread(self.raw.rollback)
            raise


class NeonPool:
    def __init__(self, dsn: str, size: int = 5):
        self.dsn, self.size = dsn, size
        self.available: asyncio.Queue[Any] = asyncio.Queue()
        self.connections: list[Any] = []

    async def open(self) -> None:
        import pg8000.dbapi
        parsed = urlparse(self.dsn)
        options = {
            "user": unquote(parsed.username or ""), "password": unquote(parsed.password or ""),
            "host": parsed.hostname, "port": parsed.port or 5432,
            "database": unquote(parsed.path.lstrip("/")), "ssl_context": True,
            "timeout": 8,
        }
        for _ in range(self.size):
            conn = await asyncio.to_thread(pg8000.dbapi.connect, **options)
            conn.autocommit = True
            self.connections.append(conn)
            await self.available.put(conn)

    @asynccontextmanager
    async def connection(self):
        raw = await self.available.get()
        try:
            yield NeonConnection(raw)
        finally:
            await self.available.put(raw)

    async def close(self) -> None:
        for conn in self.connections:
            await asyncio.to_thread(conn.close)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global db_pool
    if DATABASE_URL:
        try:
            db_pool = NeonPool(DATABASE_URL, size=1)
            await db_pool.open()
            schema = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
            async with db_pool.connection() as conn:
                for statement in schema.split(";"):
                    if statement.strip():
                        await conn.execute(statement)
            await hydrate_neon_store()
            print("Neon PostgreSQL connected; order schema is ready.")
        except Exception as exc:
            db_pool = None
            print(f"Neon database initialization failed: {type(exc).__name__}: {exc}")
    try:
        yield
    finally:
        if db_pool:
            await db_pool.close()


app = FastAPI(title="Techjays Customer Support Assistant", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"], allow_credentials=False)
budget_lock = asyncio.Lock()
checkout_lock = asyncio.Lock()


class ChatInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    phone: str | None = None
    channel: str = "web"
    lang: str = "auto"
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=8)


class ApprovalInput(BaseModel):
    decision: str
    reviewer: str = "Support executive"


class PolicyInput(BaseModel):
    policy: str = Field(min_length=1, max_length=50000)


class SendInput(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    text: str = Field(min_length=1, max_length=4000)


class CheckoutItem(BaseModel):
    product_id: str = Field(min_length=1, max_length=80)
    size: str = Field(min_length=1, max_length=20)
    quantity: int = Field(default=1, ge=1, le=10)


class CheckoutInput(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    phone: str = Field(min_length=10, max_length=16)
    address: str = Field(min_length=8, max_length=300)
    city: str = Field(min_length=2, max_length=80)
    state: str = Field(min_length=2, max_length=80)
    pincode: str = Field(pattern=r"^[1-9][0-9]{5}$")
    requested_delivery_date: date
    requested_delivery_slot: str = Field(pattern=r"^(09:00-12:00|12:00-15:00|15:00-18:00|18:00-21:00)$")
    items: list[CheckoutItem] = Field(min_length=1, max_length=30)
    idempotency_key: str = Field(min_length=8, max_length=80)


class CancelOrderInput(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    reason: str = Field(min_length=3, max_length=500)


class ReturnOrderInput(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    reason: str = Field(min_length=3, max_length=500)
    unused: bool = False
    original_packaging: bool = False


class OrderStatusInput(BaseModel):
    status: str = Field(min_length=3, max_length=40)
    note: str = Field(default="", max_length=500)
    estimated_delivery_from: date | None = None
    estimated_delivery_to: date | None = None


class AccountCancelInput(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class AccountReturnInput(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    unused: bool = False
    original_packaging: bool = False


class RegisterInput(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    phone: str = Field(min_length=10, max_length=20)
    password: str = Field(min_length=10, max_length=128)


class LoginInput(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def provider_status() -> dict[str, Any]:
    whatsapp_provider = os.getenv("WHATSAPP_PROVIDER", "openwa").strip().lower()
    openwa_configured = bool(os.getenv("OPENWA_API_KEY") and os.getenv("OPENWA_SESSION_ID"))
    meta_configured = bool(os.getenv("WHATSAPP_ACCESS_TOKEN") and os.getenv("WHATSAPP_PHONE_NUMBER_ID"))
    return {"groq": bool(os.getenv("GROQ_API_KEY")), "gemini": bool(os.getenv("GEMINI_API_KEY")), "whatsapp": openwa_configured if whatsapp_provider == "openwa" else meta_configured, "whatsapp_provider": "OpenWA" if whatsapp_provider == "openwa" else "Meta Cloud API", "email":bool(os.getenv("EMAIL_APPS_SCRIPT_URL") and os.getenv("EMAIL_APPS_SCRIPT_TOKEN"))}


def budget_info() -> dict[str, Any]:
    month = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m")
    spent = sum(float(item.get("cost_inr", 0)) for item in store.get("ai_usage", []) if item.get("month") == month)
    limit = float(os.getenv("AI_MONTHLY_BUDGET_INR", "50000"))
    return {"month":month,"spent_inr":round(spent,4),"limit_inr":limit,"remaining_inr":round(max(0,limit-spent),4),"percent":round(min(100,(spent/limit*100 if limit else 100)),1),"ledger":"estimated"}


def estimate_ai_cost(provider: str, input_tokens: int = 0, output_tokens: int = 0, audio_seconds: float = 0) -> float:
    fx = float(os.getenv("AI_USD_TO_INR", "90"))
    if provider == "groq":
        usd = input_tokens*float(os.getenv("GROQ_INPUT_USD_PER_M", "0.075"))/1_000_000 + output_tokens*float(os.getenv("GROQ_OUTPUT_USD_PER_M", "0.30"))/1_000_000
    elif provider == "gemini":
        usd = input_tokens*float(os.getenv("GEMINI_INPUT_USD_PER_M", "0.30"))/1_000_000 + output_tokens*float(os.getenv("GEMINI_OUTPUT_USD_PER_M", "2.50"))/1_000_000
    else:
        usd = audio_seconds*float(os.getenv("WHISPER_USD_PER_AUDIO_HOUR", "0.04"))/3600
    return usd*fx


async def reserve_ai_budget(provider: str, input_tokens: int = 0, output_tokens: int = 0, audio_seconds: float = 0) -> dict[str, Any] | None:
    estimate = estimate_ai_cost(provider,input_tokens,output_tokens,audio_seconds)
    async with budget_lock:
        info = budget_info()
        if info["spent_inr"] + estimate > info["limit_inr"]:
            return None
        entry = {"id":str(uuid.uuid4()),"provider":provider,"month":info["month"],"at":utcnow(),"input_tokens":input_tokens,"output_tokens":output_tokens,"audio_seconds":audio_seconds,"cost_inr":estimate,"status":"reserved"}
        store.setdefault("ai_usage",[]).append(entry)
        # Keep a short history for the local prototype; retain the full monthly ledger in production storage.
        store["ai_usage"] = store["ai_usage"][-5000:]
        save_store(store)
        return entry


async def settle_ai_budget(entry: dict[str, Any], input_tokens: int = 0, output_tokens: int = 0, audio_seconds: float = 0, status: str = "settled") -> None:
    async with budget_lock:
        current = next((item for item in store.get("ai_usage",[]) if item["id"] == entry["id"]),None)
        if current:
            current.update({"input_tokens":input_tokens,"output_tokens":output_tokens,"audio_seconds":audio_seconds,"cost_inr":estimate_ai_cost(current["provider"],input_tokens,output_tokens,audio_seconds),"status":status})
            save_store(store)


async def send_order_email(order: dict[str, Any]) -> dict[str, Any]:
    url, token = os.getenv("EMAIL_APPS_SCRIPT_URL"), os.getenv("EMAIL_APPS_SCRIPT_TOKEN")
    if not url or not token: return {"sent":False,"reason":"Apps Script email is not configured."}
    payload={"token":token,"order":{"order_id":order["id"],"name":order["customer"],"email":order["email"],"phone":order["phone"],"items":order["line_items"],"subtotal":order["subtotal"],"shipping":order["shipping"],"total":order["amount"],"payment":order["payment"],"delivery":order["delivery"],"estimated_delivery_from":order["estimated_delivery_from"],"estimated_delivery_to":order["estimated_delivery_to"],"requested_delivery_date":order["requested_delivery_date"],"requested_delivery_slot":order["requested_delivery_slot"],"address":order["address"],"city":order["city"],"state":order["state"],"pincode":order["pincode"]}}
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=True) as client:
            response=await client.post(url,json=payload)
            response.raise_for_status()
            result=response.json()
            return {"sent":bool(result.get("sent")),"reason":result.get("reason")}
    except (httpx.HTTPError,ValueError) as exc:
        return {"sent":False,"reason":f"Apps Script delivery failed ({type(exc).__name__})."}


def is_returns_topic(question: str) -> bool:
    return bool(re.search(r"\b(refunds?|returns?|money back|exchange)\b|திருப்பி|திரும்ப|ரீஃபண்ட்", question, re.I))


def tamil_or_tanglish(question: str) -> bool:
    return bool(re.search(r"[\u0B80-\u0BFF]|\b(venum|pannanum|pannunga|thiruppi|thirumba|enna|eppo|enga|evlo|sattai|irukka|aachu)\b", question, re.I))


def collection_intent(question: str) -> str | None:
    q = question.lower().replace("’", "'")
    has_collection_word = bool(re.search(r"\b(collections?|catalog|categories)\b", q))
    if has_collection_word and re.search(r"\b(all|what|which|available|there|list)\b", q):
        return "all"
    if not re.search(r"\b(collection|clothes|clothing|wear|fashion|catalog|outfits)\b|show me|browse|shop", q):
        return None
    if re.search(r"\b(men|mens|men's|male|gents|menswear)\b", q): return "Men"
    if re.search(r"\b(women|womens|women's|woman|female|ladies|womenswear)\b", q): return "Women"
    if re.search(r"\b(unisex|others|other|accessories|accessory)\b", q): return "Unisex"
    return None


def is_order_tracking_question(question: str) -> bool:
    return bool(re.search(r"\b(track|tracking|where is|status of|check on|check my|locate|find)\b.{0,50}\b(order|purchase|delivery|package|shipment)\b|\b(order|purchase|delivery|package|shipment)\b.{0,50}\b(status|where|track|tracking|arrive|coming)\b", question, re.I))


def is_add_to_cart_request(question: str) -> bool:
    return bool(re.search(r"\b(add|put|move|place)\b.{0,60}\b(cart|bag)\b|\b(cart|bag)\b.{0,40}\b(add|put)\b", question, re.I))


def recent_product_references(question: str, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve pronouns only against products shown in recent assistant turns."""
    if not re.search(r"\b(it|this|that|these|those|one|same)\b", question, re.I):
        return []
    by_id = {p["id"]: p for p in store["products"]}
    for turn in reversed(history[-8:]):
        if turn.get("role") != "assistant":
            continue
        products = [by_id[x.get("id")] for x in turn.get("products", []) if isinstance(x, dict) and x.get("id") in by_id]
        if products:
            return products[:4]
        answer = str(turn.get("text", "" )).casefold()
        mentioned = [p for p in store["products"] if p["name"].casefold() in answer]
        if mentioned:
            return mentioned[:4]
    return []


def resolve_cart_target(question: str, context: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a product and an explicit available size, or a product needing size choice."""
    norm = re.sub(r"[^a-z0-9]", "", question.casefold())
    exact = [p for p in store["products"] if re.sub(r"[^a-z0-9]", "", p["name"].casefold()) in norm]
    candidates = exact or recent_product_references(question, history)
    if not candidates:
        candidates = [p for p in context.get("products", []) if re.sub(r"[^a-z0-9]", "", p["name"].casefold()) in norm]
    if len(candidates) != 1:
        return None
    product = candidates[0]
    if product.get("sizes") == ["ONE SIZE"]:
        return {"product":product,"size":"ONE SIZE"}
    for size in product.get("sizes", []):
        if re.search(rf"(?<![a-z0-9]){re.escape(size.casefold())}(?![a-z0-9])", question.casefold()):
            return {"product":product,"size":size}
    aliases = {"extra small":"XS","small":"S","medium":"M","large":"L","extra large":"XL","double extra large":"XXL"}
    q = question.casefold()
    for alias, size in aliases.items():
        if size in product.get("sizes", []) and re.search(rf"\b{re.escape(alias)}\b", q):
            return {"product":product,"size":size}
    return {"product":product,"size":None}


def get_context(question: str, verified_phone: str | None = None, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    history = history or []
    norm = re.sub(r"[^a-z0-9]", "", question.lower())
    phone_digits = re.sub(r"\D", "", verified_phone or "")
    order = next((o for o in store["orders"] if re.sub(r"[^a-z0-9]", "", o["id"].lower()) in norm), None)
    if not order and (is_order_tracking_question(question) or is_returns_topic(question)):
        for turn in reversed(history[-8:]):
            prior = re.sub(r"[^a-z0-9]", "", str(turn.get("text", "")).lower())
            order = next((o for o in store["orders"] if re.sub(r"[^a-z0-9]", "", o["id"].lower()) in prior), None)
            if order: break
    if not order and phone_digits:
        order = next((o for o in store["orders"] if re.sub(r"\D", "", o.get("phone", "")) == phone_digits), None)
    words = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if len(w) > 2}
    collection = collection_intent(question)
    query_folded = question.casefold()
    def product_score(product: dict[str, Any]) -> int:
        searchable = f"{product['name']} {product['category']} {product['audience']} {product['collection']} {product['detail']} {' '.join(product.get('aliases', []))}".casefold()
        return sum(word in searchable for word in words) + sum(3 for alias in product.get("aliases", []) if alias.casefold() in query_folded)
    ranked = sorted(store["products"], key=product_score, reverse=True)
    products = [product for product in ranked if product_score(product) > 0][:3]
    if collection == "all": products = store["products"][:12]
    elif collection: products = [product for product in store["products"] if product.get("audience") == collection][:8]
    elif recent_product_references(question, history): products = recent_product_references(question, history)
    policy = ""
    if is_returns_topic(question):
        policy_terms = words - {"refund", "return", "returns", "money", "back", "exchange", "what", "when", "how", "your", "policy", "about", "please"}
        chunks = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", store["policy"]) if part.strip()]
        scored = sorted(((sum(term in part.lower() for term in policy_terms), part) for part in chunks), key=lambda item: item[0], reverse=True)
        selected = [part for score, part in scored if score > 0][:6]
        if not selected:
            selected = [part for part in chunks if re.search(r"14.day|unused|original packaging|approval|₹\s?2,000|eligible|COD|5.?7 business", part, re.I)][:6] or chunks[:3]
        policy = " ".join(selected)[:2400]
    # Only send the minimum order fields needed to answer; never expose phone/email/payment data to a model.
    safe_order = ({key: order[key] for key in ("id", "items", "amount", "refund_amount", "status", "placed", "delivery", "requested_delivery_date", "requested_delivery_slot", "estimated_delivery_from", "estimated_delivery_to", "return_until") if key in order} if order else None)
    return {"order": safe_order, "products": products, "policy": policy, "collection":collection}


def deterministic_answer(question: str, context: dict[str, Any]) -> str:
    q = question.lower()
    tamil = tamil_or_tanglish(question)
    if context["order"]:
        o = context["order"]
        if is_returns_topic(question):
            if tamil:
                return f"**{o['id']} return தகவல்**\n- Order நிலை: {o['status']}\n- Delivered ஆன 14 நாட்களுக்குள் unused item-ஐ original packaging-இல் return செய்யலாம்.\n- ₹2,000-க்கும் குறைவான eligible return policy மூலம் auto-approve ஆகும்; ₹2,000 அல்லது அதற்கு மேல் admin approval தேவை.\n- Refund, item திரும்பப் பெற்று ஆய்வு செய்த பிறகே process செய்யப்படும்.\n\nRequest செய்ய **Orders & returns** திறக்கவும்."
            return f"**Return information for {o['id']}**\n- Order status: {o['status']}\n- Returns are accepted within 14 days of delivery for unused items in original packaging.\n- Eligible returns below ₹2,000 are auto-approved by policy; requests of ₹2,000 or more need admin approval.\n- A refund is processed only after the item is received and inspected.\n\nOpen **Orders & returns** to submit a request."
        if tamil:
            state = {"Delivered":"டெலிவரி செய்யப்பட்டது","In transit":"வழியில் உள்ளது","Refund requested":"refund request பதிவு செய்யப்பட்டுள்ளது","Order placed":"ஆர்டர் உறுதிப்படுத்தப்பட்டது"}.get(o["status"],o["status"])
            date_label="எதிர்பார்க்கப்படும் delivery" if o["status"] in ("Order placed","In transit") else "delivery date"
            payment=" Cash on delivery." if o["status"]=="Order placed" else ""
            return f"உங்கள் order {o['id']} {state}. இதில் {o['items']} உள்ளது. {date_label}: {o['delivery']}.{payment} மேலும் உதவி வேண்டுமா?"
        if o["status"] == "Order placed":
            requested=o.get("requested_delivery_date",o["delivery"])
            slot=o.get("requested_delivery_slot","")
            delivery=f"It is confirmed with cash on delivery. Your requested delivery is {requested}{' during '+slot if slot else ''}."
        elif o["status"] == "In transit":
            delivery=f"It is on the way. Estimated delivery is {o['delivery']}."
        elif o["status"] == "Delivered":
            delivery=f"It was delivered on {o['delivery']}."
        else:
            delivery=f"The latest order update is: {o['status']}."
        return f"I found order {o['id']}. It contains {o['items']}. {delivery} Is there anything else I can help with?"
    if is_order_tracking_question(question):
        if tamil:
            if db_pool is None: return "இப்போது order database இணைக்கப்படவில்லை; அதனால் order நிலையைச் சரிபார்க்க முடியவில்லை. Neon connection மீண்டும் அமைக்கப்பட்டதும், உங்கள் order ID மற்றும் checkout செய்த phone number-ஐ அனுப்புங்கள்."
            return "உங்கள் order ID மற்றும் checkout-இல் பயன்படுத்திய phone number-ஐ அனுப்புங்கள்; order நிலையைச் சரிபார்க்கிறேன்."
        if db_pool is None:
            return "Order tracking is temporarily unavailable because the Neon order database isn’t connected. Once it’s reconnected, send your order ID and the phone number used at checkout and I can look it up."
        return "Please send your order ID and the phone number used at checkout so I can look up the latest status and delivery estimate."
    if context.get("collection") == "all":
        if tamil: return "எங்களிடம் Women, Men, மற்றும் Unisex & Accessories collections உள்ளன. ஒவ்வொரு collection-லிருந்தும் சில pieces கீழே உள்ளன. எதைப் பார்க்க விரும்புகிறீர்கள்?"
        return "We have three collections: Women, Men, and Unisex & Accessories. I’ve picked a few pieces from each below. Which collection would you like to explore?"
    if context.get("collection"):
        label = "Unisex & Accessories" if context["collection"] == "Unisex" else context["collection"]
        if tamil: return f"இதோ எங்கள் {label} collection. உங்களுக்கு விருப்பமான piece-ஐத் தேர்வு செய்து size-ஐப் பார்க்கலாம்."
        return f"Here’s our {label} collection. Choose a piece below to see its colors and available sizes."
    if is_returns_topic(question):
        if tamil:
            return "**Return & refund**\n- Delivery ஆன 14 நாட்களுக்குள், unused item-ஐ original packaging-இல் return செய்யலாம்.\n- ₹2,000-க்கும் குறைவான eligible returns policy மூலம் auto-approve ஆகும்; ₹2,000 அல்லது அதற்கு மேல் admin approval தேவை.\n- Item திரும்பப் பெற்று ஆய்வு செய்த பிறகே refund process செய்யப்படும்.\n\n**Orders & returns** திறந்து order-ஐ தேர்ந்தெடுத்து request சமர்ப்பிக்கவும்."
        return "**Returns & refunds**\n- Return within 14 days of delivery, if the item is unused and in its original packaging.\n- Eligible returns below ₹2,000 are auto-approved by policy; requests of ₹2,000 or more require admin approval.\n- Refunds are processed only after the item is received and inspected.\n\nOpen **Orders & returns**, choose the order, and submit a return request."
    if any(x in q for x in ("hello", "hi", "vanakkam", "வணக்கம்")):
        if tamil: return "வணக்கம்! Order status, returns, refunds, product details பற்றி உதவுகிறேன். என்ன உதவி வேண்டும்?"
        return "Vanakkam! I can help with orders, returns, refunds, and product questions. What can I help you with today?"
    if context["products"]:
        p = context["products"][0]
        if tamil: return f"{p['name']}: {p['detail']} விலை {p['price']:,} ரூபாய். வேறு அளவு அல்லது உதவி வேண்டுமா?"
        return f"{p['name']}: {p['detail']} It is ₹{p['price']:,}. Would you like help with anything else?"
    return "I can help with order status, returns and refunds, or product details. Share your order ID (for example, TJ-48291) or tell me which product you’re asking about. [Support team available]"


async def generate_answer(question: str, context: dict[str, Any], lang: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    history = history or []
    prompt = (
        "You are Techjays ecommerce customer support. Be concise and warm. Reply in the customer's language "
        "(English, Tamil, or Tanglish); preserve order IDs. Answer only from VERIFIED CONTEXT. If it is missing, "
        "say you do not have the information and offer human support. Never invent order state, stock, delivery dates, "
        "or policies. Never claim an action completed without a confirmed tool result. Eligible returns below ₹2,000 "
        "are automatically approved by policy; returns of ₹2,000 or more require admin approval. Refund processing only "
        "happens after the item is received and inspected. Never ask for OTP, PIN, password, or full card details. "
        "Format useful answers in Markdown: a short **bold heading**, then brief bullets for steps or key facts, with normal text for a short closing. Avoid giant paragraphs, fake links, invented support forms, and source labels not present in context. "
        f"Language hint: {lang}\nRECENT CONVERSATION: {json.dumps(history[-8:], ensure_ascii=False)}\nVERIFIED CONTEXT: {json.dumps(context, ensure_ascii=False)}\nCUSTOMER: {question}"
    )
    errors: list[str] = []
    non_ascii = sum(1 for char in prompt if ord(char) > 127)
    input_estimate = math.ceil(len(prompt) / 3) + 2 * non_ascii
    if os.getenv("GROQ_API_KEY"):
        reservation = await reserve_ai_budget("groq",input_estimate,450)
        if reservation:
            try:
                async with httpx.AsyncClient(timeout=25) as client:
                    response = await client.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}, json={"model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"), "messages": [{"role":"system","content":"Follow the exact customer support constraints in the user prompt."},{"role":"user","content":prompt}], "temperature":0.2,"max_tokens":450})
                    response.raise_for_status()
                    payload = response.json()
                    usage=payload.get("usage",{})
                    await settle_ai_budget(reservation,usage.get("prompt_tokens",input_estimate),usage.get("completion_tokens",450))
                    return {"answer":payload["choices"][0]["message"]["content"],"provider":"Groq · GPT-OSS 20B","model":payload.get("model",os.getenv("GROQ_MODEL","openai/gpt-oss-20b"))}
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                errors.append(f"Groq fallback ({type(exc).__name__})")
        else:
            errors.append("Monthly AI budget limit reached for Groq")
    else:
        errors.append("Groq key not configured")
    if os.getenv("GEMINI_API_KEY"):
        reservation = await reserve_ai_budget("gemini",input_estimate,450)
        if reservation:
            try:
                model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", headers={"x-goog-api-key":os.environ["GEMINI_API_KEY"]}, json={"systemInstruction":{"parts":[{"text":"Follow the exact customer support constraints in the prompt."}]},"contents":[{"role":"user","parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.2,"maxOutputTokens":450}})
                    response.raise_for_status()
                    payload = response.json()
                    usage=payload.get("usageMetadata",{})
                    await settle_ai_budget(reservation,usage.get("promptTokenCount",input_estimate),usage.get("candidatesTokenCount",450)+usage.get("thoughtsTokenCount",0))
                    answer = "".join(p.get("text","") for p in payload["candidates"][0]["content"]["parts"])
                    return {"answer":answer,"provider":"Gemini · Flash fallback","model":model}
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                errors.append(f"Gemini fallback ({type(exc).__name__})")
        else:
            errors.append("Monthly AI budget limit reached for Gemini")
    return {"answer":deterministic_answer(question,context),"provider":"Local demo mode","model":"deterministic fallback","warning":"; ".join(errors)}


def maybe_create_refund(question: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """Explicit, deterministic side effect; model output never initiates a refund."""
    has_refund_topic = is_returns_topic(question)
    explicit_request = re.search(r"\b(request|start|initiate|submit|place|process|want|need|would like|could you|can you|please|confirm)\b.{0,55}\b(refund|return|money back)\b|\b(?:i'd like|i would like|i want|i need|can i get|can i request)\b.{0,55}\b(refund|return|money back)\b|\b(?:refund|return)\b.{0,25}\b(?:this|the|my)\s+(?:order|item)\b|\b(?:refund|return)\s+(?:venum|pannanum|pannunga|seyyanum|seyyunga|please)\b|\b(?:venum|pannanum|pannunga|seyyanum|seyyunga)\b.{0,25}\b(?:refund|return)\b|(?:திருப்பி.*(?:தர|வேண்டும்|செய்ய)|(?:பணம்|தொகை).*(?:திரும்ப|வேண்டும்)|ரீஃபண்ட்.*(?:வேண்டும்|செய்))", question, re.I)
    if not has_refund_topic or not explicit_request:
        return None
    matched = context.get("order")
    if not matched:
        return None
    order = next((o for o in store["orders"] if o["id"] == matched["id"]), None)
    if not order: return None
    refund_amount=order.get("refund_amount",order["amount"])
    if order["status"] not in ("Delivered", "Refund requested"):
        return {"type":"refund_blocked","message":"This order has not been delivered yet, so a return cannot be requested."}
    previous = next((r for r in store["refunds"] if r["order_id"] == order["id"] and r["status"] not in ("Declined",)), None)
    if previous:
        if previous["status"] == "Pending executive approval":
            return {"type":"approval_required","message":f"Your ₹{refund_amount:,} request for {order['id']} is waiting for a support executive’s approval."}
        return {"type":"refund_existing","message":f"A refund request for {order['id']} is already in progress ({previous['status'].lower()})."}
    delivered = date.fromisoformat(order["delivery"])
    today_in_india = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
    if (today_in_india - delivered).days > 14:
        return {"type":"refund_expired","message":"The 14-day return window has passed. I can connect you with the support team to review this."}
    condition_ok = re.search(r"\b(unused|unworn|not used|not worn)\b|(?:use|wear|try) pann(?:ala|avillai)|பயன்படுத்தவில்லை|அணியவில்லை", question, re.I)
    packaging_ok = re.search(r"original (?:packaging|box|package)|(?:அசல்|original).*(?:பெட்டி|பேக்கேஜிங்|பேக்கேஜ்|box)", question, re.I)
    condition_confirmed = condition_ok and packaging_ok
    if not condition_confirmed:
        return {"type":"refund_confirmation_needed","message":f"Before I submit a return for {order['id']}, please confirm the item is unused and in its original packaging. The return window is open until {order['return_until']}."}
    requires_admin = refund_amount >= 2000
    status = "Pending executive approval" if requires_admin else "Approved"
    request = {"id":f"RF-{uuid.uuid4().hex[:6].upper()}","order_id":order["id"],"customer":order["customer"],"amount":refund_amount,"reason":"Customer requested a return/refund","requested":utcnow(),"status":status}
    store["refunds"].insert(0,request)
    if requires_admin:
        store["approvals"].insert(0,{**request,"status":"Pending review"})
        message=f"I’ve submitted your ₹{refund_amount:,} return/refund request for {order['id']}. It is waiting for admin approval and will only be processed after the item is received and inspected."
        kind="approval_required"
        order["status"] = "Return requested"
    else:
        message=f"Your ₹{refund_amount:,} return request for {order['id']} has been automatically approved under the low-value return policy. The refund will be processed after the item is received and inspected."
        kind="refund_auto_approved"
        order["status"] = "Return approved"
    save_store(store)
    return {"type":kind,"message":message,"refund_id":request["id"]}


def localize_refund_action(question: str, action: dict[str, Any], order: dict[str, Any]) -> str:
    if not tamil_or_tanglish(question): return action["message"]
    order_id, amount = order["id"], f"₹{order.get('refund_amount',order['amount']):,}"
    kind = action["type"]
    if kind == "refund_confirmation_needed":
        return f"{order_id}-க்கு return தொடங்க, பொருள் பயன்படுத்தப்படவில்லை என்றும் original packaging-இல் உள்ளது என்றும் உறுதிப்படுத்துங்கள். Return window {order['return_until']} வரை திறந்திருக்கும்."
    if kind == "approval_required":
        return f"{order_id}-க்கான {amount} refund request காத்திருக்கிறது. Payment செயலுக்கு முன் support executive approval தேவை."
    if kind == "refund_auto_approved":
        return f"{order_id}-க்கான {amount} return குறைந்த தொகைக்கான policy அடிப்படையில் தானாக approve செய்யப்பட்டது. பொருள் திரும்பப் பெற்று ஆய்வு செய்த பிறகு refund process செய்யப்படும்."
    if kind == "refund_requested":
        return f"{order_id}-க்கான {amount} refund request சமர்ப்பிக்கப்பட்டது. Return ஆய்வுக்குப் பிறகு payment processing-க்கு செல்லும்."
    if kind == "refund_existing":
        return f"{order_id}-க்கான refund request ஏற்கனவே செயல்பாட்டில் உள்ளது."
    if kind == "refund_blocked":
        return f"{order_id} இன்னும் delivery ஆகவில்லை; அதனால் return request செய்ய முடியாது."
    if kind == "refund_expired":
        return "14 நாள் return காலம் முடிந்துள்ளது. Support team-ஐ தொடர்புகொள்ள உதவுகிறேன்."
    return action["message"]


async def respond(question: str, phone: str | None, channel: str, lang: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    history = history or []
    context = get_context(question, phone if channel == "whatsapp" else None, history)
    cart_request = is_add_to_cart_request(question) and channel == "web"
    cart_target = resolve_cart_target(question, context, history) if cart_request else None
    action = maybe_create_refund(question,context)
    if action and context.get("order"):
        full_order=next((o for o in store["orders"] if o["id"]==context["order"]["id"]),None)
        if full_order: action["message"]=localize_refund_action(question,action,full_order)
    # Refund submissions are explicit workflow outcomes; skip paid generation and return the verified result.
    if cart_request and cart_target and cart_target["size"]:
        p = cart_target["product"]
        generated={"answer":f"**Added to your bag**\n- {p['name']} · {p.get('color','')} · size {cart_target['size']}\n\nYour bag is updated.","provider":"Storefront action · confirmed","model":"cart-add-v1"}
    elif cart_request and cart_target:
        p = cart_target["product"]
        generated={"answer":f"**Choose a size**\n- {p['name']} is available in {', '.join(p.get('sizes',[]))}.\n\nChoose your size below to add it to your bag.","provider":"Catalogue · deterministic","model":"cart-size-selection-v1"}
    elif cart_request:
        generated={"answer":"**Which piece would you like?**\n- Tap a product card above, or tell me the product name.\n- If I just showed several options, please name the one you want to add.","provider":"Catalogue · deterministic","model":"cart-target-selection-v1"}
    elif action:
        generated={"answer":action["message"],"provider":"Workflow · deterministic","model":"refund-policy-v1"}
    elif is_returns_topic(question):
        generated={"answer":deterministic_answer(question,context),"provider":"Returns policy · deterministic","model":"returns-policy-v1"}
    elif context.get("collection"):
        generated={"answer":deterministic_answer(question,context),"provider":"Catalogue · deterministic","model":"collection-search-v1"}
    elif is_order_tracking_question(question):
        generated={"answer":deterministic_answer(question,context),"provider":"Orders · deterministic","model":"order-lookup-v1"}
    else:
        generated=await generate_answer(question,context,lang,history)
    sources = []
    if context.get("order"): sources.append("Orders database")
    if context.get("products"): sources.append("Product catalogue")
    if is_returns_topic(question): sources.append("Returns policy")
    if action:
        generated["answer"] = action["message"]
        generated["action"] = action["type"]
        if action.get("refund_id"): generated["refund_id"] = action["refund_id"]
    ui_actions = [{"type":"orders","label":"Open Orders & returns"}] if is_returns_topic(question) or is_order_tracking_question(question) else []
    if cart_request and cart_target:
        ui_actions.append({"type":"add_to_cart" if cart_target["size"] else "choose_size","label":"Add to bag" if cart_target["size"] else "Choose size & add","product":cart_target["product"],"size":cart_target["size"]})
    recommendations = context["products"] if context.get("collection") or recent_product_references(question, history) else []
    conversation = {"id":str(uuid.uuid4()),"phone":phone,"channel":channel,"text":question,"answer":generated["answer"],"provider":generated["provider"],"model":generated["model"],"sources":sources,"recommendations":recommendations,"ui_actions":ui_actions,"action":generated.get("action"),"warning":generated.get("warning"),"at":utcnow(),"trace":{"retrieval":{"order_id":context.get("order",{}).get("id") if context.get("order") else None,"product_hits":[p["name"] for p in context["products"]],"policy_used":bool(sources and "Returns policy" in sources)},"action":action}}
    store["conversations"].insert(0,conversation)
    store["conversations"] = store["conversations"][:500]
    save_store(store)
    return conversation


async def transcribe_audio(audio: bytes, mime: str) -> str:
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(503,"Voice transcription is not configured. Add GROQ_API_KEY.")
    reservation = await reserve_ai_budget("whisper",audio_seconds=900)
    if not reservation:
        raise HTTPException(429,"The monthly AI budget limit has been reached. Please type your message or contact support.")
    files = {"file":("voice.webm",audio,mime)}
    data = {"model":"whisper-large-v3-turbo","response_format":"json"}
    async with httpx.AsyncClient(timeout=90) as client:
        try:
            response = await client.post("https://api.groq.com/openai/v1/audio/transcriptions",headers={"Authorization":f"Bearer {os.environ['GROQ_API_KEY']}"},files=files,data=data)
            response.raise_for_status()
            payload=response.json()
            duration=float(payload.get("duration") or 900)
            await settle_ai_budget(reservation,audio_seconds=duration)
            return payload.get("text","")
        except httpx.HTTPError as exc:
            raise HTTPException(502,"Voice transcription failed; please try typing your message.") from exc


async def whatsapp_send(phone: str, text: str) -> dict[str, Any]:
    provider = os.getenv("WHATSAPP_PROVIDER", "openwa").strip().lower()
    recipient=re.sub(r"\D","",phone)
    if len(recipient)==10: recipient="91"+recipient
    elif len(recipient)==11 and recipient.startswith("0"): recipient="91"+recipient[1:]
    if provider == "openwa":
        api_key = os.getenv("OPENWA_API_KEY", "").strip()
        session_id = os.getenv("OPENWA_SESSION_ID", "").strip()
        if not api_key or not session_id:
            return {"sent":False,"reason":"OpenWA is not configured. Set OPENWA_API_KEY and OPENWA_SESSION_ID."}
        base = os.getenv("OPENWA_BASE_URL", "http://127.0.0.1:2785").rstrip("/")
        api_base = base if base.endswith("/api") else f"{base}/api"
        chat_id = phone.strip() if "@" in phone else f"{recipient}@c.us"
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                response = await client.post(f"{api_base}/sessions/{session_id}/messages/send-text", headers={"X-API-Key":api_key}, json={"chatId":chat_id,"text":text})
                if response.is_error:
                    return {"sent":False,"reason":f"OpenWA returned HTTP {response.status_code}. Check its session and API key."}
                data=response.json()
                return {"sent":True,"provider":"OpenWA","message_id":data.get("messageId")}
            except httpx.HTTPError:
                return {"sent":False,"reason":"Could not reach the OpenWA gateway. Check that it is running at OPENWA_BASE_URL."}
    if provider != "meta":
        return {"sent":False,"reason":"WHATSAPP_PROVIDER must be openwa or meta."}
    token, phone_id = os.getenv("WHATSAPP_ACCESS_TOKEN"), os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_id:
        return {"sent":False,"reason":"WhatsApp Cloud API credentials are not configured."}
    version = os.getenv("WHATSAPP_API_VERSION","v23.0")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"https://graph.facebook.com/{version}/{phone_id}/messages",headers={"Authorization":f"Bearer {token}"},json={"messaging_product":"whatsapp","to":recipient,"type":"text","text":{"body":text}})
        if response.is_error: return {"sent":False,"reason":f"WhatsApp API returned {response.status_code}."}
        return {"sent":True,"message_id":response.json().get("messages",[{}])[0].get("id")}


async def process_whatsapp(payload: dict[str, Any]) -> None:
    for entry in payload.get("entry",[]):
        for change in entry.get("changes",[]):
            value = change.get("value",{})
            contacts = {c.get("wa_id"):c.get("profile",{}).get("name") for c in value.get("contacts",[])}
            for message in value.get("messages",[]):
                phone = message.get("from")
                try:
                    if message.get("type") == "text":
                        question = message.get("text",{}).get("body","")
                    elif message.get("type") == "audio":
                        media_id = message.get("audio",{}).get("id")
                        question = await fetch_whatsapp_audio(media_id) if media_id else ""
                    else:
                        question = ""
                    if not question: question = "I received a message type that needs a human support agent. Please connect me to support."
                    result = await respond(question,phone,"whatsapp","auto")
                    await whatsapp_send(phone,result["answer"])
                except Exception as exc:
                    print(f"WhatsApp message processing error: {exc}")


async def process_openwa_event(payload: dict[str, Any], idempotency_key: str) -> None:
    try:
        if payload.get("event") != "message.received":
            return
        message = payload.get("data") or {}
        if message.get("fromMe") or message.get("isGroup"):
            return
        sender = str(message.get("from") or message.get("chatId") or "")
        phone = sender.split("@", 1)[0]
        if not re.sub(r"\D", "", phone):
            return
        question = str(message.get("body") or "").strip()
        if not question:
            question = "I received your message. I can currently help with text questions here. Please type your question and I’ll help."
        result = await respond(question, phone, "whatsapp", "auto")
        delivery = await whatsapp_send(sender, result["answer"])
        if not delivery.get("sent"):
            print(f"OpenWA reply failed for inbound message: {delivery.get('reason', 'unknown error')}")
            return
        processed = store.setdefault("openwa_processed", [])
        processed.append(idempotency_key)
        store["openwa_processed"] = processed[-2000:]
        save_store(store)
    except Exception as exc:
        print(f"OpenWA message processing error: {exc}")
    finally:
        openwa_inflight.discard(idempotency_key)


async def fetch_whatsapp_audio(media_id: str) -> str:
    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not token or not os.getenv("GROQ_API_KEY"):
        return "I received your voice note. Please type your question or try again later."
    async with httpx.AsyncClient(timeout=35) as client:
        meta = await client.get(f"https://graph.facebook.com/{os.getenv('WHATSAPP_API_VERSION','v23.0')}/{media_id}",headers={"Authorization":f"Bearer {token}"})
        meta.raise_for_status()
        media = await client.get(meta.json()["url"],headers={"Authorization":f"Bearer {token}"})
        media.raise_for_status()
        return await transcribe_audio(media.content,meta.json().get("mime_type","audio/ogg"))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok":True,"providers":provider_status(),"database":{"configured":bool(DATABASE_URL),"connected":db_pool is not None}}


def require_neon() -> None:
    if not DATABASE_URL:
        raise HTTPException(503, "Order tracking requires NEON_DATABASE_URL. Configure Neon before managing orders.")
    if db_pool is None:
        raise HTTPException(503, "Neon is configured but not connected. Check the backend startup log.")


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_urlsafe(18)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310_000).hex()
    return f"pbkdf2_sha256$310000${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256": return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def sign_session(user: dict[str, Any]) -> str:
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "SESSION_SECRET must be set to a random value of at least 32 characters.")
    payload = {**user, "exp": int(time.time()) + 8 * 60 * 60}
    encoded = base64.urlsafe_b64encode(json_text(payload).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def read_session(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get("ff_session", "")
    secret = os.getenv("SESSION_SECRET", "")
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, supplied): return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return payload if payload.get("exp", 0) > time.time() else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def set_session_cookie(response: Response, user: dict[str, Any]) -> None:
    response.set_cookie("ff_session", sign_session(user), httponly=True, secure=os.getenv("APP_ENV") == "production", samesite="lax", max_age=8 * 60 * 60, path="/api")


def require_admin(request: Request) -> dict[str, Any]:
    session = read_session(request)
    if not session or session.get("role") != "admin":
        raise HTTPException(401, "Administrator sign-in is required.")
    return session


def require_customer(request: Request) -> dict[str, Any]:
    session = read_session(request)
    if not session or session.get("role") != "customer":
        raise HTTPException(401, "Sign in to your customer account to manage your orders.")
    return session


@app.post("/api/auth/register")
async def register_account(body: RegisterInput, response: Response) -> dict[str, Any]:
    require_neon()
    email = body.email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(422, "Enter a valid email address.")
    phone = normalize_order_phone(body.phone)
    if not 11 <= len(phone) <= 15:
        raise HTTPException(422, "Enter a valid phone number including its country code if needed.")
    async with db_pool.connection() as conn:
        try:
            row = await (await conn.execute("INSERT INTO user_accounts(full_name,email,phone_e164,password_hash) VALUES (%s,%s,%s,%s) RETURNING id", (body.name.strip(), email, phone, hash_password(body.password)))).fetchone()
        except Exception as exc:
            if "unique" in str(exc).lower(): raise HTTPException(409, "An account already exists for that email.") from exc
            raise
    user = {"id":row[0],"email":email,"name":body.name.strip(),"phone":phone,"role":"customer"}
    set_session_cookie(response, user)
    return {"user":user}


@app.post("/api/auth/login")
async def login_account(body: LoginInput, response: Response) -> dict[str, Any]:
    email = body.email.strip().lower()
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
    if admin_email and email == admin_email:
        if not admin_hash or not verify_password(body.password, admin_hash):
            raise HTTPException(401, "Email or password is incorrect.")
        user = {"id":"admin","email":admin_email,"name":"Store administrator","role":"admin"}
    else:
        require_neon()
        async with db_pool.connection() as conn:
            row = await (await conn.execute("SELECT id,full_name,password_hash,phone_e164 FROM user_accounts WHERE email=%s AND role='customer'", (email,))).fetchone()
        if not row or not verify_password(body.password, row[2]):
            raise HTTPException(401, "Email or password is incorrect.")
        user = {"id":row[0],"email":email,"name":row[1],"phone":row[3],"role":"customer"}
    set_session_cookie(response, user)
    return {"user":user}


@app.get("/api/auth/me")
async def current_account(request: Request) -> dict[str, Any]:
    session = read_session(request)
    if not session: return {"user":None}
    user = {key:session[key] for key in ("id","email","name","phone","role") if key in session}
    if session.get("role") == "customer" and db_pool:
        async with db_pool.connection() as conn:
            row = await (await conn.execute("SELECT full_name,email,phone_e164 FROM user_accounts WHERE id=%s AND role='customer'", (int(session["id"]),))).fetchone()
        if row:
            user.update({"name":row[0],"email":row[1],"phone":row[2]})
    return {"user":user}


@app.post("/api/auth/logout")
async def logout_account(response: Response) -> dict[str, bool]:
    response.delete_cookie("ff_session", path="/api", httponly=True, secure=os.getenv("APP_ENV") == "production", samesite="lax")
    return {"ok":True}


async def persist_order_to_neon(order: dict[str, Any], idempotency_key: str) -> None:
    if not DATABASE_URL:
        raise HTTPException(503, "Order tracking requires NEON_DATABASE_URL. Configure Neon before accepting orders.")
    if db_pool is None:
        raise HTTPException(503, "Neon database is configured but unavailable; order was not accepted. Check backend logs.")
    address = {key: order.get(key, "") for key in ("address", "city", "state", "pincode")}
    estimate_from = date.fromisoformat(order.get("estimated_delivery_from", order["delivery"]))
    estimate_to = date.fromisoformat(order.get("estimated_delivery_to", order["delivery"]))
    snapshot = dict(order)
    async with db_pool.connection() as conn:
        async with conn.transaction():
            customer = await (await conn.execute(
                "INSERT INTO customers (full_name,email,phone_e164) VALUES (%s,%s,%s) "
                "ON CONFLICT (phone_e164) DO UPDATE SET full_name=EXCLUDED.full_name,email=EXCLUDED.email,updated_at=now() RETURNING id",
                (order["customer"], order["email"], order["phone"]),
            )).fetchone()
            inserted = await (await conn.execute(
                "INSERT INTO orders (order_number,customer_id,status,payment_method,payment_status,subtotal_paise,shipping_paise,total_paise,shipping_address,placed_at,estimated_delivery_from,estimated_delivery_to,requested_delivery_date,requested_delivery_slot,promised_delivery_date,return_window_ends_at,idempotency_key,notification_status,metadata) "
                "VALUES (%s,%s,%s,%s,'Pending',%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) RETURNING id",
                (order["id"], customer[0], order["status"], order["payment"], order["subtotal"]*100, order["shipping"]*100, order["amount"]*100,
                 json_text(address), datetime.fromisoformat(order["placed"]).replace(tzinfo=timezone(timedelta(hours=5, minutes=30))),
                 estimate_from, estimate_to, date.fromisoformat(order["requested_delivery_date"]), order["requested_delivery_slot"], date.fromisoformat(order["delivery"]), date.fromisoformat(order["return_until"]),
                 idempotency_key, json_text(order.get("notifications", {})), json_text({"order_snapshot": snapshot})),
            )).fetchone()
            for item in order["line_items"]:
                await conn.execute(
                    "INSERT INTO order_items (order_id,product_id,product_name,color,size,quantity,unit_price_paise,line_total_paise,image_url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (inserted[0], item["product_id"], item["name"], item.get("color", ""), item["size"], item["quantity"], item["unit_price"]*100, item["line_total"]*100, item.get("image")),
                )
            await conn.execute("INSERT INTO order_events (order_id,event_type,to_status,description) VALUES (%s,'order_placed',%s,%s)", (inserted[0], order["status"], "COD order placed through website"))


async def load_neon_orders() -> list[dict[str, Any]]:
    if db_pool is None:
        raise HTTPException(503, "Order tracking requires a live Neon database connection.")
    async with db_pool.connection() as conn:
        rows = await (await conn.execute("SELECT metadata->'order_snapshot',status,estimated_delivery_from,estimated_delivery_to,notification_status,delivered_at,return_window_ends_at FROM orders ORDER BY placed_at DESC LIMIT 1000")).fetchall()
    result = []
    for snapshot, status, estimate_from, estimate_to, notifications, delivered_at, return_until in rows:
        order = dict(parse_json(snapshot) or {})
        notifications = parse_json(notifications)
        order.update({"status":status,"estimated_delivery_from":estimate_from.isoformat(),"estimated_delivery_to":estimate_to.isoformat(),"notifications":notifications or order.get("notifications", {})})
        if delivered_at:
            order["delivered_at"] = delivered_at.isoformat()
        if return_until:
            order["return_until"] = return_until.isoformat()
        result.append(order)
    return result


async def update_neon_notifications(order_id: str, notifications: dict[str, Any]) -> None:
    if db_pool is None:
        return
    async with db_pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute("SELECT id,metadata FROM orders WHERE order_number=%s FOR UPDATE", (order_id,))).fetchone()
            if row:
                metadata = parse_json(row[1]) or {}
                metadata.setdefault("order_snapshot", {})["notifications"] = notifications
                await conn.execute("UPDATE orders SET notification_status=%s::jsonb,metadata=%s::jsonb WHERE id=%s", (json_text(notifications), json_text(metadata), row[0]))


async def find_neon_order_by_idempotency(key: str) -> dict[str, Any] | None:
    if db_pool is None:
        raise HTTPException(503, "Order tracking requires a live Neon database connection.")
    async with db_pool.connection() as conn:
        row = await (await conn.execute("SELECT metadata->'order_snapshot',status,estimated_delivery_from,estimated_delivery_to,notification_status FROM orders WHERE idempotency_key=%s", (key,))).fetchone()
    if not row:
        return None
    snapshot, status, estimate_from, estimate_to, notifications = row
    order = dict(parse_json(snapshot) or {})
    order.update({"status":status,"estimated_delivery_from":estimate_from.isoformat(),"estimated_delivery_to":estimate_to.isoformat(),"notifications":parse_json(notifications) or {}})
    return order


async def set_neon_order_status(order: dict[str, Any], new_status: str, event_type: str, description: str, extra: dict[str, Any] | None = None) -> None:
    if db_pool is None:
        return
    async with db_pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute("SELECT id,status,metadata FROM orders WHERE order_number=%s FOR UPDATE", (order["id"],))).fetchone()
            if not row:
                raise HTTPException(404, "Order not found in Neon.")
            previous, metadata = row[1], parse_json(row[2]) or {}
            metadata.setdefault("order_snapshot", {}).update(order)
            metadata["order_snapshot"]["status"] = new_status
            if extra:
                metadata["order_snapshot"].update(extra)
            if new_status == "Cancelled":
                await conn.execute("UPDATE orders SET status=%s,cancelled_at=now(),cancellation_requested_at=now(),cancellation_reason=%s,metadata=%s::jsonb WHERE id=%s", (new_status, description, json_text(metadata), row[0]))
            elif new_status == "Delivered":
                await conn.execute("UPDATE orders SET status=%s,delivered_at=now(),return_window_ends_at=(CURRENT_DATE + 14),metadata=%s::jsonb WHERE id=%s", (new_status, json_text(metadata), row[0]))
            else:
                await conn.execute("UPDATE orders SET status=%s,metadata=%s::jsonb WHERE id=%s", (new_status, json_text(metadata), row[0]))
            await conn.execute("INSERT INTO order_events(order_id,event_type,from_status,to_status,description) VALUES (%s,%s,%s,%s,%s)", (row[0], event_type, previous, new_status, description))


async def create_neon_return(order: dict[str, Any], reason: str, notes: str, executive_approval: bool) -> str | None:
    if db_pool is None:
        return None
    request_number = f"RT-{uuid.uuid4().hex[:8].upper()}"
    status = "Pending executive approval" if executive_approval else "Approved"
    async with db_pool.connection() as conn:
        async with conn.transaction():
            row = await (await conn.execute("SELECT id,status,metadata FROM orders WHERE order_number=%s FOR UPDATE", (order["id"],))).fetchone()
            if not row:
                raise HTTPException(404, "Order not found in Neon.")
            exists = await (await conn.execute("SELECT request_number FROM return_requests WHERE order_id=%s AND status NOT IN ('Rejected','Refunded') LIMIT 1", (row[0],))).fetchone()
            if exists:
                return exists[0]
            created = await (await conn.execute("INSERT INTO return_requests(request_number,order_id,status,reason,customer_notes,refund_amount_paise) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (request_number,row[0],status,reason,notes,int(order.get("refund_amount",order["amount"])*100)))).fetchone()
            items = await (await conn.execute("SELECT id,quantity FROM order_items WHERE order_id=%s", (row[0],))).fetchall()
            for item_id, quantity in items:
                await conn.execute("INSERT INTO return_request_items(return_request_id,order_item_id,quantity,reason) VALUES (%s,%s,%s,%s)", (created[0],item_id,quantity,reason))
            metadata = parse_json(row[2]) or {}
            next_order_status = "Return requested" if executive_approval else "Return approved"
            metadata.setdefault("order_snapshot", {})["status"] = next_order_status
            await conn.execute("UPDATE orders SET status=%s,metadata=%s::jsonb WHERE id=%s", (next_order_status,json_text(metadata),row[0]))
            event_type = "return_requested" if executive_approval else "return_auto_approved"
            await conn.execute("INSERT INTO order_events(order_id,event_type,from_status,to_status,description) VALUES (%s,%s,%s,%s,%s)", (row[0],event_type,row[1],next_order_status,f"{request_number}: {reason}"))
    return request_number


async def hydrate_neon_store() -> None:
    if db_pool is None:
        return
    async with db_pool.connection() as conn:
        rows = await (await conn.execute("SELECT metadata->'order_snapshot',order_number,idempotency_key,status,estimated_delivery_from,estimated_delivery_to,notification_status,delivered_at,return_window_ends_at FROM orders ORDER BY placed_at DESC LIMIT 1000")).fetchall()
    stored_ids = {order["id"] for order in store["orders"]}
    for snapshot, order_number, key, status, estimate_from, estimate_to, notifications, delivered_at, return_until in rows:
        order = dict(parse_json(snapshot) or {})
        notifications = parse_json(notifications)
        order.update({"id":order_number,"status":status,"estimated_delivery_from":estimate_from.isoformat(),"estimated_delivery_to":estimate_to.isoformat(),"notifications":notifications or order.get("notifications", {})})
        if delivered_at:
            order["delivered_at"] = delivered_at.isoformat()
        if return_until:
            order["return_until"] = return_until.isoformat()
        if order_number not in stored_ids:
            store["orders"].insert(0, order)
            stored_ids.add(order_number)
        if key:
            store.setdefault("checkout_keys", {})[key] = order_number
    async with db_pool.connection() as conn:
        returns = await (await conn.execute("SELECT r.request_number,o.order_number,r.status,r.reason,r.requested_at,r.refund_amount_paise,o.metadata->'order_snapshot' FROM return_requests r JOIN orders o ON o.id=r.order_id ORDER BY r.requested_at DESC LIMIT 500")).fetchall()
    store["refunds"] = []
    store["approvals"] = []
    for request_number, order_number, status, reason, requested_at, amount, snapshot in returns:
        snapshot = parse_json(snapshot) or {}
        record = {"id":request_number,"order_id":order_number,"customer":snapshot.get("customer","Customer"),"amount":(amount or 0)//100,"reason":reason,"requested":requested_at.isoformat(),"status":status}
        store["refunds"].append(record)
        if status == "Pending executive approval":
            store["approvals"].append({**record,"status":"Pending review"})


@app.get("/api/catalog")
async def catalog() -> dict[str, Any]:
    audiences={p.get("audience","Unisex") for p in store["products"]}
    return {"products":store["products"],"collections":[{"id":"Women","name":"Women","count":sum(p.get("audience")=="Women" for p in store["products"])},{"id":"Men","name":"Men","count":sum(p.get("audience")=="Men" for p in store["products"])},{"id":"Unisex","name":"Unisex & Accessories","count":sum(p.get("audience")=="Unisex" for p in store["products"])}],"audiences":sorted(audiences)}


def normalize_order_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    return digits


def customer_order(order_id: str, phone: str) -> dict[str, Any]:
    order = next((item for item in store["orders"] if item["id"].lower() == order_id.lower()), None)
    if not order or not hmac.compare_digest(normalize_order_phone(order.get("phone", "")), normalize_order_phone(phone)):
        raise HTTPException(404, "Order not found. Check the order number and phone number.")
    return order


@app.get("/api/orders/{order_id}")
async def get_order(order_id: str, phone: str) -> dict[str, Any]:
    require_neon()
    if db_pool:
        orders = await load_neon_orders()
        order = next((item for item in orders if item["id"].lower() == order_id.lower()), None)
    else:
        order = next((item for item in store["orders"] if item["id"].lower() == order_id.lower()), None)
    if not order or not hmac.compare_digest(normalize_order_phone(order.get("phone", "")), normalize_order_phone(phone)):
        raise HTTPException(404, "Order not found. Check the order number and phone number.")
    if db_pool:
        async with db_pool.connection() as conn:
            rows = await (await conn.execute("SELECT event_type,from_status,to_status,description,occurred_at FROM order_events e JOIN orders o ON o.id=e.order_id WHERE o.order_number=%s ORDER BY occurred_at DESC", (order["id"],))).fetchall()
            returns = await (await conn.execute("SELECT request_number,status,reason,requested_at,refund_amount_paise FROM return_requests r JOIN orders o ON o.id=r.order_id WHERE o.order_number=%s ORDER BY requested_at DESC", (order["id"],))).fetchall()
        order["events"] = [{"type":r[0],"from_status":r[1],"status":r[2],"description":r[3],"at":r[4].isoformat()} for r in rows]
        order["returns"] = [{"id":r[0],"status":r[1],"reason":r[2],"requested_at":r[3].isoformat(),"refund_amount":(r[4] or 0)//100} for r in returns]
    return {"order":order}


async def account_order_records(user_id: int, order_id: str | None = None) -> list[dict[str, Any]]:
    require_neon()
    async with db_pool.connection() as conn:
        where = "AND o.order_number=%s" if order_id else ""
        params = (user_id, order_id) if order_id else (user_id,)
        rows = await (await conn.execute(
            "SELECT o.metadata->'order_snapshot',o.order_number,o.status,o.estimated_delivery_from,o.estimated_delivery_to,o.notification_status,o.delivered_at,o.return_window_ends_at "
            "FROM user_accounts u JOIN customers c ON lower(c.email)=lower(u.email) AND c.phone_e164=u.phone_e164 "
            "JOIN orders o ON o.customer_id=c.id WHERE u.id=%s " + where + " ORDER BY o.placed_at DESC LIMIT 100", params
        )).fetchall()
    result = []
    for snapshot, number, status, estimate_from, estimate_to, notifications, delivered, return_until in rows:
        order = dict(parse_json(snapshot) or {})
        order.update({"id":number,"status":status,"estimated_delivery_from":estimate_from.isoformat(),"estimated_delivery_to":estimate_to.isoformat(),"notifications":parse_json(notifications) or {}})
        if delivered: order["delivered_at"] = delivered.isoformat()
        if return_until: order["return_until"] = return_until.isoformat()
        result.append(order)
    return result


@app.get("/api/account/orders")
async def account_orders(request: Request) -> dict[str, Any]:
    session = require_customer(request)
    return {"orders":await account_order_records(int(session["id"]))}


@app.post("/api/account/orders/{order_id}/cancel")
async def account_cancel_order(order_id: str, body: AccountCancelInput, request: Request) -> dict[str, Any]:
    session = require_customer(request)
    order = next(iter(await account_order_records(int(session["id"]), order_id)), None)
    if not order: raise HTTPException(404, "That order was not found in your account.")
    if order["status"] not in ("Order placed", "Confirmed", "Processing"):
        raise HTTPException(409, f"This order cannot be cancelled while it is {order['status']}.")
    order.update({"status":"Cancelled","cancellation_reason":body.reason.strip(),"cancelled_at":utcnow()})
    await set_neon_order_status(order,"Cancelled","order_cancelled",body.reason.strip())
    return {"order_id":order_id,"status":"Cancelled","message":"Order cancellation recorded."}


@app.post("/api/account/orders/{order_id}/returns")
async def account_return_order(order_id: str, body: AccountReturnInput, request: Request) -> dict[str, Any]:
    session = require_customer(request)
    order = next(iter(await account_order_records(int(session["id"]), order_id)), None)
    if not order: raise HTTPException(404, "That order was not found in your account.")
    if order["status"] != "Delivered": raise HTTPException(409,"A return can be requested after the order is delivered.")
    delivered = date.fromisoformat(order.get("delivered_at",order["delivery"])[:10])
    deadline = date.fromisoformat(order.get("return_until",(delivered+timedelta(days=14)).isoformat()))
    today = (datetime.now(timezone.utc)+timedelta(hours=5,minutes=30)).date()
    if today > deadline: raise HTTPException(409,f"The return window ended on {deadline.isoformat()}.")
    if not body.unused or not body.original_packaging: raise HTTPException(422,"Confirm that the item is unused and in its original packaging.")
    amount = int(order.get("refund_amount",order["amount"]))
    requires_admin = amount >= 2000
    request_number=await create_neon_return(order,body.reason.strip(),"Customer confirmed item condition in account.",requires_admin)
    status = "Pending executive approval" if requires_admin else "Approved"
    message = "Return request submitted. It is waiting for admin approval." if requires_admin else "Return automatically approved under the low-value policy. Refund processing follows receipt and inspection."
    return {"return":{"id":request_number,"order_id":order_id,"status":status},"message":message}


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(order_id: str, body: CancelOrderInput, request: Request) -> dict[str, Any]:
    require_neon()
    session = require_customer(request)
    if normalize_order_phone(session.get("phone", "")) != normalize_order_phone(body.phone):
        raise HTTPException(403, "Sign in with the account phone number used for this order.")
    order = customer_order(order_id, body.phone)
    if order.get("email", "").lower() != session.get("email", "").lower(): raise HTTPException(404, "Order not found in your account.")
    if order["status"] not in ("Order placed", "Confirmed", "Processing"):
        raise HTTPException(409, f"This order cannot be cancelled while it is {order['status']}.")
    order["status"] = "Cancelled"
    order["cancellation_reason"] = body.reason.strip()
    order["cancelled_at"] = utcnow()
    await set_neon_order_status(order, "Cancelled", "order_cancelled", body.reason.strip())
    save_store(store)
    return {"order_id":order["id"],"status":order["status"],"message":"Order cancellation recorded."}


@app.post("/api/orders/{order_id}/returns")
async def request_return(order_id: str, body: ReturnOrderInput, request: Request) -> dict[str, Any]:
    require_neon()
    session = require_customer(request)
    if normalize_order_phone(session.get("phone", "")) != normalize_order_phone(body.phone):
        raise HTTPException(403, "Sign in with the account phone number used for this order.")
    order = customer_order(order_id, body.phone)
    if order.get("email", "").lower() != session.get("email", "").lower(): raise HTTPException(404, "Order not found in your account.")
    if order["status"] != "Delivered":
        raise HTTPException(409, "A return can be requested after the order is delivered.")
    delivered_date = date.fromisoformat(order.get("delivered_at", order["delivery"])[:10])
    deadline = date.fromisoformat(order.get("return_until", (delivered_date+timedelta(days=14)).isoformat()))
    today = (datetime.now(timezone.utc)+timedelta(hours=5, minutes=30)).date()
    if today > deadline:
        raise HTTPException(409, f"The return window ended on {deadline.isoformat()}.")
    if not body.unused or not body.original_packaging:
        raise HTTPException(422, "Confirm that the item is unused and in its original packaging before requesting a return.")
    if any(r.get("status") not in ("Rejected", "Declined") for r in order.get("returns", [])) or any(r.get("order_id")==order["id"] and r.get("status") not in ("Declined", "Rejected") for r in store["refunds"]):
        raise HTTPException(409, "A return request is already active for this order.")
    value = int(order.get("refund_amount", order["amount"]))
    requires_admin = value >= 2000
    request_id = await create_neon_return(order, body.reason.strip(), "Customer confirmed unused and original packaging.", requires_admin)
    status = "Pending executive approval" if requires_admin else "Approved"
    order["status"] = "Return requested" if requires_admin else "Return approved"
    request = {"id":request_id or f"RT-{uuid.uuid4().hex[:8].upper()}","order_id":order["id"],"customer":order["customer"],"amount":value,"reason":body.reason.strip(),"requested":utcnow(),"status":status}
    store["refunds"].insert(0,request)
    if requires_admin:
        store["approvals"].insert(0,{**request,"status":"Pending review"})
    save_store(store)
    message = "Return request recorded and sent for admin approval." if requires_admin else "Return automatically approved under the low-value policy. Refund processing follows receipt and inspection."
    return {"return":request,"message":message}


@app.patch("/api/orders/{order_id}/status")
async def update_order_status(order_id: str, body: OrderStatusInput, request: Request) -> dict[str, Any]:
    require_admin(request)
    require_neon()
    allowed = {"Confirmed","Processing","Packed","Shipped","In transit","Out for delivery","Delivered","Cancellation requested","Cancelled","Return approved","Return rejected","Return picked up","Returned","Refund pending","Refund completed"}
    if body.status not in allowed:
        raise HTTPException(422, "Unsupported order status.")
    if body.estimated_delivery_from and body.estimated_delivery_to and body.estimated_delivery_to < body.estimated_delivery_from:
        raise HTTPException(422, "Estimated delivery end must not be before its start.")
    if DATABASE_URL and db_pool is None:
        raise HTTPException(503, "Neon database is configured but unavailable.")
    order = next((item for item in store["orders"] if item["id"].lower()==order_id.lower()), None)
    if db_pool:
        order = next((item for item in await load_neon_orders() if item["id"].lower()==order_id.lower()), order)
    if not order:
        raise HTTPException(404, "Order not found.")
    previous = order["status"]
    order["status"] = body.status
    extra = {}
    if body.estimated_delivery_from:
        extra["estimated_delivery_from"] = body.estimated_delivery_from.isoformat()
    if body.estimated_delivery_to:
        extra["estimated_delivery_to"] = body.estimated_delivery_to.isoformat()
    if body.status == "Delivered":
        order["delivered_at"] = utcnow()
        order["return_until"] = ((datetime.now(timezone.utc)+timedelta(hours=5, minutes=30)).date()+timedelta(days=14)).isoformat()
        extra.update({"delivered_at":order["delivered_at"],"return_until":order["return_until"]})
    order.update(extra)
    if db_pool:
        await set_neon_order_status(order, body.status, "status_updated", body.note or f"Status updated from {previous} to {body.status}.", extra)
        if body.estimated_delivery_from or body.estimated_delivery_to:
            async with db_pool.connection() as conn:
                await conn.execute("UPDATE orders SET estimated_delivery_from=COALESCE(%s,estimated_delivery_from),estimated_delivery_to=COALESCE(%s,estimated_delivery_to) WHERE order_number=%s", (body.estimated_delivery_from,body.estimated_delivery_to,order["id"]))
    save_store(store)
    return {"order_id":order["id"],"status":body.status,"estimated_delivery_from":order.get("estimated_delivery_from"),"estimated_delivery_to":order.get("estimated_delivery_to"),"return_until":order.get("return_until")}


@app.get("/api/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    require_admin(request)
    orders = await load_neon_orders()
    await hydrate_neon_store()
    return {"orders":orders,"approvals":store["approvals"],"refunds":store["refunds"],"conversations":store["conversations"][:50],"policy":store["policy"],"products":store["products"],"providers":provider_status(),"budget":budget_info()}


@app.post("/api/chat")
async def chat(body: ChatInput) -> dict[str, Any]:
    result = await respond(body.text.strip(),body.phone,body.channel,body.lang,body.history)
    if db_pool and result.get("refund_id"):
        order_id = result.get("trace",{}).get("retrieval",{}).get("order_id")
        order = next((item for item in store["orders"] if item["id"] == order_id), None)
        if order:
            request = next((item for item in store["refunds"] if item.get("id") == result["refund_id"]), {})
            try:
                requires_admin = request.get("amount", 0) >= 2000
                await create_neon_return(order, request.get("reason", "Customer requested a return/refund"), "Customer confirmed item condition in support chat.", requires_admin)
                order["status"] = "Return requested" if requires_admin else "Return approved"
                save_store(store)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
    if body.channel == "whatsapp" and body.phone:
        result["delivery"] = await whatsapp_send(body.phone,result["answer"])
    return result


@app.post("/api/checkout")
async def checkout(body: CheckoutInput, request: Request) -> dict[str, Any]:
    if not DATABASE_URL or db_pool is None:
        raise HTTPException(503, "Checkout is unavailable until Neon is configured and connected. Orders are stored in Neon only.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",body.email):
        raise HTTPException(422,"Enter a valid email address.")
    phone_digits=re.sub(r"\D","",body.phone)
    if len(phone_digits)==10: phone_digits="91"+phone_digits
    elif len(phone_digits)==11 and phone_digits.startswith("0"): phone_digits="91"+phone_digits[1:]
    if not 11<=len(phone_digits)<=15: raise HTTPException(422,"Enter a valid phone number with country code if needed.")
    account = read_session(request)
    if account and account.get("role") == "customer":
        async with db_pool.connection() as conn:
            profile = await (await conn.execute("SELECT full_name,email,phone_e164 FROM user_accounts WHERE id=%s AND role='customer'", (int(account["id"]),))).fetchone()
        if not profile:
            raise HTTPException(401, "Your customer account could not be verified. Sign in again and retry checkout.")
        body.name, body.email, body.phone = profile[0], profile[1], profile[2]
        phone_digits = normalize_order_phone(profile[2])
    existing = await find_neon_order_by_idempotency(body.idempotency_key)
    if existing:
        return {"order":{"id":existing["id"],"amount":existing["amount"],"payment":existing["payment"],"status":existing["status"],"delivery":existing["delivery"],"requested_delivery_date":existing.get("requested_delivery_date"),"requested_delivery_slot":existing.get("requested_delivery_slot"),"line_items":existing["line_items"]},"notifications":existing.get("notifications",{}),"duplicate":True}
    by_id={p["id"]:p for p in store["products"]}
    line_items=[]
    for item in body.items:
        product=by_id.get(item.product_id)
        if not product: raise HTTPException(422,f"Unknown product: {item.product_id}")
        size=next((s for s in product.get("sizes",[]) if s.lower()==item.size.lower()),None)
        if not size: raise HTTPException(422,f"{item.size} is not an available size for {product['name']}.")
        line_items.append({"product_id":product["id"],"name":product["name"],"size":size,"color":product.get("color",""),"quantity":item.quantity,"unit_price":product["price"],"line_total":product["price"]*item.quantity,"image":product.get("image")})
    subtotal=sum(item["line_total"] for item in line_items)
    shipping=0 if subtotal>=2500 else 99
    total=subtotal+shipping
    india_today=(datetime.now(timezone.utc)+timedelta(hours=5,minutes=30)).date()
    estimate_from=india_today+timedelta(days=4)
    estimate_to=india_today+timedelta(days=7)
    if not estimate_from <= body.requested_delivery_date <= estimate_to:
        raise HTTPException(422, f"Choose a requested delivery date between {estimate_from.isoformat()} and {estimate_to.isoformat()}.")
    delivery=body.requested_delivery_date
    order_id=f"FF-{india_today.strftime('%y%m')}-{uuid.uuid4().hex[:6].upper()}"
    address=" ".join(part.strip() for part in (body.address,body.city,body.state,body.pincode))
    order={"id":order_id,"customer":body.name.strip(),"email":body.email.strip().lower(),"phone":phone_digits,"address":address,"city":body.city.strip(),"state":body.state.strip(),"pincode":body.pincode,"line_items":line_items,"items":"; ".join(f"{i['name']} · {i['color']} / {i['size']} × {i['quantity']}" for i in line_items),"subtotal":subtotal,"refund_amount":subtotal,"shipping":shipping,"amount":total,"payment":"Cash on delivery","status":"Order placed","placed":india_today.isoformat(),"delivery":delivery.isoformat(),"requested_delivery_date":body.requested_delivery_date.isoformat(),"requested_delivery_slot":body.requested_delivery_slot,"estimated_delivery_from":estimate_from.isoformat(),"estimated_delivery_to":estimate_to.isoformat(),"return_until":(delivery+timedelta(days=14)).isoformat(),"channel":"Website","notifications":{}}
    async with checkout_lock:
        # Recheck idempotency inside the lock to avoid duplicate orders on simultaneous submits.
        existing = await find_neon_order_by_idempotency(body.idempotency_key)
        if existing:
            return {"order":{"id":existing["id"],"amount":existing["amount"],"payment":existing["payment"],"status":existing["status"],"delivery":existing["delivery"],"requested_delivery_date":existing.get("requested_delivery_date"),"requested_delivery_slot":existing.get("requested_delivery_slot"),"line_items":existing["line_items"]},"notifications":existing.get("notifications",{}),"duplicate":True}
        await persist_order_to_neon(order, body.idempotency_key)
        store["orders"].insert(0,order)
    item_summary="\n".join(f"• {i['name']} ({i['size']}) × {i['quantity']} — ₹{i['line_total']:,}" for i in line_items)
    whatsapp_text=f"Hi {order['customer']}, your Form & Field order {order_id} is confirmed.\n{item_summary}\nTotal: ₹{total:,} · Cash on delivery\nRequested delivery: {body.requested_delivery_date.isoformat()} · {body.requested_delivery_slot}\nThank you for shopping with us!"
    email_result,whatsapp_result=await asyncio.gather(send_order_email(order),whatsapp_send(phone_digits,whatsapp_text),return_exceptions=True)
    if isinstance(email_result,Exception): email_result={"sent":False,"reason":"Email delivery failed."}
    if isinstance(whatsapp_result,Exception): whatsapp_result={"sent":False,"reason":"WhatsApp delivery failed."}
    notifications={"email":email_result,"whatsapp":whatsapp_result}
    order["notifications"]=notifications
    await update_neon_notifications(order_id, notifications)
    return {"order":{"id":order_id,"amount":total,"payment":order["payment"],"status":order["status"],"delivery":order["delivery"],"requested_delivery_date":order["requested_delivery_date"],"requested_delivery_slot":order["requested_delivery_slot"],"line_items":line_items},"notifications":notifications,"duplicate":False}


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)) -> dict[str, str]:
    audio = await file.read(20_000_001)
    if len(audio) > 20_000_000: raise HTTPException(413,"Audio file is too large (20 MB limit).")
    if not audio: raise HTTPException(400,"The audio file is empty.")
    return {"text":await transcribe_audio(audio,file.content_type or "audio/webm")}


@app.post("/api/approvals/{approval_id}")
async def review_approval(approval_id: str, body: ApprovalInput, request: Request) -> dict[str, Any]:
    require_admin(request)
    if body.decision not in ("approve","decline"): raise HTTPException(400,"Decision must be approve or decline.")
    approval = next((a for a in store["approvals"] if a["id"] == approval_id),None)
    if not approval: raise HTTPException(404,"Approval not found.")
    if approval["status"] != "Pending review": raise HTTPException(409,"This approval has already been reviewed.")
    approval.update({"status":"Approved" if body.decision == "approve" else "Declined","reviewed_at":utcnow(),"reviewed_by":body.reviewer})
    refund=next((r for r in store["refunds"] if r["order_id"]==approval["order_id"]),None)
    if refund: refund["status"] = "Approved · queued for payment processing" if body.decision=="approve" else "Declined"
    if body.decision=="approve":
        order=next((o for o in store["orders"] if o["id"]==approval["order_id"]),None)
        if order:
            order["status"]="Return approved"
            if db_pool:
                try: await set_neon_order_status(order,"Return approved","return_approved",f"Approved by {body.reviewer}.")
                except HTTPException as exc:
                    if exc.status_code != 404: raise
    else:
        order=next((o for o in store["orders"] if o["id"]==approval["order_id"]),None)
        if order and db_pool:
            order["status"]="Return rejected"
            try: await set_neon_order_status(order,"Return rejected","return_rejected",f"Declined by {body.reviewer}.")
            except HTTPException as exc:
                if exc.status_code != 404: raise
    if db_pool:
        async with db_pool.connection() as conn:
            await conn.execute("UPDATE return_requests SET status=%s,approved_at=CASE WHEN %s='Approved' THEN now() ELSE approved_at END WHERE order_id=(SELECT id FROM orders WHERE order_number=%s) AND status='Pending executive approval'", ("Approved" if body.decision=="approve" else "Rejected","Approved" if body.decision=="approve" else "Rejected",approval["order_id"]))
    save_store(store)
    return approval


@app.post("/api/policy")
async def update_policy(body: PolicyInput, request: Request) -> dict[str,bool]:
    require_admin(request)
    store["policy"] = body.policy.strip()
    save_store(store)
    return {"ok":True}


@app.post("/api/policy/upload")
async def upload_policy(request: Request, file: UploadFile = File(...)) -> dict[str,Any]:
    require_admin(request)
    data=await file.read(12_000_001)
    if len(data)>12_000_000: raise HTTPException(413,"File too large; maximum size is 12 MB.")
    if file.filename and file.filename.lower().endswith(".pdf"):
        try: text="\n".join(page.get_text() for page in pymupdf.open(stream=data,filetype="pdf"))
        except Exception as exc: raise HTTPException(400,"Could not read PDF. Please upload a text-based PDF.") from exc
    elif file.filename and file.filename.lower().endswith((".txt",".md")):
        text=data.decode("utf-8",errors="replace")
    else: raise HTTPException(415,"Upload a PDF, TXT, or Markdown policy file.")
    text=text.strip()
    if len(text)<20: raise HTTPException(400,"No usable policy text found in this file.")
    store["policy"] = text[:50000]
    save_store(store)
    return {"ok":True,"filename":file.filename,"characters":len(text)}


@app.post("/api/whatsapp/send")
async def send_message(body: SendInput, request: Request) -> dict[str,Any]:
    require_admin(request)
    return await whatsapp_send(body.phone,body.text)


@app.post("/webhooks/openwa")
async def openwa_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
    raw = await request.body()
    secret = os.getenv("OPENWA_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "Configure OPENWA_WEBHOOK_SECRET and the matching OpenWA webhook secret.")
    signature = request.headers.get("x-openwa-signature", "")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid OpenWA webhook signature.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid OpenWA webhook JSON.") from exc
    if payload.get("event") != "message.received":
        return {"received": True}
    idempotency_key = request.headers.get("x-openwa-idempotency-key") or payload.get("idempotencyKey")
    if not idempotency_key:
        raise HTTPException(400, "OpenWA idempotency key is required.")
    if idempotency_key in store.get("openwa_processed", []) or idempotency_key in openwa_inflight:
        return {"received": True}
    openwa_inflight.add(idempotency_key)
    background_tasks.add_task(process_openwa_event, payload, idempotency_key)
    return {"received": True}


@app.get("/webhooks/whatsapp")
async def verify_whatsapp(request: Request) -> PlainTextResponse:
    q=request.query_params
    if q.get("hub.mode")=="subscribe" and hmac.compare_digest(q.get("hub.verify_token",""),os.getenv("WHATSAPP_VERIFY_TOKEN","")):
        return PlainTextResponse(q.get("hub.challenge",""))
    raise HTTPException(403,"WhatsApp webhook verification failed.")


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str,bool]:
    raw=await request.body()
    secret=os.getenv("WHATSAPP_APP_SECRET","")
    signature=request.headers.get("x-hub-signature-256","")
    if secret:
        expected="sha256="+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected): raise HTTPException(401,"Invalid webhook signature.")
    elif os.getenv("APP_ENV","development")=="production":
        raise HTTPException(503,"Configure WHATSAPP_APP_SECRET before production use.")
    payload=await request.json()
    background_tasks.add_task(process_whatsapp,payload)
    return {"received":True}


@app.get("/{asset_path:path}")
async def frontend(asset_path: str):
    public=ROOT/"frontend"/"dist"
    if not public.exists():
        raise HTTPException(503,"Frontend build not found. Run npm install and npm run build inside frontend/.")
    base=public.resolve()
    candidate=(base/asset_path).resolve()
    if not candidate.is_relative_to(base): raise HTTPException(403,"Forbidden")
    if candidate.is_file(): return FileResponse(candidate)
    return FileResponse(public/"index.html")
