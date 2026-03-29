from fastapi import FastAPI, Request
import httpx
import hashlib

app = FastAPI()

BOT_TOKEN = "8464660617:AAE5sUjb_Y-buaAri4UYIiwoIg1eyk4xQuY"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# In production, use database
verified_devices = set()  # store fingerprint hashes

@app.post("/verify")
async def verify(request: Request):
    data = await request.json()
    fingerprint = data.get("fingerprint")
    if not fingerprint:
        return {"status":"error","message":"Fingerprint missing"}

    # Compute fingerprint hash
    fp_hash = hashlib.sha256(fingerprint.encode()).hexdigest()

    # Check if already verified
    if fp_hash in verified_devices:
        return {"status":"error","message":"Device already verified"}

    verified_devices.add(fp_hash)

    # Trigger /verified on bot
    try:
        # Replace <chat_id> with actual chat_id logic if needed
        # For device-only verification without user_id, you can store chat_id mapping on first click
        url = f"{TELEGRAM_API}/sendMessage?chat_id=<CHAT_ID>&text=/verified"
        httpx.get(url)
    except Exception as e:
        return {"status":"error","message":str(e)}

    return {"status":"success","message":"✅ Device Verified Successfully!"}
