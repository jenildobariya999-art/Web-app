from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import redis
import os
import hashlib
import urllib.parse

app = FastAPI()

r = redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)

# 🔥 FRONTEND PAGE
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    query = dict(request.query_params)
    uid = query.get("uid", "")
    ref = query.get("ref", "")

    return f"""
    <html>
    <body>
    <h2>Verifying...</h2>

    <script>
    function getCanvas() {{
        const c = document.createElement("canvas");
        const ctx = c.getContext("2d");
        ctx.fillText("fp", 10, 10);
        return c.toDataURL();
    }}

    async function verify() {{
        const res = await fetch("/verify", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
                uid: "{uid}",
                ref: "{ref}",
                canvas: getCanvas()
            }})
        }});

        const data = await res.json();
        document.body.innerHTML = "<h2>" + JSON.stringify(data) + "</h2>";
    }}

    verify();
    </script>
    </body>
    </html>
    """

# 🔥 VERIFY API
@app.post("/verify")
async def verify(request: Request):
    data = await request.json()

    uid = data.get("uid")
    ref = data.get("ref")
    canvas = data.get("canvas", "")

    ip = request.client.host
    ua = request.headers.get("user-agent", "")

    raw = ip + ua + canvas
    fp = hashlib.sha256(raw.encode()).hexdigest()

    # ❌ Same device block
    if r.get(f"fp:{fp}"):
        return {"status": "blocked", "reason": "device used"}

    # ❌ Already verified user
    if r.get(f"user:{uid}"):
        return {"status": "already verified"}

    # ✅ Save fingerprint
    r.set(f"fp:{fp}", uid)

    # ✅ Save user
    r.set(f"user:{uid}", fp)

    # 🎯 Referral system
    if ref and ref != uid:
        if r.get(f"user:{ref}"):
            r.incr(f"ref_count:{ref}")

    return {"status": "verified"}
