import json
import os
import redis

REDIS_URL = os.getenv("REDIS_URL")

# safety check
if not REDIS_URL:
    raise Exception("REDIS_URL not set")

r = redis.from_url(REDIS_URL, decode_responses=True)

def handler(request):

    # GET request (check API)
    if request.method == "GET":
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "API Running ✅"})
        }

    try:
        body = json.loads(request.body)

        user_id = str(body.get("user_id"))
        fingerprint = body.get("fingerprint")
        ip = body.get("ip")
        ua = body.get("user_agent")

        # basic validation
        if not fingerprint:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "status": "error",
                    "title": "Invalid",
                    "message": "Fingerprint missing"
                })
            }

        # unique key (NO simple user id only)
        key = f"verify:{fingerprint}"

        saved = r.get(key)

        # ===== FIRST TIME =====
        if not saved:
            r.set(key, json.dumps({
                "ip": ip,
                "ua": ua
            }))

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "success",
                    "title": "Verification Success",
                    "message": "Device registered"
                })
            }

        saved_data = json.loads(saved)

        # ===== IP CHANGE CHECK =====
        if saved_data["ip"] != ip:
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "error",
                    "title": "Suspicious Activity",
                    "message": "IP mismatch detected"
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
