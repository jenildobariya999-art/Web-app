import os
from fastapi import FastAPI, Request
from telegram import Bot, Update

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot)

    if update.message and update.message.text == "/start":
        await update.message.reply_text("💎 Bot is Live on Vercel!")

    return {"ok": True}
