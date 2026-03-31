from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
import hashlib
import os
import redis
import requests

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
REDIS_URL = os.getenv("REDIS_URL")

r = redis.from_url(REDIS_URL, decode_responses=True)

@app.get("/")
def home():
    return FileResponse("index.html")

@app.post("/verify")
async def verify(req: Request):
    data = await req.json()

    uid = str(data.get("uid", ""))
    ref = str(data.get("ref", ""))
    ip = str(data.get("ip", ""))
    vpn = bool(data.get("vpn", False))
    tg = bool(data.get("telegram", False))

    fingerprint = str(data.get("fingerprint", ""))

    if uid == "" or fingerprint == "":
        return {
            "status": "error",
            "message": "Invalid verification request"
        }

    if vpn:
        return {
            "status": "error",
            "message": "VPN / Proxy detected"
        }

    if not tg:
        return {
            "status": "error",
            "message": "Open this page inside Telegram"
        }

    fp_hash = hashlib.sha256(fingerprint.encode()).hexdigest()

    old_uid = r.get("fp:" + fp_hash)

    if old_uid and old_uid != uid:
        return {
            "status": "error",
            "message": "This device is already used"
        }

    r.set("fp:" + fp_hash, uid)
    r.set("userfp:" + uid, fp_hash)

    if ref != "" and ref != uid:
        already_referred = r.get("refdone:" + uid)

        if not already_referred:
            ref_fp = r.get("userfp:" + ref)

            if ref_fp != fp_hash:
                r.set("refdone:" + uid, "1")
                r.incr("refs:" + ref)

    requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        params={
            "chat_id": uid,
            "text": "/verified"
        }
    )

    return {
        "status": "success",
        "message": "Verified successfully"
        }
