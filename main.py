import os, datetime
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras

load_dotenv()

# ===== Env =====
from fastapi.responses import HTMLResponse
ADMIN_PANEL_TOKEN = os.getenv("ADMIN_PANEL_TOKEN", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
ADMIN_IDS = [i.strip() for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))
PAYMENT_ADDRESS = os.getenv("PAYMENT_ADDRESS", "YOUR_USDT_ADDRESS")
PAYMENT_NETWORK = os.getenv("PAYMENT_NETWORK", "TRC20")
DATABASE_URL = os.getenv("DATABASE_URL")

if not (BOT_TOKEN and WEBHOOK_SECRET and TELEGRAM_WEBHOOK_SECRET and DATABASE_URL):
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET, TELEGRAM_WEBHOOK_SECRET, DATABASE_URL")

app = FastAPI(title="SourceTrader MVP")

# ===== DB helpers (Postgres) =====
def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def db_exec(q, args=()):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, args)
            try:
                return cur.fetchall()
            except psycopg2.ProgrammingError:
                return None

def utcnow() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def now_dt() -> datetime.datetime:
    # همیشه datetime با timezone (UTC) برگردون
    return datetime.datetime.now(datetime.timezone.utc)


def add_days(dt: datetime.datetime, days: int) -> datetime.datetime:
    return dt + datetime.timedelta(days=days)

def init_db():
    db_exec("""
    CREATE TABLE IF NOT EXISTS users(
        telegram_id BIGINT PRIMARY KEY,
        username TEXT, first_name TEXT, last_name TEXT,
        joined_at TIMESTAMPTZ,
        trial_started_at TIMESTAMPTZ,
        subscription_expires_at TIMESTAMPTZ,
        referred_by TEXT,
        is_admin BOOLEAN DEFAULT FALSE
    );
    """)
    db_exec("""
    CREATE TABLE IF NOT EXISTS signals(
        id BIGSERIAL PRIMARY KEY,
        symbol TEXT, side TEXT, price DOUBLE PRECISION,
        time TEXT, created_at TIMESTAMPTZ
    );
    """)
    db_exec("""
    CREATE TABLE IF NOT EXISTS payments(
        id BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT, txid TEXT,
        status TEXT, created_at TIMESTAMPTZ
    );
    """)
    # ثبت ادمین‌ها
    now = now_dt()
    for aid in ADMIN_IDS:
        try:
            db_exec("""
            INSERT INTO users(telegram_id, is_admin, joined_at)
            VALUES (%s, TRUE, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET is_admin=EXCLUDED.is_admin;
            """, (int(aid), now))
        except Exception:
            pass

def is_active_user(telegram_id: int) -> bool:
    rows = db_exec("SELECT subscription_expires_at FROM users WHERE telegram_id=%s", (telegram_id,))
    if not rows:
        return False
    exp = rows[0]["subscription_expires_at"]
    if not exp:
        return False
    # exp باید timezone-aware باشد؛ اگر نبود، UTC بده
    if getattr(exp, "tzinfo", None) is None:
        exp = exp.replace(tzinfo=datetime.timezone.utc)
    # now هم timezone-aware است (تابع بالا)
    return now_dt() <= exp


def ensure_trial(telegram_id: int):
    rows = db_exec("SELECT trial_started_at, subscription_expires_at FROM users WHERE telegram_id=%s", (telegram_id,))
    if not rows: return
    if rows[0]["trial_started_at"]: return
    start = now_dt()
    exp = add_days(start, TRIAL_DAYS)
    db_exec("UPDATE users SET trial_started_at=%s, subscription_expires_at=%s WHERE telegram_id=%s",
            (start, exp, telegram_id))

def extend_subscription(telegram_id: int, days: int):
    rows = db_exec("SELECT subscription_expires_at FROM users WHERE telegram_id=%s", (telegram_id,))
    base = now_dt()
    if rows and rows[0]["subscription_expires_at"]:
        cur_exp = rows[0]["subscription_expires_at"]
        if cur_exp and cur_exp > base:
            base = cur_exp
    new_exp = add_days(base, days)
    db_exec("UPDATE users SET subscription_expires_at=%s WHERE telegram_id=%s", (new_exp, telegram_id))
    return new_exp

def list_active_users() -> List[int]:
    rows = db_exec("SELECT telegram_id, subscription_expires_at FROM users", ())
    res = []
    for r in rows or []:
        exp = r["subscription_expires_at"]
        if exp and now_dt() <= exp:
            res.append(int(r["telegram_id"]))
    return res

def save_signal(symbol: str, side: str, price, t: str) -> int:
    rows = db_exec("""
        INSERT INTO signals(symbol, side, price, time, created_at)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (symbol, side, price, t or "", now_dt()))
    return rows[0]["id"] if rows else 0

# ===== Telegram helpers =====
async def tg_send(chat_id: int, text: str, parse_mode: Optional[str] = "HTML"):
    async with httpx.AsyncClient(timeout=20) as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode: data["parse_mode"] = parse_mode
        await client.post(url, data=data)

async def tg_send_to_admins(text: str):
    for aid in ADMIN_IDS:
        try: await tg_send(int(aid), text)
        except: pass

def format_signal(title, symbol, side, price, t):
    price_str = f"{price:.8f}" if isinstance(price, (int,float)) else "N/A"
    return (
        f"📡 <b>{title}</b>\n"
        f"🔹 Symbol: <b>{symbol}</b>\n"
        f"🔸 Side: <b>{side.upper()}</b>\n"
        f"💲 Price: <b>{price_str}</b>\n"
        f"🕒 Time: <code>{t or ''}</code>"
    )

# ===== Models =====
class TVPayload(BaseModel):
    strategy: Optional[str] = None
    symbol:   Optional[str] = None
    side:     Optional[str] = None   # LONG/SHORT
    price:    Optional[float] = None
    time:     Optional[str] = None
    secret:   Optional[str] = None

# ===== Startup =====
init_db()

# ===== Health =====
@app.get("/health")
def health():
    return {"status": "ok"}

# ===== TradingView Webhook (or manual test) =====
@app.post("/tv")
async def tv_hook(payload: TVPayload):
    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")
    title  = payload.strategy or "Signal"
    symbol = payload.symbol or "UNKNOWN"
    side   = (payload.side or "N/A").upper()
    sid = save_signal(symbol, side, payload.price, payload.time)
    text = format_signal(title, symbol, side, payload.price, payload.time)

    # ارسال برای کاربران فعال
    users = list_active_users()
    async with httpx.AsyncClient(timeout=20) as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        for uid in users:
            try:
                await client.post(url, data={"chat_id": uid, "text": text, "parse_mode":"HTML","disable_web_page_preview":True})
            except: pass
    return {"ok": True, "id": sid, "delivered_to": len(users)}

# ===== Telegram Webhook (commands) =====
@app.post("/tg/webhook")
async def tg_webhook(req: Request):
    # امنیت ساده: Secret Token در هدر
    sec = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if sec != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid telegram secret")

    update = await req.json()
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    from_user = message.get("from") or {}
    chat_id = chat.get("id"); uid = from_user.get("id")
    username = from_user.get("username"); first = from_user.get("first_name"); last = from_user.get("last_name")
    if not chat_id or not uid: return {"ok": True}

    # ثبت/بروزرسانی کاربر
    now = now_dt()
    db_exec("""
    INSERT INTO users(telegram_id, username, first_name, last_name, joined_at)
    VALUES (%s,%s,%s,%s,%s)
    ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name
    """, (uid, username, first, last, now))

    # دستورات
    if text.startswith("/start"):
        ensure_trial(uid)
        row = db_exec("SELECT subscription_expires_at FROM users WHERE telegram_id=%s", (uid,))
        exp = row[0]["subscription_expires_at"] if row else None
        msg = (
            "👋 به ربات سیگنال خوش آمدی!\n\n"
            f"✅ {TRIAL_DAYS} روز اشتراک رایگان برای تست فعال شد.\n"
            f"⏰ انقضا: <b>{exp or 'N/A'}</b>\n\n"
            "دستورات:\n"
            "/last - آخرین سیگنال‌ها\n"
            "/status - وضعیت اشتراک\n"
            "/subscribe - راهنمای پرداخت و تمدید\n"
            "/help - راهنمای استفاده\n"
            "/edu - نکات آموزشی\n"
            "/whoami - نمایش شناسه شما\n"
        )
        await tg_send(chat_id, msg); return {"ok": True}

    if text.startswith("/whoami"):
        await tg_send(chat_id, f"🆔 Telegram ID: <code>{uid}</code>"); return {"ok": True}

    if text.startswith("/help"):
        await tg_send(chat_id, "ℹ️ راهنما:\nسیگنال‌ها فقط برای کاربرانِ دارای اشتراک فعال ارسال می‌شود. با /subscribe روش تمدید را ببین."); return {"ok": True}

    if text.startswith("/edu"):
        await tg_send(chat_id, "📚 نکات:\n1) مدیریت ریسک را رعایت کنید.\n2) با سرمایه قابل‌تحمل معامله کنید.\n3) حدضرر فراموش نشود."); return {"ok": True}

    if text.startswith("/status"):
        row = db_exec("SELECT subscription_expires_at FROM users WHERE telegram_id=%s", (uid,))
        exp = row[0]["subscription_expires_at"] if row else None
        active = "✅ فعال" if is_active_user(uid) else "⛔️ غیرفعال"
        await tg_send(chat_id, f"وضعیت اشتراک: {active}\nانقضا: <b>{exp or 'N/A'}</b>"); return {"ok": True}

    if text.startswith("/last"):
        rows = db_exec("SELECT symbol, side, price, time, created_at FROM signals ORDER BY id DESC LIMIT 5")
        if not rows:
            await tg_send(chat_id, "هنوز سیگنالی ثبت نشده."); return {"ok": True}
        lines = []
        for r in rows:
            price = r["price"] if r["price"] is not None else "N/A"
            lines.append(f"• {r['symbol']} | {r['side']} | {price} | {r['time'] or r['created_at']}")
        await tg_send(chat_id, "📈 آخرین سیگنال‌ها:\n" + "\n".join(lines)); return {"ok": True}

    if text.startswith("/subscribe"):
        msg = (
            "💳 تمدید اشتراک:\n"
            f"• آدرس پرداخت: <code>{PAYMENT_ADDRESS}</code>\n"
            f"• شبکه: <b>{PAYMENT_NETWORK}</b>\n"
            "• پس از پرداخت، TXID را با دستور /tx وارد کنید.\n"
            "مثال: /tx f1a2b3c4...\n"
        )
        await tg_send(chat_id, msg); return {"ok": True}

    if text.startswith("/tx"):
        parts = text.split()
        if len(parts) < 2:
            await tg_send(chat_id, "TXID نامعتبر. مثال:\n/tx f1a2b3c4..."); return {"ok": True}
        txid = parts[1].strip()
        db_exec("INSERT INTO payments(telegram_id, txid, status, created_at) VALUES (%s,%s,'pending',%s)", (uid, txid, now_dt()))
        await tg_send(chat_id, "✅ درخواست تمدید ثبت شد. پس از تأیید، اشتراک تمدید می‌شود.")
        await tg_send_to_admins(f"🧾 پرداخت جدید:\nUser: {uid}\nTXID: {txid}\nتایید: /confirm {uid} 30"); return {"ok": True}

    # دستورات ادمین
    if text.startswith("/confirm"):
        if str(uid) not in ADMIN_IDS:
            await tg_send(chat_id, "⛔️ فقط ادمین."); return {"ok": True}
        parts = text.split()
        if len(parts) < 3:
            await tg_send(chat_id, "استفاده:\n/confirm <user_id> <days>"); return {"ok": True}
        try:
            target, days = int(parts[1]), int(parts[2])
            new_exp = extend_subscription(target, days)
            db_exec("UPDATE payments SET status='approved' WHERE telegram_id=%s AND status='pending'", (target,))
            await tg_send(chat_id, f"✅ تمدید شد تا: <b>{new_exp.strftime('%Y-%m-%dT%H:%M:%SZ')}</b>")
            await tg_send(target, f"🎉 اشتراک شما تمدید شد تا: <b>{new_exp.strftime('%Y-%m-%dT%H:%M:%SZ')}</b>")
        except Exception as e:
            await tg_send(chat_id, f"خطا در تایید: {e}")
        return {"ok": True}

    if text.startswith("/broadcast"):
        if str(uid) not in ADMIN_IDS:
            await tg_send(chat_id, "⛔️ فقط ادمین."); return {"ok": True}
        msg = text.replace("/broadcast", "", 1).strip()
        if not msg: await tg_send(chat_id, "متن خالی است."); return {"ok": True}
        rows = db_exec("SELECT telegram_id FROM users", ())
        for r in rows or []:
            try: await tg_send(int(r["telegram_id"]), f"📢 {msg}")
            except: pass
        await tg_send(chat_id, "✅ ارسال شد."); return {"ok": True}

    await tg_send(chat_id, "دستور نامعتبر. /help را ببین."); return {"ok": True}

@app.get("/admin", response_class=HTMLResponse)
def admin_home(token: str):
    if not ADMIN_PANEL_TOKEN or token != ADMIN_PANEL_TOKEN:
        return HTMLResponse("<h3>Unauthorized</h3>", status_code=401)
    users = db_exec("SELECT telegram_id, username, subscription_expires_at, is_admin FROM users ORDER BY subscription_expires_at DESC NULLS LAST")
    pays  = db_exec("SELECT id, telegram_id, txid, status, created_at FROM payments ORDER BY id DESC LIMIT 50")
    sigs  = db_exec("SELECT id, symbol, side, price, time, created_at FROM signals ORDER BY id DESC LIMIT 50")
    def row(tds): return "<tr>" + "".join([f"<td>{td}</td>" for td in tds]) + "</tr>"
    html = ["<html><head><meta charset='utf-8'><style>table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px;font-family:Arial;font-size:13px}</style></head><body>"]
    html += ["<h2>Admin Panel</h2>",
             "<h3>Users</h3><table><tr><th>ID</th><th>Username</th><th>Expires</th><th>Admin</th></tr>"]
    for u in users or []:
        html.append(row([u['telegram_id'], u.get('username',''), u.get('subscription_expires_at',''), "✅" if u.get('is_admin') else "—"]))
    html += ["</table><h3>Payments (last 50)</h3><table><tr><th>ID</th><th>User</th><th>TXID</th><th>Status</th><th>Created</th></tr>"]
    for p in pays or []:
        html.append(row([p['id'], p['telegram_id'], p['txid'], p['status'], p['created_at']]))
    html += ["</table><h3>Signals (last 50)</h3><table><tr><th>ID</th><th>Symbol</th><th>Side</th><th>Price</th><th>Time</th><th>Created</th></tr>"]
    for s in sigs or []:
        html.append(row([s['id'], s['symbol'], s['side'], s.get('price',''), s.get('time',''), s['created_at']]))
    html.append("</table></body></html>")
    return "".join(html)
