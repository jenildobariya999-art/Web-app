import json
import os
import redis

# ===== REDIS =====
REDIS_URL = os.getenv("REDIS_URL")

if not REDIS_URL:
    raise Exception("REDIS_URL missing")

r = redis.from_url(REDIS_URL, decode_responses=True)

# ===== HANDLER =====
def handler(request):

    # Allow only POST
    if request.method != "POST":
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "API Working"})
        }

    try:
        body = json.loads(request.body)

        user_id = str(body.get("user_id"))
        fingerprint = body.get("fingerprint")
        ip = body.get("ip")
        ua = body.get("user_agent")

        if not user_id or not fingerprint:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "status": "error",
                    "title": "Invalid Request",
                    "message": "Missing user data"
                })
            }

        key = f"user:{user_id}"
        saved = r.get(key)

        # ===== FIRST TIME =====
        if not saved:
            r.set(key, json.dumps({
                "fingerprint": fingerprint,
                "ip": ip,
                "ua": ua
            }))

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "success",
                    "title": "Verified Successfully",
                    "message": "Device registered"
                })
            }

        saved_data = json.loads(saved)

        # ===== DEVICE CHECK =====
        if saved_data["fingerprint"] != fingerprint:
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "error",
                    "title": "Multiple Devices Detected",
                    "message": "Access denied"
                })
            }

        # ===== SUCCESS AGAIN =====
        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "info",
                "title": "Already Verified",
                "message": "Welcome back"
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
