import json
import os
import redis

REDIS_URL = os.getenv("REDIS_URL")

if not REDIS_URL:
    raise Exception("REDIS_URL not set")

r = redis.from_url(REDIS_URL, decode_responses=True)

def handler(request):

    if request.method == "GET":
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "OK"})
        }

    try:
        body = json.loads(request.body)

        fingerprint = body.get("fingerprint")
        ip = body.get("ip")

        if not fingerprint:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "status": "error",
                    "message": "No fingerprint"
                })
            }

        key = f"verify:{fingerprint}"
        saved = r.get(key)

        if not saved:
            r.set(key, json.dumps({"ip": ip}))
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "success",
                    "message": "Verified"
                })
            }

        old = json.loads(saved)

        if old["ip"] != ip:
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "error",
                    "message": "IP changed"
                })
            }

        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "info",
                "message": "Already verified"
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
