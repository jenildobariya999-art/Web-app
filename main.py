from fastapi import FastAPI, Request
import httpx

app = FastAPI()

BOT_TOKEN = "8464660617:AAE5sUjb_Y-buaAri4UYIiwoIg1eyk4xQuY"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Simulate database (in production use DB)
user_tokens = {}  # key=user_id, value=token

@app.get("/")
async def verify(token: str = None, user_id: str = None, request: Request = None):
    client_ip = request.client.host

    # Check inputs
    if not token or not user_id:
        return {"status":"error","message":"Missing token or user_id"}

    # TODO: In production, fetch saved token from DB
    saved_token = user_tokens.get(user_id)
    if saved_token != token:
        return {"status":"error","message":"Invalid token"}

    try:
        # Trigger /verified command
        url = f"{TELEGRAM_API}/sendMessage?chat_id={user_id}&text=/verified"
        httpx.get(url)
        return {"status":"success","message":"User verified"}
    except Exception as e:
        return {"status":"error","message":str(e)}
