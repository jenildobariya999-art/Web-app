import json
import hashlib

def handler(request):
    try:
        if request.method == "OPTIONS":
            return {
                "statusCode": 200,
                "headers": {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type"
                },
                "body": ""
            }

        params = request.get("query", {})
        uid = params.get("uid", "unknown")

        ip = request.headers.get("x-forwarded-for", "0.0.0.0")
        ua = request.headers.get("user-agent", "unknown")

        fingerprint = hashlib.md5((ip + ua).encode()).hexdigest()

        return {
            "statusCode": 200,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({
                "status": "success",
                "message": "Device Verified ✅",
                "uid": uid,
                "fingerprint": fingerprint
            })
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({
                "status": "error",
                "message": str(e)
            })
        }
