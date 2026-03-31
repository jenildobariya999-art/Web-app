from fastapi import FastAPI, Request
import redis
import os
import hashlib

app = FastAPI()

# Redis connect
REDIS_URL = os.getenv("REDIS_URL")
r = redis.from_url(REDIS_URL, decode_responses=True)

@app.get("/")
def home():
    return {"status": "ok"}

@app.post("/verify")
async def verify(request: Request):
    data = await request.json()

    ip = request.client.host
    ua = request.headers.get("user-agent", "")
    canvas = data.get("canvas", "")
    audio = data.get("audio", "")

    raw = ip + ua + canvas + audio
    fp = hashlib.sha256(raw.encode()).hexdigest()

    if r.get(fp):
        return {"status": "blocked"}

    r.set(fp, "1")

    return {"status": "verified", "fp": fp}
