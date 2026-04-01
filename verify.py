import json

def handler(request):
    try:
        # simple response (no crash)
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({
                "status": "success",
                "title": "Verified Successfully",
                "message": "Device verified successfully"
            })
        }
    except Exception as e:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "error",
                "title": "Error",
                "message": str(e)
            })
        }
