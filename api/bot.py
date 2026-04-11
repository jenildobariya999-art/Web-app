import os
import json
from fastapi import FastAPI, Request
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)

app = FastAPI()

# ===== YOUR SIMPLE START =====
def get_text():
    return "💎 FUND SYSTEM ACTIVE 💎"

def get_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 FUND", callback_data="x")]
    ])

# ===== WEBHOOK ENDPOINT =====
@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, bot)

    # ---- /start ----
    if update.message and update.message.text == "/start":
        await update.message.reply_text(
            get_text(),
            reply_markup=get_buttons()
        )

    return {"ok": True}
