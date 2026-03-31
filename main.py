from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
import redis
import os
import hashlib

app = FastAPI()

# 🔴 CHANGE HERE (Upstash Redis URL)
REDIS_URL = "redis://default:gQAAAAAAAVxuAAIncDIwZWZkNjIwN2QyOTU0YTQ1YWZmMGE5NmE0OWJlMTBmYXAyODkxOTg@climbing-lizard-89198.upstash.io:6379"

r = redis.from_url(REDIS_URL, decode_responses=True)


# Home page (UI open karega)
@app.get("/")
async def home():
    return FileResponse("index.html")


# Verification API
@app.post("/verify")
async def verify(request: Request):
    data = await request.json()

    fingerprint = data.get("fingerprint")
    ip = request.client.host

    if not fingerprint:
        return JSONResponse({"status": "error", "msg": "No fingerprint"})

    # Unique device ID
    device_id = hashlib.sha256((fingerprint + ip).encode()).hexdigest()

    # Check already used
    if r.get(device_id):
        return JSONResponse({
            "status": "blocked",
            "msg": "❌ Already verified / Fake user detected"
        })

    # Save device
    r.set(device_id, "verified")

    return JSONResponse({
        "status": "success",
        "msg": "✅ Verification Successful"
    })
