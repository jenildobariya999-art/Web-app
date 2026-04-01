from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import hashlib

app = FastAPI()

# simple memory (vercel reset hota hai, but test ke liye ok)
users = {}

def generate_fingerprint(ip, ua):
    return hashlib.sha256(f"{ip}-{ua}".encode()).hexdigest()

@app.get("/api/verify")
async def verify(request: Request):
    try:
        ip = request.client.host
        ua = request.headers.get("user-agent")

        uid = request.query_params.get("uid")

        if not uid:
            return JSONResponse({"status": "error", "message": "No UID"})

        fingerprint = generate_fingerprint(ip, ua)

        if fingerprint in users:
            return JSONResponse({
                "status": "error",
                "message": "Already used device"
            })

        users[fingerprint] = uid

        return JSONResponse({
            "status": "success",
            "message": "Device verified successfully"
        })

    except Exception as e:
        return JSONResponse({
            "status": "error",
            "message": str(e)
        })
