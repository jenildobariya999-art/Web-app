import re
import json
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler,
    ConversationHandler
)

BOT_TOKEN = "8077986423:AAF37oJEJA7sq4W2OSp1GClNh00ZBQXsOwA"

ADMINS = [7528813331, 6925391837]

bot_fund = 500.0
total_payout = 0.0

FUND_CHANNELS = []
PAYOUT_CHANNEL_ID = None

live_msg_ids = {}
user_ids = set()

custom_text = None
custom_buttons = None

ADD, REMOVE, SETFUND, SETPAY, BROADCAST, EDITUI, REMOVECH = range(7)

DATA_FILE = "/tmp/data.json"

# ===== LOAD / SAVE =====
def load_data():
    global bot_fund, total_payout, FUND_CHANNELS, PAYOUT_CHANNEL_ID
    global custom_text, custom_buttons

    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            bot_fund = data.get("fund", 500.0)
            total_payout = data.get("payout", 0.0)
            FUND_CHANNELS = data.get("channels", [])
            PAYOUT_CHANNEL_ID = data.get("payout_channel", None)
            custom_text = data.get("text", None)
            custom_buttons = data.get("buttons", None)

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump({
            "fund": bot_fund,
            "payout": total_payout,
            "channels": FUND_CHANNELS,
            "payout_channel": PAYOUT_CHANNEL_ID,
            "text": custom_text,
            "buttons": custom_buttons
        }, f)

# ===== UI =====
def get_text():
    return f"💎 FUND: ₹ {bot_fund:.2f}"

def get_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💰 ₹ {bot_fund:.2f}", callback_data="x")]
    ])

# ===== POST LIVE =====
async def post_live(context):
    for ch in FUND_CHANNELS:
        try:
            if ch in live_msg_ids:
                await context.bot.edit_message_text(
                    chat_id=ch,
                    message_id=live_msg_ids[ch],
                    text=get_text(),
                    reply_markup=get_buttons()
                )
            else:
                msg = await context.bot.send_message(
                    chat_id=ch,
                    text=get_text(),
                    reply_markup=get_buttons()
                )
                live_msg_ids[ch] = msg.message_id
        except:
            pass

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_ids.add(update.effective_user.id)
    await update.message.reply_text(get_text(), reply_markup=get_buttons())

# ===== PAYOUT DETECT =====
async def payout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_fund, total_payout

    if not update.channel_post:
        return

    if update.channel_post.chat.id != PAYOUT_CHANNEL_ID:
        return

    text = update.channel_post.text or ""
    match = re.search(r'([\d.]+)', text)

    if match:
        amt = float(match.group(1))
        bot_fund -= amt
        total_payout += amt
        save_data()
        await post_live(context)

# ===== ADMIN =====
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    await update.message.reply_text("ADMIN PANEL")

# ===== APP INIT =====
app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("adminpanel", admin))
app.add_handler(MessageHandler(filters.ChatType.CHANNEL, payout))

# ===== VERCEL HANDLER =====
async def handler(request):
    load_data()

    if request.method == "POST":
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.initialize()
        await app.process_update(update)

    return {
        "statusCode": 200,
        "body": "ok"
    }
