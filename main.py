from fastapi import FastAPI, Request
import hashlib
import requests

app = FastAPI()

BOT_TOKEN = "8645066724:AAFwkbpnQDmpAjEf-lf-3nraM-A72Q9pd8Q"
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Store verified fingerprints (use DB in real)
verified = {}

@app.post("/verify")
async def verify(req: Request):
    data = await req.json()

    fingerprint = data.get("fp")
    user_id = data.get("user")

    if not fingerprint or not user_id:
        return {"status":"error","message":"Invalid request"}

    hash_fp = hashlib.sha256(fingerprint.encode()).hexdigest()

    # Check duplicate device
    if hash_fp in verified:
        return {"status":"error","message":"Multiple devices detected"}

    verified[hash_fp] = user_id

    # Send verified command to bot
    requests.get(f"{BOT_API}/sendMessage?chat_id={user_id}&text=/verified")

    return {"status":"success","message":"Verified Successfully"}
