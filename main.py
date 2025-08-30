import os, re, json, datetime
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import psycopg2, psycopg2.extras
from zoneinfo import ZoneInfo
import jdatetime
from fastapi.responses import HTMLResponse, PlainTextResponse

load_dotenv()

# ===================== ENV =====================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
ADMIN_IDS = [i.strip() for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))
PAYMENT_ADDRESS = os.getenv("PAYMENT_ADDRESS", "YOUR_USDT_ADDRESS")
PAYMENT_NETWORK = os.getenv("PAYMENT_NETWORK", "TRC20")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PANEL_TOKEN = os.getenv("ADMIN_PANEL_TOKEN", "")

if not (BOT_TOKEN and WEBHOOK_SECRET and TELEGRAM_WEBHOOK_SECRET and DATABASE_URL):
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET, TELEGRAM_WEBHOOK_SECRET, DATABASE_URL")

app = FastAPI(title="SourceTrader MVP (FA + Jalali)")

# ===================== TIME HELPERS =====================
TEHRAN = ZoneInfo("Asia/Tehran")

def now_dt() -> datetime.datetime:
    # tz-aware (UTC)
    return datetime.datetime.now(datetime.timezone.utc)

def to_tehran(dt: datetime.datetime) -> datetime.datetime:
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(TEHRAN)

def jalali_str(dt: datetime.datetime, with_time: bool = True) -> str:
    dt_th = to_tehran(dt)
    j = jdatetime.datetime.fromgregorian(datetime=dt_th)
    return f"{j.strftime('%Y/%m/%d')} - {dt_th.strftime('%H:%M')}" if with_time else j.strftime('%Y/%m/%d')

# ===================== DB (Postgres) =====================
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

def init_db():
    db_exec("""
    CREATE TABLE IF NOT EXISTS users(
        telegram_id BIGINT PRIMARY KEY,
        username TEXT, first_name TEXT, last_name TEXT,
        joined_at TIMESTAMPTZ,
        trial_started_at TIMESTAMPTZ,
        subscription_expires_at TIMESTAMPTZ,
        referred_by TEXT,
        is_admin BOOLEAN DEFAULT FALSE,
        awaiting_tx BOOLEAN DEFAULT FALSE
    );
    """)
    db_exec("ALTER TABLE users ADD COLUMN IF NOT EXISTS awaiting_tx BOOLEAN DEFAULT FALSE;")
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
    # seed admins
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
    if not rows: return False
    exp = rows[0]["subscription_expires_at"]
    if not exp: return False
    if getattr(exp, "tzinfo", None) is None:
        exp = exp.replace(tzinfo=datetime.timezone.utc)
    return now_dt() <= exp

def ensure_trial(telegram_id: int):
    rows = db_exec("SELECT trial_started_at FROM users WHERE telegram_id=%s", (telegram_id,))
    if not rows: return
    if rows[0]["trial_started_at"]: return
    start = now_dt()
    exp = start + datetime.timedelta(days=TRIAL_DAYS)
    db_exec("""
        UPDATE users SET trial_started_at=%s, subscription_expires_at=%s
        WHERE telegram_id=%s
    """, (start, exp, telegram_id))

def extend_subscription(telegram_id: int, days: int):
    rows = db_exec("SELECT subscription_expires_at FROM users WHERE telegram_id=%s", (telegram_id,))
    base = now_dt()
    if rows and rows[0]["subscription_expires_at"]:
        cur_exp = rows[0]["subscription_expires_at"]
        if cur_exp and cur_exp > base:
            base = cur_exp
    new_exp = base + datetime.timedelta(days=days)
    db_exec("UPDATE users SET subscription_expires_at=%s WHERE telegram_id=%s", (new_exp, telegram_id))
    return new_exp

def list_active_users() -> List[int]:
    rows = db_exec("SELECT telegram_id, subscription_expires_at FROM users")
    res = []
    for r in rows or []:
        exp = r["subscription_expires_at"]
        if exp:
            if getattr(exp, "tzinfo", None) is None:
                exp = exp.replace(tzinfo=datetime.timezone.utc)
            if now_dt() <= exp:
                res.append(int(r["telegram_id"]))
    return res

def save_signal(symbol: str, side: str, price, t: str) -> int:
    rows = db_exec("""
        INSERT INTO signals(symbol, side, price, time, created_at)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (symbol, side, price, t or "", now_dt()))
    return rows[0]["id"] if rows else 0

def find_ref_open_id(symbol: str, close_side: str) -> Optional[int]:
    # برای CLOSE_LONG آخرین LONG، برای CLOSE_SHORT آخرین SHORT را برمی‌گرداند
    s = (close_side or "").upper()
    open_side = "LONG" if s == "CLOSE_LONG" else "SHORT" if s == "CLOSE_SHORT" else None
    if not open_side:
        return None
    rows = db_exec("""
        SELECT id FROM signals
        WHERE symbol=%s AND side=%s
        ORDER BY id DESC LIMIT 1
    """, (symbol, open_side))
    return rows[0]["id"] if rows else None

# ===================== TELEGRAM HELPERS =====================
async def tg_send(chat_id: int, text: str, parse_mode: Optional[str] = "HTML", reply_markup: Optional[dict] = None):
    async with httpx.AsyncClient(timeout=20) as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode: data["parse_mode"] = parse_mode
        if reply_markup: data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        await client.post(url, data=data)

async def tg_send_to_admins(text: str):
    for aid in ADMIN_IDS:
        try:
            await tg_send(int(aid), text)
        except Exception:
            pass

def fa_side(side: str) -> str:
    m = {
        "LONG": "خرید",
        "SHORT": "فروش",
        "CLOSE_LONG": "بستن خرید",
        "CLOSE_SHORT": "بستن فروش",
    }
    return m.get((side or "").upper(), side)

def format_signal(title, symbol, side, price, t, sig_id=None, sl=None, tp=None):
    price_str = str(int(round(price))) if isinstance(price, (int, float)) else "N/A"
    lines = []
    lines.append(f"📡 <b>{title}</b>" + (f"  #{sig_id}" if sig_id else ""))
    lines.append(f"📌 نماد: <b>{symbol}</b>")
    lines.append(f"🧭 جهت: <b>{fa_side(side)}</b>")
    lines.append(f"💲 قیمت: <b>{price_str}</b>")
    lines.append(f"🕒 زمان ارسال: <code>{jalali_str(now_dt(), True)}</code>")
    if sl is not None:
        lines.append(f"⛔ حد ضرر: <b>{int(round(sl))}</b>")
    if tp is not None:
        lines.append(f"🎯 تارگت: <b>{int(round(tp))}</b>")
    return "\n".join(lines)

def extract_txid(text: str) -> Optional[str]:
    if not text: return None
    text = text.strip()
    m = re.search(r'(0x)?[A-Fa-f0-9]{32,}', text)  # هگز طولانی
    return m.group(0) if m else None

# ===================== MODELS =====================
class TVPayload(BaseModel):
    strategy: Optional[str] = None
    symbol:   Optional[str] = None
    side:     Optional[str] = None   # LONG/SHORT/CLOSE_LONG/CLOSE_SHORT
    price:    Optional[float] = None
    time:     Optional[str] = None
    secret:   Optional[str] = None
    sl:       Optional[float] = None    # اختیاری
    tp:       Optional[float] = None    # اختیاری
    tf:       Optional[str] = None      # اختیاری (مثلا "15")

# ===================== STARTUP =====================
init_db()

# ===================== ROUTES =====================
# Health: GET + HEAD (برای UptimeRobot رایگان)
@app.get("/health")
def health_get():
    return {"status": "ok"}

@app.head("/health")
def health_head():
    return PlainTextResponse("", status_code=200)

# TradingView (وبهوک سیگنال‌ها)
@app.post("/tv")
async def tv_hook(payload: TVPayload):
    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    title  = payload.strategy or "Signal"
    symbol = payload.symbol or "UNKNOWN"
    side   = (payload.side or "N/A").upper()

    # ذخیره سیگنال و دریافت کد
    sid = save_signal(symbol, side, payload.price, payload.time)

    # اگر Close بود، ارجاع به آخرین Open همان سمت بساز
    if side in ("CLOSE_LONG", "CLOSE_SHORT"):
        ref = find_ref_open_id(symbol, side)
        if ref:
            title = f"{title} (بستن #{ref})"

    text = format_signal(
        title=title,
        symbol=symbol,
        side=side,
        price=payload.price,
        t=payload.time,
        sig_id=None if side.startswith("CLOSE") else sid,   # کد را فقط روی Open نشان بده
        sl=payload.sl,
        tp=payload.tp,
    )

    users = list_active_users()
    async with httpx.AsyncClient(timeout=20) as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        for uid in users:
            try:
                await client.post(url, data={"chat_id": uid, "text": text, "parse_mode":"HTML","disable_web_page_preview":True})
            except Exception:
                pass
    return {"ok": True, "id": sid, "delivered_to": len(users)}

# Telegram webhook
@app.post("/tg/webhook")
async def tg_webhook(req: Request):
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

    # register/update user
    db_exec("""
    INSERT INTO users(telegram_id, username, first_name, last_name, joined_at)
    VALUES (%s,%s,%s,%s,%s)
    ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name
    """, (uid, username, first, last, now_dt()))

    # commands
    if text.startswith("/start"):
        ensure_trial(uid)
        row = db_exec("SELECT subscription_expires_at FROM users WHERE telegram_id=%s", (uid,))
        exp = row[0]["subscription_expires_at"] if row else None
        exp_txt = jalali_str(exp, with_time=True) if exp else "N/A"
        msg = (
            "👋 به ربات سیگنال خوش آمدی!\n\n"
            f"✅ {TRIAL_DAYS} روز اشتراک رایگان برای تست فعال شد.\n"
            f"⏰ انقضا: <b>{exp_txt}</b>\n\n"
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
        exp_txt = jalali_str(exp, with_time=True) if exp else "N/A"
        await tg_send(chat_id, f"وضعیت اشتراک: {active}\nانقضا: <b>{exp_txt}</b>"); return {"ok": True}

    if text.startswith("/last"):
        rows = db_exec("SELECT symbol, side, price, time, created_at FROM signals ORDER BY id DESC LIMIT 5")
        if not rows:
            await tg_send(chat_id, "هنوز سیگنالی ثبت نشده."); return {"ok": True}
        lines = []
        for r in rows:
            price = r["price"] if r["price"] is not None else "N/A"
            created = r["created_at"]
            created_text = jalali_str(created, with_time=True) if created else "-"
            # قیمت بدون اعشار
            price_txt = str(int(round(price))) if isinstance(price, (int,float)) else price
            lines.append(f"• {r['symbol']} | {fa_side(r['side'])} | {price_txt} | {created_text}")
        await tg_send(chat_id, "📈 آخرین سیگنال‌ها:\n" + "\n".join(lines)); return {"ok": True}

    if text.startswith("/subscribe"):
        db_exec("UPDATE users SET awaiting_tx=TRUE WHERE telegram_id=%s", (uid,))
        msg = (
            "💳 تمدید اشتراک (خیلی ساده):\n"
            f"1) مبلغ را به آدرس زیر بفرست:\n"
            f"   • آدرس: <code>{PAYMENT_ADDRESS}</code>\n"
            f"   • شبکه: <b>{PAYMENT_NETWORK}</b>\n"
            "2) بعد از پرداخت، همین‌جا «هش تراکنش یا لینک اکسپلورر» را بفرست.\n"
            "   (نیازی به فرمت خاص نیست؛ فقط بفرست.)\n"
            "3) ما بررسی و تایید می‌کنیم. ✅"
        )
        buttons = {
            "inline_keyboard": [
                [{"text": "📋 کپی آدرس", "switch_inline_query_current_chat": PAYMENT_ADDRESS}],
                [{"text": "🧭 Tronscan", "url": "https://tronscan.org/"}]
            ]
        }
        await tg_send(chat_id, msg, reply_markup=buttons); return {"ok": True}

    if text.startswith("/tx"):
        parts = text.split()
        if len(parts) < 2:
            await tg_send(chat_id, "TXID نامعتبر. مثال:\n/tx f1a2b3c4..."); return {"ok": True}
        txid = parts[1].strip()
        db_exec("INSERT INTO payments(telegram_id, txid, status, created_at) VALUES (%s,%s,'pending',%s)", (uid, txid, now_dt()))
        db_exec("UPDATE users SET awaiting_tx=FALSE WHERE telegram_id=%s", (uid,))
        await tg_send(chat_id, "✅ درخواست تمدید ثبت شد. پس از تأیید، اشتراک تمدید می‌شود.")
        await tg_send_to_admins(f"🧾 پرداخت جدید:\nUser: {uid}\nTXID: {txid}\nتایید: /confirm {uid} 30"); return {"ok": True}

    # Admin: debug
    if text.startswith("/debug"):
        if str(uid) not in ADMIN_IDS:
            await tg_send(chat_id, "⛔️ فقط ادمین."); return {"ok": True}
        row = db_exec("SELECT trial_started_at, subscription_expires_at, awaiting_tx FROM users WHERE telegram_id=%s", (uid,))
        ts, exp, aw = (row[0]["trial_started_at"], row[0]["subscription_expires_at"], row[0]["awaiting_tx"]) if row else (None,None,None)
        def dt_line(name, dtv):
            if not dtv: return f"{name}: N/A"
            if getattr(dtv, 'tzinfo', None) is None: dtv = dtv.replace(tzinfo=datetime.timezone.utc)
            return f"{name}: UTC={dtv.isoformat()} | Tehran/Jalali={jalali_str(dtv, True)}"
        msg = (
            "🛠 DEBUG\n"
            f"TRIAL_DAYS={TRIAL_DAYS}\n"
            f"{dt_line('now', now_dt())}\n"
            f"{dt_line('trial_started_at', ts)}\n"
            f"{dt_line('subscription_expires_at', exp)}\n"
            f"awaiting_tx={aw}"
        )
        await tg_send(chat_id, f"<code>{msg}</code>"); return {"ok": True}

    # Admin: تایید پرداخت و تمدید
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
            await tg_send(chat_id, f"✅ تمدید شد تا: <b>{jalali_str(new_exp, with_time=True)}</b>")
            await tg_send(target, f"🎉 اشتراک شما تمدید شد تا: <b>{jalali_str(new_exp, with_time=True)}</b>")
        except Exception as e:
            await tg_send(chat_id, f"خطا در تایید: {e}")
        return {"ok": True}

    if text.startswith("/broadcast"):
        if str(uid) not in ADMIN_IDS:
            await tg_send(chat_id, "⛔️ فقط ادمین."); return {"ok": True}
        msg = text.replace("/broadcast", "", 1).strip()
        if not msg:
            await tg_send(chat_id, "متن خالی است."); return {"ok": True}
        rows = db_exec("SELECT telegram_id FROM users", ())
        for r in rows or []:
            try: await tg_send(int(r["telegram_id"]), f"📢 {msg}")
            except: pass
        await tg_send(chat_id, "✅ ارسال شد."); return {"ok": True}

    # متن آزاد: اگر کاربر در حالت انتظار TX باشد، همین را به‌عنوان پرداخت ثبت کن
    row = db_exec("SELECT awaiting_tx FROM users WHERE telegram_id=%s", (uid,))
    if row and row[0]["awaiting_tx"]:
        tx = extract_txid(text) or text
        db_exec("INSERT INTO payments(telegram_id, txid, status, created_at) VALUES (%s,%s,'pending',%s)", (uid, tx, now_dt()))
        db_exec("UPDATE users SET awaiting_tx=FALSE WHERE telegram_id=%s", (uid,))
        await tg_send(chat_id, "✅ درخواست تمدید ثبت شد. پس از تأیید، اشتراک شما تمدید می‌شود.")
        await tg_send_to_admins(f"🧾 پرداخت جدید:\nUser: {uid}\nTXID: {tx}\nتایید: /confirm {uid} 30")
        return {"ok": True}

    # اگر شبیه TXID بود، به‌عنوان پرداخت ثبت کن (QoL)
    tx_guess = extract_txid(text)
    if tx_guess:
        db_exec("INSERT INTO payments(telegram_id, txid, status, created_at) VALUES (%s,%s,'pending',%s)", (uid, tx_guess, now_dt()))
        await tg_send(chat_id, "✅ درخواست تمدید ثبت شد. پس از تأیید، اشتراک شما تمدید می‌شود.")
        await tg_send_to_admins(f"🧾 پرداخت جدید:\nUser: {uid}\nTXID: {tx_guess}\nتایید: /confirm {uid} 30")
        return {"ok": True}

    # پیش‌فرض
    await tg_send(chat_id, "دستور نامعتبر. /help را ببین."); return {"ok": True}

# ===================== SIMPLE ADMIN PANEL =====================
@app.get("/admin", response_class=HTMLResponse)
def admin_home(token: str):
    if not ADMIN_PANEL_TOKEN or token != ADMIN_PANEL_TOKEN:
        return HTMLResponse("<h3>Unauthorized</h3>", status_code=401)
    users = db_exec("SELECT telegram_id, username, subscription_expires_at, is_admin, joined_at, awaiting_tx FROM users ORDER BY subscription_expires_at DESC NULLS LAST")
    pays  = db_exec("SELECT id, telegram_id, txid, status, created_at FROM payments ORDER BY id DESC LIMIT 50")
    sigs  = db_exec("SELECT id, symbol, side, price, time, created_at FROM signals ORDER BY id DESC LIMIT 50")

    def row(tds): return "<tr>" + "".join([f"<td>{td}</td>" for td in tds]) + "</tr>"

    html = ["<html><head><meta charset='utf-8'><title>Admin</title>"
            "<style>table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px;font-family:Arial;font-size:13px}</style>"
            "</head><body>"]
    html.append("<h2>Admin Panel</h2>")

    html.append("<h3>Users</h3><table><tr><th>ID</th><th>Username</th><th>Joined</th><th>Expires</th><th>Admin</th><th>Awaiting TX</th></tr>")
    for u in users or []:
        joined = u.get('joined_at'); exp = u.get('subscription_expires_at')
        joined_txt = jalali_str(joined, True) if joined else "-"
        exp_txt = jalali_str(exp, True) if exp else "-"
        html.append(row([u['telegram_id'], u.get('username',''), joined_txt, exp_txt, "✅" if u.get('is_admin') else "—", "⏳" if u.get('awaiting_tx') else "—"]))
    html.append("</table>")

    html.append("<h3>Payments (last 50)</h3><table><tr><th>ID</th><th>User</th><th>TXID</th><th>Status</th><th>Created</th></tr>")
    for p in pays or []:
        created_txt = jalali_str(p['created_at'], True) if p.get('created_at') else "-"
        html.append(row([p['id'], p['telegram_id'], p['txid'], p['status'], created_txt]))
    html.append("</table>")

    html.append("<h3>Signals (last 50)</h3><table><tr><th>ID</th><th>Symbol</th><th>Side</th><th>Price</th><th>Created</th></tr>")
    for s in sigs or []:
        created_txt = jalali_str(s['created_at'], True) if s.get('created_at') else "-"
        price_txt = str(int(round(s['price']))) if isinstance(s.get('price'), (int,float)) else s.get('price','')
        html.append(row([s['id'], s['symbol'], fa_side(s['side']), price_txt, created_txt]))
    html.append("</table>")

    html.append("</body></html>")
    return "".join(html)
