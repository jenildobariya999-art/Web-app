import json, redis, hashlib

r = redis.Redis.from_url("redis://default:gQAAAAAAAVxuAAIncDIwZWZkNjIwN2QyOTU0YTQ1YWZmMGE5NmE0OWJlMTBmYXAyODkxOTg@climbing-lizard-89198.upstash.io:6379")

def handler(request):

    # 🔐 API
    if request.path == "/api/verify":

        fp = request.query.get("fp", "")

        ip = request.headers.get("x-forwarded-for", "")
        ua = request.headers.get("user-agent", "")

        raw = ip + ua + fp
        final_fp = hashlib.sha256(raw.encode()).hexdigest()

        key = "dev:" + final_fp

        if r.exists(key):
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "error",
                    "message": "❌ Device already used"
                })
            }

        r.set(key, "1")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "success",
                "message": "✅ Verified Successfully"
            })
        }

    # 🌐 UI
    html = """
    <!DOCTYPE html>
    <html>
    <body style="background:#111;color:white;text-align:center;padding-top:100px">

    <h2>🔐 Device Verification</h2>
    <p id="s">Checking...</p>

    <script>
    let fp = navigator.userAgent + screen.width + screen.height;

    fetch("/api/verify?fp=" + encodeURIComponent(fp))
    .then(r => r.json())
    .then(d => {
        document.getElementById("s").innerText = d.message;

        if(d.status === "success"){
            setTimeout(()=>{
                window.location.href = "https://t.me/TestingWithAll?start=verify_done";
            },1500);
        }
    })
    .catch(()=>{
        document.getElementById("s").innerText = "❌ Network Error";
    });
    </script>

    </body>
    </html>
    """

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": html
    }
