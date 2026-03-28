from fastapi import FastAPI, Request
import httpx     # For calling Telegram API

app = FastAPI()

BOT_TOKEN = "8464660617:AAE5sUjb_Y-buaAri4UYIiwoIg1eyk4xQuY"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

@app.get("/")
def home(user_id: str = None, request: Request = None):
    """
    This page verifies device/IP and then triggers /verified command
    to the user via Telegram Bot API.
    """

    client_host = request.client.host  # visitor IP

    # Here you can add device/IP logic (block, allow lists, rate limits etc)
    # For demo, we auto verify.

    if user_id:
        # Send /verified command to bot on behalf of user
        text = "/verified"
        send_url = f"{TELEGRAM_API}/sendMessage?chat_id={user_id}&text={text}"
        httpx.get(send_url)

        return {"status": "success", "message": "Verification completed."}
    else:
        return {"status": "error", "message": "Invalid user."}
