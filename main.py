# main.py
# -*- coding: utf-8 -*-

import os
import json
import math
import traceback
from datetime import datetime, timedelta, timezone

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Request, Header, Query
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import pytz
import jdatetime

# ─────────────────────────────────────────────────────────────
# Load env & Settings
# ─────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_PANEL_TOKEN = os.getenv("ADMIN_PANEL_TOKEN", "")
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))
CRON_TOKEN = os.getenv("CRON_TOKEN", "Nw8CnNI4dfwWLwGJQuxBt4hI_XAM7W9ZHx1Yk")

SHOW_FIXED_SLTP = os.getenv("SHOW_FIXED_SLTP", "true").lower() == "true"
FIXED_SL_PCT = float(os.getenv("FIXED_SL_PCT", "0.02"))
FIXED_TP_PCT = float(os.getenv("FIXED_TP_PCT", "0.04"))

ALLOWED_SYMBOLS = os.getenv("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT,DOGEUSDT,SOLUSDT,BNBUSDT").split(",")

# ─────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="SourceTrader")

# ─────────────────────────────────────────────────────────────
# DB Helpers
# ─────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def db_exec(q, args=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, args or ())
            if cur.description:
                return cur.fetchall()
            return None

def migrate_db():
    # users
    db_exec(
        """CREATE TABLE IF NOT EXISTS users(
            id BIGINT PRIMARY KEY,
            expires_at TIMESTAMPTZ,
            awaiting_tx BOOLEAN DEFAULT FALSE,
            trial_started_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )"""
    )
    # signals
    db_exec(
        """CREATE TABLE IF NOT EXISTS signals(
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            time TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )"""
    )
    # migrations/additions
    db_exec("ALTER TABLE signals ADD COLUMN IF NOT EXISTS ref_open_id INTEGER")
    db_exec("ALTER TABLE signals ADD COLUMN IF NOT EXISTS pnl_pct DOUBLE PRECISION")
    db_exec("ALTER TABLE signals ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ")
    db_exec("CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(time)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_signals_ref ON signals(ref_open_id)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_signals_side ON signals(side)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_users_expires ON users(expires_at)")

# Run migration on startup
migrate_db()

# ─────────────────────────────────────────────────────────────
# Time & Jalali Helpers
# ─────────────────────────────────────────────────────────────
TZ_TEHRAN = pytz.timezone("Asia/Tehran")

def now_dt() -> datetime:
    return datetime.now(timezone.utc)

def to_tehran(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_TEHRAN)

def jalali_date_str(dt: datetime) -> str:
    t = to_tehran(dt)
    j = jdatetime.GregorianToJalali(t.year, t.month, t.day).getJalaliList()
    y, m, d = j
    hh = str(t.hour).zfill(2)
    mm = str(t.minute).zfill(2)
    return f"{y:04d}/{m:02d}/{d:02d} - {hh}:{mm}"

def jalali_short(dt: datetime) -> str:
    t = to_tehran(dt)
    j = jdatetime.GregorianToJalali(t.year, t.month, t.day).getJalaliList()
    y, m, d = j
    return f"{y:04d}/{m:02d}/{d:02d}"

# ─────────────────────────────────────────────────────────────
# Price Formatter (فارسی + حذف صفر)
# ─────────────────────────────────────────────────────────────
def format_price(p: float) -> str:
    try:
        p = float(p)
    except:
        return str(p)

    if p >= 100:
        fmt = f"{p:.2f}"
    elif p >= 1:
        fmt = f"{p:.4f}"
    elif p >= 0.1:
        fmt = f"{p:.5f}"
    else:
        fmt = f"{p:.5f}"

    # حذف صفرهای انتهایی و نقطه اضافه
    fmt = fmt.rstrip("0").rstrip(".")
    return fmt

# ─────────────────────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────────────────────
def tg_keyboard_default():
    return {
        "keyboard": [
            [{"text": "📊 آمار"}, {"text": "📥 اشتراک"}],
            [{"text": "🧾 آخرین سیگنال‌ها"}, {"text": "ℹ️ راهنما"}],
            [{"text": "🆘 پشتیبانی"}],
        ],
        "resize_keyboard": True,
    }

HELP_TEXT = (
    "ℹ️ راهنما\n\n"
    "به سورس‌تریدر خوش آمدید!\n"
    "— سیگنال‌ها به‌صورت خودکار از استراتژی شما ارسال می‌شوند.\n"
    "— تاریخ‌ها به‌صورت شمسی و زمان بر اساس تهران نمایش داده می‌شود.\n\n"
    "دستورها:\n"
    "• /start — شروع و نمایش وضعیت اشتراک\n"
    "• 📥 اشتراک — دریافت و تأیید اشتراک (دستی)\n"
    "• 🧾 آخرین سیگنال‌ها — نمایش چند سیگنال آخر\n"
    "• 📊 آمار — آمار امروز/هفته/ماه (برد/باخت/WinRate/جمعِ سودهای مثبت)\n"
    "• ℹ️ راهنما — همین صفحه\n"
    "• 🆘 پشتیبانی — ارتباط با پشتیبانی\n\n"
    "پشتیبانی: @sourcetrader_support"
)

RISK_NOTE = (
    "ℹ️ توجه: حد ضرر و تارگت پیشنهادی صرفاً جهت مدیریت ریسک در نظر گرفته شده‌اند.\n"
    "ممکن است ربات قبل از رسیدن به این سطوح، «سیگنال بستن هوشمند» ارسال کند.\n"
    "انتخاب نهایی، مدیریت سرمایه و تصمیم به نگه‌داشتن یا بستن معامله همیشه با شماست."
)

def tg_send(chat_id: int, text: str, parse_mode: str = "Markdown", reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    # Retry ساده
    for i in range(3):
        try:
            r = httpx.post(f"{TG_API}/sendMessage", json=payload, timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return None

# ─────────────────────────────────────────────────────────────
# Users & Subscription helpers
# ─────────────────────────────────────────────────────────────
def get_user(uid: int):
    rows = db_exec("SELECT * FROM users WHERE id=%s", (uid,))
    return rows[0] if rows else None

def ensure_user(uid: int):
    u = get_user(uid)
    if not u:
        db_exec("INSERT INTO users(id, awaiting_tx, trial_started_at) VALUES(%s, FALSE, NOW())", (uid,))
    return get_user(uid)

def is_active_user(uid: int) -> bool:
    row = db_exec("SELECT expires_at FROM users WHERE id=%s", (uid,))
    if not row:
        return False
    exp = row[0]["expires_at"]
    if not exp:
        return False
    return now_dt() <= exp

def activate_trial(uid: int, days: int = TRIAL_DAYS):
    # اگر قبلاً trial_started_at دارد و expires_at هم دارد، دوباره ست نکن
    u = ensure_user(uid)
    if u and u.get("trial_started_at") and u.get("expires_at"):
        return
    exp = now_dt() + timedelta(days=days)
    db_exec(
        "UPDATE users SET expires_at=%s, trial_started_at=COALESCE(trial_started_at, NOW()) WHERE id=%s",
        (exp, uid),
    )

def set_awaiting_tx(uid: int, val: bool):
    db_exec("UPDATE users SET awaiting_tx=%s WHERE id=%s", (val, uid))

# ─────────────────────────────────────────────────────────────
# Signals helpers
# ─────────────────────────────────────────────────────────────
def insert_signal(symbol: str, side: str, price: float, t: datetime) -> int:
    rows = db_exec(
        "INSERT INTO signals(symbol, side, price, time) VALUES(%s,%s,%s,%s) RETURNING id",
        (symbol, side, price, t),
    )
    return rows[0]["id"]

def update_signal_ref(sid: int, ref_open_id: int):
    db_exec("UPDATE signals SET ref_open_id=%s WHERE id=%s", (ref_open_id, sid))

def set_signal_closed(close_id: int):
    db_exec("UPDATE signals SET closed_at=NOW() WHERE id=%s", (close_id,))

def _calc_pnl_pct_for_close(close_row):
    if not close_row or not close_row.get("ref_open_id"):
        return None
    op = db_exec("SELECT id, side, price FROM signals WHERE id=%s", (close_row["ref_open_id"],))
    if not op:
        return None
    open_price = float(op[0]["price"])
    close_price = float(close_row["price"])
    side = close_row["side"].upper()
    if open_price <= 0:
        return None
    if side == "CLOSE_LONG":
        pnl = (close_price - open_price) / open_price * 100.0
    elif side == "CLOSE_SHORT":
        pnl = (open_price - close_price) / open_price * 100.0
    else:
        return None
    return round(pnl, 4)

def backfill_missing_pnl():
    rows = db_exec(
        """
        SELECT id, side, price, ref_open_id
        FROM signals
        WHERE (side='CLOSE_LONG' OR side='CLOSE_SHORT')
          AND pnl_pct IS NULL
          AND ref_open_id IS NOT NULL
        """
    )
    updated = 0
    if not rows:
        return 0
    for r in rows:
        pnl = _calc_pnl_pct_for_close(r)
        if pnl is not None:
            db_exec("UPDATE signals SET pnl_pct=%s WHERE id=%s", (pnl, r["id"]))
            updated += 1
    return updated

# ─────────────────────────────────────────────────────────────
# Stats helpers
# ─────────────────────────────────────────────────────────────
def _stats_since_days(days: int):
    days = max(1, min(int(days), 90))
    q = f"""
    WITH c AS (
      SELECT pnl_pct
      FROM signals
      WHERE (side='CLOSE_LONG' OR side='CLOSE_SHORT')
        AND pnl_pct IS NOT NULL
        AND closed_at >= NOW() - INTERVAL '{days} days'
    )
    SELECT
      COUNT(*)                                         AS total,
      COUNT(*) FILTER (WHERE pnl_pct > 0)              AS wins,
      COUNT(*) FILTER (WHERE pnl_pct <= 0)             AS losses,
      COALESCE(AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END) * 100, 0) AS winrate,
      COALESCE(SUM(CASE WHEN pnl_pct > 0 THEN pnl_pct ELSE 0 END), 0)     AS sum_profit_pos
    FROM c;
    """
    rows = db_exec(q)
    if not rows:
        return {"total": 0, "wins": 0, "losses": 0, "winrate": 0.0, "sum_profit_pos": 0.0}
    r = rows[0]
    return {
        "total": int(r["total"] or 0),
        "wins": int(r["wins"] or 0),
        "losses": int(r["losses"] or 0),
        "winrate": round(float(r["winrate"] or 0.0), 1),
        "sum_profit_pos": round(float(r["sum_profit_pos"] or 0.0), 2),
    }

def _bar(winrate: float) -> str:
    winrate = max(0.0, min(100.0, winrate))
    blocks = 10
    filled = int(round(winrate / 10.0))
    return "█" * filled + "░" * (blocks - filled)

def format_stats_message():
    try:
        backfill_missing_pnl()
    except Exception:
        pass

    d1 = _stats_since_days(1)
    d7 = _stats_since_days(7)
    d30 = _stats_since_days(30)

    def block(title, d):
        return (
            f"• {title}:\n"
            f"  ├─ معاملات: {d['total']}\n"
            f"  ├─ برد: {d['wins']}  |  باخت: {d['losses']}\n"
            f"  ├─ WinRate: {d['winrate']}٪  { _bar(d['winrate']) }\n"
            f"  └─ جمعِ سودهای مثبت: +{d['sum_profit_pos']}٪\n"
        )

    msg = (
        "📊 *آمار عملکرد سیگنال‌ها*\n"
        "نتایج بر اساس معاملات «بسته‌شده» محاسبه شده‌اند.\n\n"
        + block("امروز", d1) + "\n"
        + block("هفته اخیر", d7) + "\n"
        + block("ماه اخیر", d30) + "\n"
        "ℹ️ نکته: مجموع سود فقط جمع «سودهای مثبت» است و زیان‌ها در این جمع لحاظ نشده‌اند."
    )
    return msg

# ─────────────────────────────────────────────────────────────
# Message builders (سیگنال‌ها)
# ─────────────────────────────────────────────────────────────
def side_fa(side_en: str) -> str:
    s = side_en.upper()
    if s == "LONG":
        return "Long"
    if s == "SHORT":
        return "Short"
    if s == "CLOSE_LONG":
        return "Close Long"
    if s == "CLOSE_SHORT":
        return "Close Short"
    return s

def format_signal_message(symbol: str, side: str, price: float, t: datetime, with_sltp=True):
    dt_str = jalali_date_str(t)
    p_str = format_price(price)
    title_icon = "🟢" if side.upper() == "LONG" else "🔴" if side.upper() == "SHORT" else "⚪️"
    sfa = side_fa(side)

    msg = (
        f"{title_icon} *سیگنال جدید*\n"
        f"نماد: `{symbol}`\n"
        f"جهت: *{sfa}*\n"
        f"قیمت: *{p_str}*\n"
        f"زمان: `{dt_str}`\n"
    )

    if with_sltp and SHOW_FIXED_SLTP and side.upper() in ("LONG", "SHORT"):
        if side.upper() == "LONG":
            sl = price * (1 - FIXED_SL_PCT)
            tp = price * (1 + FIXED_TP_PCT)
        else:
            sl = price * (1 + FIXED_SL_PCT)
            tp = price * (1 - FIXED_TP_PCT)
        msg += f"\nحد ضرر: `{format_price(sl)}`\n"
        msg += f"تارگت: `{format_price(tp)}`\n"

    msg += f"\n{RISK_NOTE}"
    return msg

# ─────────────────────────────────────────────────────────────
# FastAPI models
# ─────────────────────────────────────────────────────────────
class TVPayload(BaseModel):
    strategy: str | None = None
    symbol: str
    side: str
    price: float
    time: str
    secret: str | None = None
    ref: int | None = None     # شناسه مرجع برای کلوز
    ref_open_id: int | None = None

# ─────────────────────────────────────────────────────────────
# Routes: Health
# ─────────────────────────────────────────────────────────────
@app.get("/health")
def health_get():
    return {"status": "ok"}

@app.head("/health")
def health_head():
    return PlainTextResponse("", status_code=200)

# ─────────────────────────────────────────────────────────────
# Routes: Telegram Webhook
# ─────────────────────────────────────────────────────────────
@app.post("/tg/webhook")
async def tg_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    # Optional: telegram secret token check
    if TG_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TG_WEBHOOK_SECRET:
        return JSONResponse({"detail": "forbidden"}, status_code=403)

    data = await request.json()
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()

    # Ensure user exists
    ensure_user(user_id)

    # Map Persian buttons to commands
    txt = text
    if txt in ("ℹ️ راهنما", "/help"):
        tg_send(chat_id, HELP_TEXT, reply_markup=tg_keyboard_default())
        return {"ok": True}

    if txt in ("🆘 پشتیبانی",):
        tg_send(chat_id, "برای پشتیبانی و سوالات: @sourcetrader_support", reply_markup=tg_keyboard_default())
        return {"ok": True}

    if txt in ("📊 آمار", "/stats"):
        try:
            msg_stats = format_stats_message()
        except Exception:
            msg_stats = "❗️ خطا در محاسبه‌ی آمار. بعداً دوباره تلاش کنید."
        tg_send(chat_id, msg_stats, reply_markup=tg_keyboard_default())
        return {"ok": True}

    if txt in ("🧾 آخرین سیگنال‌ها", "/last"):
        rows = db_exec("SELECT symbol, side, price, time FROM signals ORDER BY id DESC LIMIT 5")
        if not rows:
            tg_send(chat_id, "فعلاً سیگنالی ثبت نشده.", reply_markup=tg_keyboard_default())
            return {"ok": True}
        lines = ["🧾 آخرین سیگنال‌ها:"]
        for r in rows:
            lines.append(
                f"- {r['symbol']} | {side_fa(r['side'])} | {format_price(r['price'])} | {jalali_date_str(r['time'])}"
            )
        tg_send(chat_id, "\n".join(lines), reply_markup=tg_keyboard_default())
        return {"ok": True}

    if txt in ("📥 اشتراک", "/subscribe"):
        set_awaiting_tx(user_id, True)
        tg_send(
            chat_id,
            "برای فعال‌سازی اشتراک، هش/لینک تراکنش کریپتو را همینجا ارسال کنید.\n"
            "پس از بررسی، اشتراک شما فعال می‌شود.",
            reply_markup=tg_keyboard_default(),
        )
        return {"ok": True}

    if txt == "/start":
        # فعال‌سازی تست اگر قبلاً نداشته
        activate_trial(user_id, TRIAL_DAYS)
        u = get_user(user_id)
        exp = u.get("expires_at")
        active = "✅ فعال" if is_active_user(user_id) else "⛔️ غیرفعال"
        exp_str = jalali_short(exp) if exp else "—"
        tg_send(
            chat_id,
            f"خوش آمدید 👋\nوضعیت اشتراک: {active}\nتاریخ انقضا: {exp_str}",
            reply_markup=tg_keyboard_default(),
        )
        return {"ok": True}

    # اگر در حالت انتظار TX هست:
    u = get_user(user_id)
    if u and u.get("awaiting_tx"):
        # هر متنی را به عنوان TXID می‌پذیریم و اشتراک را ۳۰ روز تمدید می‌کنیم
        db_exec("UPDATE users SET awaiting_tx=FALSE WHERE id=%s", (user_id,))
        new_exp = now_dt() + timedelta(days=30)
        db_exec("UPDATE users SET expires_at=%s WHERE id=%s", (new_exp, user_id))
        tg_send(
            chat_id,
            f"✅ پرداخت دریافت شد و اشتراک تا {jalali_short(new_exp)} فعال شد.",
            reply_markup=tg_keyboard_default(),
        )
        return {"ok": True}

    # اندازه‌گیری وضعیت
    if txt == "/status":
        u = get_user(user_id)
        exp = u.get("expires_at")
        active = "✅ فعال" if is_active_user(user_id) else "⛔️ غیرفعال"
        exp_str = jalali_short(exp) if exp else "—"
        tg_send(chat_id, f"وضعیت اشتراک: {active}\nتاریخ انقضا: {exp_str}", reply_markup=tg_keyboard_default())
        return {"ok": True}

    # پیش‌فرض: راهنما
    tg_send(chat_id, HELP_TEXT, reply_markup=tg_keyboard_default())
    return {"ok": True}

# ─────────────────────────────────────────────────────────────
# Routes: TradingView webhook
# ─────────────────────────────────────────────────────────────
@app.post("/tv")
async def tv_hook(payload: TVPayload):
    try:
        if WEBHOOK_SECRET and payload.secret != WEBHOOK_SECRET:
            return JSONResponse({"detail": "invalid secret"}, status_code=403)

        symbol = payload.symbol.upper()
        side = payload.side.upper()
        price = float(payload.price)
        t = datetime.fromisoformat(payload.time.replace("Z", "+00:00"))

        if symbol not in ALLOWED_SYMBOLS:
            return {"ok": True, "ignored": "symbol not allowed"}

        # ثبت سیگنال
        sid = insert_signal(symbol, side, price, t)

        # اگر CLOSE_* است، مرجع را داشته باشیم + بسته شدن را ست کنیم + pnl
        if side in ("CLOSE_LONG", "CLOSE_SHORT"):
            if payload.ref_open_id:
                update_signal_ref(sid, payload.ref_open_id)
            set_signal_closed(sid)
            # محاسبه‌ی PnL
            row = {"id": sid, "side": side, "price": price, "ref_open_id": payload.ref_open_id}
            pnl = _calc_pnl_pct_for_close(row)
            if pnl is not None:
                db_exec("UPDATE signals SET pnl_pct=%s WHERE id=%s", (pnl, sid))

        # ارسال پیام برای همه‌ی کاربران فعال
        users = db_exec("SELECT id FROM users WHERE expires_at IS NOT NULL AND expires_at >= NOW()")
        msg = format_signal_message(symbol, side, price, t, with_sltp=True)
        for u in users or []:
            tg_send(u["id"], msg, reply_markup=tg_keyboard_default())

        # اگر سیگنال باز (LONG/SHORT) بود و یک ref از طرف TV آوردی، آن ref را روی رکورد باز تنظیم کن
        if side in ("LONG", "SHORT") and payload.ref is not None:
            update_signal_ref(sid, payload.ref)

        return {"ok": True, "id": sid}
    except Exception as e:
        print("TV ERROR:", e, traceback.format_exc())
        return JSONResponse({"detail": "server error"}, status_code=500)

# ─────────────────────────────────────────────────────────────
# Admin (ساده)
# ─────────────────────────────────────────────────────────────
@app.get("/admin")
def admin_home(token: str = Query(default="")):
    if token != ADMIN_PANEL_TOKEN:
        return HTMLResponse("<h3>Forbidden</h3>", status_code=403)

    sigs = db_exec(
        "SELECT id, symbol, side, price, time, created_at, ref_open_id, pnl_pct, closed_at "
        "FROM signals ORDER BY id DESC LIMIT 50"
    )
    rows = []
    for s in sigs or []:
        rows.append(
            f"<tr>"
            f"<td>{s['id']}</td>"
            f"<td>{s['symbol']}</td>"
            f"<td>{s['side']}</td>"
            f"<td>{format_price(s['price'])}</td>"
            f"<td>{jalali_date_str(s['time'])}</td>"
            f"<td>{s.get('ref_open_id') or ''}</td>"
            f"<td>{'' if s.get('pnl_pct') is None else round(s['pnl_pct'], 2)}</td>"
            f"<td>{'' if not s.get('closed_at') else jalali_date_str(s['closed_at'])}</td>"
            f"</tr>"
        )
    html = f"""
    <html><head><meta charset="utf-8"><title>Admin</title>
    <style>
    body {{font-family: Vazirmatn, sans-serif; padding:20px;}}
    table {{border-collapse: collapse; width:100%;}}
    td,th {{border:1px solid #ccc; padding:6px; font-size:14px; text-align:center}}
    </style>
    </head><body>
    <h2>آخرین سیگنال‌ها</h2>
    <table>
      <tr><th>ID</th><th>Symbol</th><th>Side</th><th>Price</th><th>Time</th><th>Ref</th><th>PNL%</th><th>ClosedAt</th></tr>
      {''.join(rows)}
    </table>
    </body></html>
    """
    return HTMLResponse(html)

# ─────────────────────────────────────────────────────────────
# Cron
# ─────────────────────────────────────────────────────────────
def _should_send_daily_summary() -> bool:
    # هر بار که /cron صدا زده می‌شود، بررسی کن آیا در بازه 23:30 تهران هستیم (±2 دقیقه)
    tehran_now = to_tehran(now_dt())
    hh, mm = tehran_now.hour, tehran_now.minute
    return (hh == 23 and 28 <= mm <= 32)  # حول‌وحوش 23:30

def _daily_summary_message():
    d1 = _stats_since_days(1)
    # بهترین سیگنال روز (بیشترین سود مثبت)
    best = db_exec(
        "SELECT symbol, side, pnl_pct, closed_at FROM signals "
        "WHERE (side='CLOSE_LONG' OR side='CLOSE_SHORT') AND pnl_pct IS NOT NULL "
        "AND closed_at >= NOW() - INTERVAL '1 day' "
        "ORDER BY pnl_pct DESC LIMIT 1"
    )
    best_line = "—"
    if best:
        b = best[0]
        best_line = f"{b['symbol']} | {side_fa(b['side'])} | +{round(b['pnl_pct'],2)}٪"

    msg = (
        "🟢 *خلاصه روزانه سیگنال‌ها*\n"
        f"• تعداد معاملات بسته‌شده: {d1['total']}\n"
        f"• درصد موفقیت (WinRate): {d1['winrate']}٪\n"
        f"• بهترین سیگنال روز: {best_line}\n"
        f"• سود تجمعی اگر همه اجرا می‌شد: +{d1['sum_profit_pos']}٪\n"
        "_این آمار بر اساس معاملات بسته‌شدهٔ ۲۴ ساعت اخیر محاسبه شده است._"
    )
    return msg

@app.get("/cron")
@app.head("/cron")
def cron(token: str = Query(default="")):
    if token != CRON_TOKEN:
        return JSONResponse({"detail": "forbidden"}, status_code=403)

    # بک‌فیل PNL (در صورت نیاز)
    try:
        backfill_missing_pnl()
    except Exception:
        pass

    # خلاصه روزانه ساعت ۲۳:۳۰ تهران
    if _should_send_daily_summary():
        users = db_exec("SELECT id FROM users WHERE expires_at IS NOT NULL AND expires_at >= NOW()")
        if users:
            msg = _daily_summary_message()
            for u in users:
                tg_send(u["id"], msg)

    return {"ok": True}

# ─────────────────────────────────────────────────────────────
# Root
# ─────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return JSONResponse({"status": "not found"}, status_code=404)
