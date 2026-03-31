import json
import os
import redis

# ===== REDIS CONNECT =====
REDIS_URL = os.getenv("REDIS_URL")

if not REDIS_URL:
    raise Exception("REDIS_URL not set")

r = redis.from_url(REDIS_URL, decode_responses=True)

# ===== MAIN HANDLER =====
def handler(request):

    if request.method != "POST":
        return {
            "statusCode": 405,
            "body": json.dumps({"error": "Method not allowed"})
        }

    try:
        body = json.loads(request.body)

        user_id = str(body.get("user_id"))
        fingerprint = body.get("fingerprint")
        ip = body.get("ip")

        if not user_id or not fingerprint:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "status": "error",
                    "title": "Invalid Request",
                    "message": "Missing data"
                })
            }

        key = f"user:{user_id}"
        saved_data = r.get(key)

        # ===== FIRST TIME =====
        if not saved_data:
            r.set(key, json.dumps({
                "fingerprint": fingerprint,
                "ip": ip
            }))

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "success",
                    "title": "Verified Successfully",
                    "message": "Device linked successfully"
                })
            }

        saved = json.loads(saved_data)

        # ===== DEVICE MISMATCH =====
        if saved["fingerprint"] != fingerprint:
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "error",
                    "title": "Multiple Devices Detected",
                    "message": "Access denied (device mismatch)"
                })
            }

        # ===== SAME USER =====
        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "info",
                "title": "Already Verified",
                "message": "You are already verified"
            })
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "status": "error",
                "message": str(e)
            })
        }
