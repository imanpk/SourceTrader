import os
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

app = FastAPI(title="TradingView → Telegram")

class TVPayload(BaseModel):
    strategy: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    price: Optional[float] = None
    time: Optional[str] = None
    secret: Optional[str] = None

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/tv")
async def tv_hook(payload: TVPayload):
    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    text = (
        f"📡 <b>{payload.strategy or 'Signal'}</b>\n"
        f"🔹 Symbol: <b>{payload.symbol or 'UNKNOWN'}</b>\n"
        f"🔸 Side: <b>{(payload.side or 'N/A').upper()}</b>\n"
        f"💲 Price: <b>{payload.price if payload.price is not None else 'N/A'}</b>\n"
        f"🕒 Time: <code>{payload.time or ''}</code>"
    )

    async with httpx.AsyncClient(timeout=15) as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        await client.post(url, data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        })

    return {"ok": True}
