from fastapi import FastAPI, Request
import redis
import os
import json

app = FastAPI()

REDIS_URL = os.getenv("REDIS_URL")
r = redis.from_url(REDIS_URL, decode_responses=True)

@app.get("/")
def home():
    return {"message": "Working ✅"}

@app.post("/")
async def verify(request: Request):
    body = await request.json()

    user_id = str(body.get("user_id"))
    fingerprint = body.get("fingerprint")
    ip = body.get("ip")

    key = f"user:{user_id}"
    saved = r.get(key)

    if not saved:
        r.set(key, json.dumps({
            "fingerprint": fingerprint,
            "ip": ip
        }))
        return {
            "status": "success",
            "title": "Verified",
            "message": "Device saved"
        }

    saved_data = json.loads(saved)

    if saved_data["fingerprint"] != fingerprint:
        return {
            "status": "error",
            "title": "Multiple Device",
            "message": "Blocked"
        }

    return {
        "status": "info",
        "title": "Already Verified",
        "message": "Welcome back"
    }
