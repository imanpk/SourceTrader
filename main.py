import os, re, json, datetime
from typing import Optional, List, Tuple
from fastapi import FastAPI, HTTPException, Request, Query
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

# Fixed TP/SL strategy (configurable)
FIXED_SL_PCT = float(os.getenv("FIXED_SL_PCT", "0.02"))  # 2%
FIXED_TP_PCT = float(os.getenv("FIXED_TP_PCT", "0.04"))  # 4%
SHOW_FIXED_SLTP = os.getenv("SHOW_FIXED_SLTP", "true").lower() in ("1","true","yes")
CRON_TOKEN = os.getenv("CRON_TOKEN", "")  # optional security token for /cron

if not (BOT_TOKEN and WEBHOOK_SECRET and TELEGRAM_WEBHOOK_SECRET and DATABASE_URL):
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET, TELEGRAM_WEBHOOK_SECRET, DATABASE_URL")

app = FastAPI(title="SourceTrader MVP (FA + Jalali + Daily Summary)")

# ===================== TIME HELPERS =====================
TEHRAN = ZoneInfo("Asia/Tehran")

def now_dt() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def to_tehran(dt: datetime.datetime) -> datetime.datetime:
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(TEHRAN)

def jalali_str(dt: datetime.datetime, with_time: bool = True) -> str:
    dt_th = to_tehran(dt)
    j = jdatetime.datetime.fromgregorian(datetime=dt_th)
    return f"{j.strftime('%Y/%m/%d')} - {dt_th.strftime('%H:%M')}" if with_time else j.strftime('%Y/%m/%d')

def tehran_day_bounds(dt_utc: Optional[datetime.datetime] = None) -> Tuple[datetime.datetime, datetime.datetime, str]:
    """Return today's Tehran [start_utc, end_utc) and tehran_date_str (YYYY-MM-DD)."""
    base = to_tehran(dt_utc or now_dt())
    tehran_date = base.date()
    start_th = datetime.datetime.combine(tehran_date, datetime.time(0,0), tzinfo=TEHRAN)
    end_th = start_th + datetime.timedelta(days=1)
    return start_th.astimezone(datetime.timezone.utc), end_th.astimezone(datetime.timezone.utc), tehran_date.isoformat()

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
    # جدول users (همون قبلی شما)
    db_exec("""
    CREATE TABLE IF NOT EXISTS users (
        id BIGINT PRIMARY KEY,
        expires_at TIMESTAMPTZ,
        awaiting_tx BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """)

    # جدول signals (حداقل ستون‌های پایه)
    db_exec("""
    CREATE TABLE IF NOT EXISTS signals (
        id SERIAL PRIMARY KEY,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,              -- LONG | SHORT | CLOSE_LONG | CLOSE_SHORT
        price DOUBLE PRECISION NOT NULL,
        time TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """)

def migrate_db():
    # ستون‌هایی که نسخه‌های جدید کد نیاز دارند:
    db_exec("ALTER TABLE signals ADD COLUMN IF NOT EXISTS ref_open_id INTEGER")
    db_exec("ALTER TABLE signals ADD COLUMN IF NOT EXISTS pnl_pct DOUBLE PRECISION")
    db_exec("ALTER TABLE signals ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ")

    # ایندکس‌های مفید (اختیاری ولی بهتره):
    db_exec("CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(time)")
    db_exec("CREATE INDEX IF NOT EXISTS idx_signals_ref ON signals(ref_open_id)")


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

def get_meta(key: str) -> Optional[str]:
    rows = db_exec("SELECT value FROM meta WHERE key=%s", (key,))
    return rows[0]["value"] if rows else None

def set_meta(key: str, value: str):
    db_exec("""
    INSERT INTO meta(key,value) VALUES (%s,%s)
    ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
    """, (key, value))

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

def update_signal_ref(sid: int, ref_open_id: Optional[int]):
    if ref_open_id:
        db_exec("UPDATE signals SET ref_open_id=%s WHERE id=%s", (ref_open_id, sid))

def find_ref_open_id(symbol: str, close_side: str) -> Optional[int]:
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
# Persian buttons
BTN_LAST = "📈 آخرین سیگنال‌ها"
BTN_STATUS = "🔑 وضعیت اشتراک"
BTN_SUBSCRIBE = "💳 تمدید اشتراک"
BTN_HELP = "ℹ️ راهنما"
BTN_SUPPORT = "🆘 پشتیبانی"

MAIN_KB = {
    "keyboard": [
        [{"text": BTN_LAST}, {"text": BTN_STATUS}],
        [{"text": BTN_SUBSCRIBE}, {"text": BTN_HELP}],
        [{"text": BTN_SUPPORT}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

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

def is_cmd(txt: str, *cmds: str) -> bool:
    t = (txt or "").strip()
    if not t: return False
    for c in cmds:
        if t == c or t.startswith(c + " "):  # allow args
            return True
    return False

# === نمایش جهت‌ها ===
def disp_side(side: str) -> str:
    s = (side or "").upper()
    if s == "LONG": return "LONG"
    if s == "SHORT": return "SHORT"
    if s == "CLOSE_LONG": return "Close LONG"
    if s == "CLOSE_SHORT": return "Close SHORT"
    return side or "N/A"

# === فرمت قیمت (طبق خواسته جدید) ===
def fmt_price(price) -> str:
    if not isinstance(price, (int, float)):
        return "N/A"
    p = abs(price)
    if p >= 100:
        out = f"{price:.2f}"
    elif p >= 1:
        out = f"{price:.4f}"
    elif p >= 0.1:
        out = f"{price:.5f}"
    else:
        out = f"{price:.5f}"
    out = out.rstrip('0').rstrip('.') if '.' in out else out
    return out

def compute_fixed_sl_tp(price: Optional[float], side: str) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(price, (int,float)) or price <= 0:
        return None, None
    s = (side or "").upper()
    if s == "LONG":
        sl = price * (1 - FIXED_SL_PCT)
        tp = price * (1 + FIXED_TP_PCT)
    elif s == "SHORT":
        sl = price * (1 + FIXED_SL_PCT)  # stop above entry
        tp = price * (1 - FIXED_TP_PCT)
    else:
        return None, None
    return sl, tp

def signal_disclaimer_in_fa() -> str:
    return (
        "\n\nℹ️ <b>توجه:</b> سطوح حدضرر/تارگت در این پیام بر اساس یک استراتژی ثابت آموزشی محاسبه شده‌اند "
        f"(SL={int(FIXED_SL_PCT*100)}٪، TP={int(FIXED_TP_PCT*100)}٪). "
        "ربات ممکن است قبل از رسیدن به این سطوح، «سیگنال بستن هوشمند» ارسال کند؛ "
        "تصمیم نهایی، اندازه موقعیت، و مدیریت ریسک به عهده شماست."
    )

def format_signal(title, symbol, side, price, t, sig_id=None, sl=None, tp=None):
    price_str = fmt_price(price)
    lines = []
    lines.append(f"📡 <b>{title}</b>" + (f"  #{sig_id}" if sig_id else ""))
    lines.append(f"📌 نماد: <b>{symbol}</b>")
    lines.append(f"🧭 جهت: <b>{disp_side(side)}</b>")
    lines.append(f"💲 قیمت: <b>{price_str}</b>")
    lines.append(f"🕒 زمان ارسال: <code>{jalali_str(now_dt(), True)}</code>")

    # اگر Close نیست و SL/TP از TV نیامده و تنظیم فعال است → محاسبه ثابت
    if (not str(side).upper().startswith("CLOSE")) and SHOW_FIXED_SLTP and (sl is None and tp is None):
        sl_c, tp_c = compute_fixed_sl_tp(price, side)
        if sl_c is not None:
            lines.append(f"⛔ حد ضرر: <b>{fmt_price(sl_c)}</b>")
        if tp_c is not None:
            lines.append(f"🎯 تارگت: <b>{fmt_price(tp_c)}</b>")
        lines.append(signal_disclaimer_in_fa())
    else:
        # اگر TV sl/tp داده بود هم نشان بدهیم
        if isinstance(sl, (int,float)):
            lines.append(f"⛔ حد ضرر: <b>{fmt_price(sl)}</b>")
        if isinstance(tp, (int,float)):
            lines.append(f"🎯 تارگت: <b>{fmt_price(tp)}</b>")
        if not str(side).upper().startswith("CLOSE"):
            lines.append(signal_disclaimer_in_fa())

    return "\n".join(lines)

def extract_txid(text: str) -> Optional[str]:
    if not text: return None
    text = text.strip()
    m = re.search(r'(0x)?[A-Fa-f0-9]{32,}', text)
    return m.group(0) if m else None

# ===================== MODELS =====================
class TVPayload(BaseModel):
    strategy: Optional[str] = None
    symbol:   Optional[str] = None
    side:     Optional[str] = None   # LONG/SHORT/CLOSE_LONG/CLOSE_SHORT
    price:    Optional[float] = None
    time:     Optional[str] = None
    secret:   Optional[str] = None
    sl:       Optional[float] = None
    tp:       Optional[float] = None
    tf:       Optional[str] = None

# ===================== STARTUP =====================
init_db()
migrate_db()
# ===================== ROUTES =====================
# Health: GET + HEAD
@app.get("/health")
def health_get():
    return {"status": "ok"}

@app.head("/health")
def health_head():
    return PlainTextResponse("", status_code=200)

# Daily summary cron hook (call every 5min via UptimeRobot)
@app.get("/cron")
def cron(token: Optional[str] = Query(default=None)):
    if CRON_TOKEN and token != CRON_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")
    # only at/after 23:30 Tehran and only once per day
    now = now_dt()
    now_th = to_tehran(now)
    if now_th.hour < 23 or (now_th.hour == 23 and now_th.minute < 30):
        return {"ok": True, "skipped": "too_early"}
    # check last sent
    _, _, tehran_date_str = tehran_day_bounds(now)
    last_sent = get_meta("last_summary_tehran_date")
    if last_sent == tehran_date_str:
        return {"ok": True, "skipped": "already_sent"}

    # compute summary for "today" Tehran
    start_utc, end_utc, _ = tehran_day_bounds(now)
    summary_text = build_daily_summary(start_utc, end_utc, now_th)
    users = list_active_users()
    for uid in users:
        try:
            # broadcast in Persian
            import asyncio
            asyncio.run(tg_send(uid, summary_text))
        except Exception:
            pass
    set_meta("last_summary_tehran_date", tehran_date_str)
    return {"ok": True, "sent_to": len(users)}


@app.head("/cron")
def cron_head(token: Optional[str] = Query(default=None)):
    if CRON_TOKEN and token != CRON_TOKEN:
        return PlainTextResponse("", status_code=401)
    # HEAD فقط چک سلامت؛ اجرای گزارش روزانه اینجا انجام نمی‌شود
    return PlainTextResponse("", status_code=200)


def build_daily_summary(start_utc: datetime.datetime, end_utc: datetime.datetime, now_th: datetime.datetime) -> str:
    # تعداد سیگنال‌ها (Open های امروز)
    opens = db_exec("""
        SELECT id, symbol, side, price, created_at
        FROM signals
        WHERE created_at >= %s AND created_at < %s
          AND side IN ('LONG','SHORT')
        ORDER BY id ASC
    """, (start_utc, end_utc)) or []

    # کلوزهایی که امروز اتفاق افتاد (برای محاسبه سود/زیان)
    closes = db_exec("""
        SELECT id, symbol, side, price, ref_open_id, created_at
        FROM signals
        WHERE created_at >= %s AND created_at < %s
          AND side IN ('CLOSE_LONG','CLOSE_SHORT')
          AND ref_open_id IS NOT NULL
        ORDER BY id ASC
    """, (start_utc, end_utc)) or []

    wins = 0
    losses = 0
    total_pnl = 0.0
    best_pnl = None
    best_sig = None

    for c in closes:
        ref_id = c["ref_open_id"]
        op = db_exec("SELECT id, symbol, side, price, created_at FROM signals WHERE id=%s", (ref_id,))
        if not op: 
            continue
        o = op[0]
        open_side = o["side"].upper()
        open_price = o["price"]
        close_price = c["price"]
        if not isinstance(open_price, (int,float)) or not isinstance(close_price, (int,float)) or open_price <= 0:
            continue
        if c["side"].upper() == "CLOSE_LONG" and open_side == "LONG":
            pnl = (close_price - open_price) / open_price
        elif c["side"].upper() == "CLOSE_SHORT" and open_side == "SHORT":
            pnl = (open_price - close_price) / open_price
        else:
            continue

        total_pnl += pnl
        if pnl > 0: wins += 1
        elif pnl < 0: losses += 1

        if (best_pnl is None) or (pnl > best_pnl):
            best_pnl = pnl
            best_sig = {"symbol": o["symbol"], "pnl": pnl, "open_id": o["id"], "close_id": c["id"]}

    total_trades = wins + losses
    winrate = (wins / total_trades * 100.0) if total_trades > 0 else None

    def pct(x):
        return f"{x*100:.2f}%" if x is not None else "N/A"

    title = f"🗓 گزارش روزانه سیگنال‌ها - {now_th.strftime('%Y/%m/%d')} (تهران)"
    lines = [title, ""]
    lines.append(f"• تعداد سیگنال‌های امروز (ورود): {len(opens)}")
    lines.append(f"• تعداد معاملات بسته‌شده امروز: {total_trades}")
    lines.append(f"• درصد موفقیت (WinRate): {f'{winrate:.1f}%' if winrate is not None else 'N/A'}")
    if best_sig:
        lines.append(f"• بهترین سیگنال: {best_sig['symbol']}  ({pct(best_sig['pnl'])})  #{best_sig['open_id']}→#{best_sig['close_id']}")
    else:
        lines.append("• بهترین سیگنال: N/A")
    lines.append(f"• سود تجمعی اگر همه اجرا می‌شد: {pct(total_pnl)}")
    lines.append("\n⚠️ این آمار صرفاً اطلاع‌رسانی است و توصیه سرمایه‌گذاری محسوب نمی‌شود.")
    return "\n".join(lines)

# TradingView webhook
@app.post("/tv")
async def tv_hook(payload: TVPayload):
    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    title  = payload.strategy or "Signal"
    symbol = payload.symbol or "UNKNOWN"
    side   = (payload.side or "N/A").upper()

    # save & get ID
    sid = save_signal(symbol, side, payload.price, payload.time)

    # Close → reference last Open and persist ref
    if side in ("CLOSE_LONG", "CLOSE_SHORT"):
        ref = find_ref_open_id(symbol, side)
        if ref:
            update_signal_ref(sid, ref)
            title = f"{title} (بستن #{ref})"

    text = format_signal(
        title=title,
        symbol=symbol,
        side=side,
        price=payload.price,
        t=payload.time,
        sig_id=None if side.startswith("CLOSE") else sid,  # code only on Open
        sl=payload.sl,
        tp=payload.tp,
    )

    users = list_active_users()
    async with httpx.AsyncClient(timeout=20) as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        for uid in users:
            try:
                await client.post(url, data={
                    "chat_id": uid,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                })
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

    # Commands or Persian buttons
    t = text
    is_last      = is_cmd(t, "/last")      or t == BTN_LAST
    is_status    = is_cmd(t, "/status")    or t == BTN_STATUS
    is_subscribe = is_cmd(t, "/subscribe") or t == BTN_SUBSCRIBE
    is_help      = is_cmd(t, "/help")      or t == BTN_HELP
    is_support   = is_cmd(t, "/support")   or t == BTN_SUPPORT
    is_menu      = is_cmd(t, "/menu")

    if is_cmd(t, "/start"):
        ensure_trial(uid)
        row = db_exec("SELECT subscription_expires_at FROM users WHERE telegram_id=%s", (uid,))
        exp = row[0]["subscription_expires_at"] if row else None
        exp_txt = jalali_str(exp, with_time=True) if exp else "N/A"
        msg = (
            "👋 به ربات سیگنال SourceTrader خوش آمدید!\n\n"
            f"✅ {TRIAL_DAYS} روز اشتراک رایگان برای تست فعال شد.\n"
            f"⏰ انقضا: <b>{exp_txt}</b>\n\n"
            "از دکمه‌های زیر استفاده کنید:"
        )
        await tg_send(chat_id, msg, reply_markup=MAIN_KB); return {"ok": True}

    if is_menu:
        await tg_send(chat_id, "منوی دستورات:", reply_markup=MAIN_KB); return {"ok": True}

    if is_help:
        msg = (
            "ℹ️ <b>راهنمای استفاده از ربات SourceTrader</b>\n\n"
            "📈 <b>آخرین سیگنال‌ها</b>: مشاهده ۵ سیگنال اخیر بازار.\n"
            "🔑 <b>وضعیت اشتراک</b>: بررسی فعال/غیرفعال بودن اشتراک و تاریخ انقضا.\n"
            "💳 <b>تمدید اشتراک</b>: راهنمای پرداخت و ثبت درخواست تمدید.\n"
            "🆘 <b>پشتیبانی</b>: در صورت هرگونه سوال یا مشکل:\n"
            "<b>@sourcetrader_support</b>\n\n"
            "⚠️ توجه: سیگنال‌ها صرفاً آموزشی هستند. مسئولیت تصمیم‌گیری، مدیریت ریسک و سرمایه با کاربر است."
        )
        await tg_send(chat_id, msg, reply_markup=MAIN_KB); return {"ok": True}

    if is_support:
        await tg_send(chat_id, "🆘 برای سوالات و پشتیبانی پیام دهید:\n@sourcetrader_support", reply_markup=MAIN_KB); return {"ok": True}

    if is_status:
        row = db_exec("SELECT subscription_expires_at FROM users WHERE telegram_id=%s", (uid,))
        exp = row[0]["subscription_expires_at"] if row else None
        active = "✅ فعال" if is_active_user(uid) else "⛔️ غیرفعال"
        exp_txt = jalali_str(exp, with_time=True) if exp else "N/A"
        await tg_send(chat_id, f"🔑 وضعیت اشتراک: {active}\n⏰ انقضا: <b>{exp_txt}</b>", reply_markup=MAIN_KB); return {"ok": True}

    if is_last:
        rows = db_exec("SELECT id, symbol, side, price, time, created_at FROM signals ORDER BY id DESC LIMIT 5")
        if not rows:
            await tg_send(chat_id, "هنوز سیگنالی ثبت نشده.", reply_markup=MAIN_KB); return {"ok": True}
        lines = []
        for r in rows:
            created = r["created_at"]
            created_text = jalali_str(created, with_time=True) if created else "-"
            price_txt = fmt_price(r["price"])
            lines.append(f"• #{r['id']}  {r['symbol']} | {disp_side(r['side'])} | {price_txt} | {created_text}")
        await tg_send(chat_id, "📈 آخرین سیگنال‌ها:\n" + "\n".join(lines), reply_markup=MAIN_KB); return {"ok": True}

    if is_subscribe:
        db_exec("UPDATE users SET awaiting_tx=TRUE WHERE telegram_id=%s", (uid,))
        msg = (
            "💳 <b>تمدید اشتراک</b>\n\n"
            f"1) مبلغ را به آدرس زیر ارسال کنید:\n"
            f"   • آدرس: <code>{PAYMENT_ADDRESS}</code>\n"
            f"   • شبکه: <b>{PAYMENT_NETWORK}</b>\n"
            "2) پس از پرداخت، همین‌جا «هش تراکنش یا لینک اکسپلورر» را ارسال کنید (بدون فرمت خاص).\n"
            "3) پس از بررسی، اشتراک شما تمدید می‌شود. ✅"
        )
        buttons = {
            "inline_keyboard": [
                [{"text": "📋 کپی آدرس", "switch_inline_query_current_chat": PAYMENT_ADDRESS}],
                [{"text": "🧭 Tronscan", "url": "https://tronscan.org/"}]
            ]
        }
        await tg_send(chat_id, msg, reply_markup=buttons); return {"ok": True}

    if is_cmd(t, "/tx"):
        parts = t.split()
        if len(parts) < 2:
            await tg_send(chat_id, "TXID نامعتبر. مثال:\n/tx f1a2b3c4...", reply_markup=MAIN_KB); return {"ok": True}
        txid = parts[1].strip()
        db_exec("INSERT INTO payments(telegram_id, txid, status, created_at) VALUES (%s,%s,'pending',%s)", (uid, txid, now_dt()))
        db_exec("UPDATE users SET awaiting_tx=FALSE WHERE telegram_id=%s", (uid,))
        await tg_send(chat_id, "✅ درخواست تمدید ثبت شد. پس از تأیید، اشتراک تمدید می‌شود.", reply_markup=MAIN_KB)
        await tg_send_to_admins(f"🧾 پرداخت جدید:\nUser: {uid}\nTXID: {txid}\nتایید: /confirm {uid} 30"); return {"ok": True}

    # Admin: debug
    if is_cmd(t, "/debug"):
        if str(uid) not in ADMIN_IDS:
            await tg_send(chat_id, "⛔️ فقط ادمین.", reply_markup=MAIN_KB); return {"ok": True}
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
        await tg_send(chat_id, f"<code>{msg}</code>", reply_markup=MAIN_KB); return {"ok": True}

    # Admin: تایید پرداخت و تمدید
    if is_cmd(t, "/confirm"):
        if str(uid) not in ADMIN_IDS:
            await tg_send(chat_id, "⛔️ فقط ادمین.", reply_markup=MAIN_KB); return {"ok": True}
        parts = t.split()
        if len(parts) < 3:
            await tg_send(chat_id, "استفاده:\n/confirm <user_id> <days>", reply_markup=MAIN_KB); return {"ok": True}
        try:
            target, days = int(parts[1]), int(parts[2])
            new_exp = extend_subscription(target, days)
            db_exec("UPDATE payments SET status='approved' WHERE telegram_id=%s AND status='pending'", (target,))
            await tg_send(chat_id, f"✅ تمدید شد تا: <b>{jalali_str(new_exp, with_time=True)}</b>", reply_markup=MAIN_KB)
            await tg_send(target, f"🎉 اشتراک شما تمدید شد تا: <b>{jalali_str(new_exp, with_time=True)}</b>")
        except Exception as e:
            await tg_send(chat_id, f"خطا در تایید: {e}", reply_markup=MAIN_KB)
        return {"ok": True}

    if is_cmd(t, "/broadcast"):
        if str(uid) not in ADMIN_IDS:
            await tg_send(chat_id, "⛔️ فقط ادمین.", reply_markup=MAIN_KB); return {"ok": True}
        msg = t.replace("/broadcast", "", 1).strip()
        if not msg:
            await tg_send(chat_id, "متن خالی است.", reply_markup=MAIN_KB); return {"ok": True}
        rows = db_exec("SELECT telegram_id FROM users", ())
        for r in rows or []:
            try: await tg_send(int(r["telegram_id"]), f"📢 {msg}")
            except: pass
        await tg_send(chat_id, "✅ ارسال شد.", reply_markup=MAIN_KB); return {"ok": True}

    # متن آزاد: اگر کاربر در حالت انتظار TX باشد، پرداخت را ثبت کن
    row = db_exec("SELECT awaiting_tx FROM users WHERE telegram_id=%s", (uid,))
    if row and row[0]["awaiting_tx"]:
        tx = extract_txid(t) or t
        db_exec("INSERT INTO payments(telegram_id, txid, status, created_at) VALUES (%s,%s,'pending',%s)", (uid, tx, now_dt()))
        db_exec("UPDATE users SET awaiting_tx=FALSE WHERE telegram_id=%s", (uid,))
        await tg_send(chat_id, "✅ درخواست تمدید ثبت شد. پس از تأیید، اشتراک شما تمدید می‌شود.", reply_markup=MAIN_KB)
        await tg_send_to_admins(f"🧾 پرداخت جدید:\nUser: {uid}\nTXID: {tx}\nتایید: /confirm {uid} 30")
        return {"ok": True}

    # اگر شبیه TXID بود، ثبت کن
    tx_guess = extract_txid(t)
    if tx_guess:
        db_exec("INSERT INTO payments(telegram_id, txid, status, created_at) VALUES (%s,%s,'pending',%s)", (uid, tx_guess, now_dt()))
        await tg_send(chat_id, "✅ درخواست تمدید ثبت شد. پس از تأیید، اشتراک شما تمدید می‌شود.", reply_markup=MAIN_KB)
        await tg_send_to_admins(f"🧾 پرداخت جدید:\nUser: {uid}\nTXID: {tx_guess}\nتایید: /confirm {uid} 30")
        return {"ok": True}

    # پیش‌فرض
    await tg_send(chat_id, "منوی دستورات:", reply_markup=MAIN_KB); return {"ok": True}

# ===================== SIMPLE ADMIN PANEL =====================
@app.get("/admin", response_class=HTMLResponse)
def admin_home(token: str):
    if not ADMIN_PANEL_TOKEN or token != ADMIN_PANEL_TOKEN:
        return HTMLResponse("<h3>Unauthorized</h3>", status_code=401)
    users = db_exec("SELECT telegram_id, username, subscription_expires_at, is_admin, joined_at, awaiting_tx FROM users ORDER BY subscription_expires_at DESC NULLS LAST")
    pays  = db_exec("SELECT id, telegram_id, txid, status, created_at FROM payments ORDER BY id DESC LIMIT 50")
    sigs  = db_exec("SELECT id, symbol, side, price, time, created_at, ref_open_id FROM signals ORDER BY id DESC LIMIT 50")

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

    html.append("<h3>Signals (last 50)</h3><table><tr><th>ID</th><th>Symbol</th><th>Side</th><th>Price</th><th>Created</th><th>Ref Open</th></tr>")
    for s in sigs or []:
        created_txt = jalali_str(s['created_at'], True) if s.get('created_at') else "-"
        price_txt = fmt_price(s.get('price'))
        html.append(row([s['id'], s['symbol'], disp_side(s['side']), price_txt, created_txt, s.get('ref_open_id') or "—"]))
    html.append("</table>")

    html.append("</body></html>")
    return "".join(html)
