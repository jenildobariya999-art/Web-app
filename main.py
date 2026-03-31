from fastapi import FastAPI, Request
import hashlib
import redis

app = FastAPI()

# 🔴 CHANGE HERE (Redis URL)
r = redis.Redis.from_url(
    "redis://default:gQAAAAAAAVxuAAIncDIwZWZkNjIwN2QyOTU0YTQ1YWZmMGE5NmE0OWJlMTBmYXAyODkxOTg@climbing-lizard-89198.upstash.io:6379",
    decode_responses=True
)

def hash_fp(fp):
    return hashlib.sha256(fp.encode()).hexdigest()

@app.get("/")
def home():
    return {"status": "ok"}

@app.post("/verify")
async def verify(req: Request):
    data = await req.json()

    fingerprint = data.get("fingerprint")
    user_id = str(data.get("user_id"))
    ref = str(data.get("ref"))

    ip = req.headers.get("x-forwarded-for", req.client.host)

    if not fingerprint or not user_id:
        return {"status": "fail"}

    fp_hash = hash_fp(fingerprint)

    # 🚫 already used device
    if r.get(f"device:{fp_hash}"):
        return {"status": "blocked"}

    # ✅ save device
    r.set(f"device:{fp_hash}", user_id)

    # ✅ mark verified
    r.set(f"user:{user_id}", "verified")

    # 💰 REFER SYSTEM (1 device = 1 refer only)
    if ref and ref != user_id:
        if not r.get(f"ref_used:{fp_hash}"):
            r.set(f"ref_used:{fp_hash}", "yes")

            # increase ref user balance
            bal = int(r.get(f"bal:{ref}") or 0)
            r.set(f"bal:{ref}", bal + 10)

    return {"status": "ok"}
