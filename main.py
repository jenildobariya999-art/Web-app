from http.server import BaseHTTPRequestHandler
import json
import redis
import hashlib

# 🔴 Redis (YOUR REAL URL)
r = redis.Redis.from_url("redis://default:gQAAAAAAAVxuAAIncDIwZWZkNjIwN2QyOTU0YTQ1YWZmMGE5NmE0OWJlMTBmYXAyODkxOTg@climbing-lizard-89198.upstash.io:6379")

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Verification</title>
<style>
body {
    margin:0;
    background: linear-gradient(135deg,#0f2027,#203a43,#2c5364);
    font-family:sans-serif;
    color:white;
    display:flex;
    justify-content:center;
    align-items:center;
    height:100vh;
}
.box {
    background: rgba(255,255,255,0.1);
    backdrop-filter: blur(15px);
    padding:30px;
    border-radius:15px;
    text-align:center;
}
</style>
</head>
<body>

<div class="box">
    <h2>🔐 Device Verification</h2>
    <p id="status">Checking...</p>
</div>

<script>
const urlParams = new URLSearchParams(window.location.search);
const uid = urlParams.get("uid");

fetch("/api/verify?uid=" + uid)
.then(res => res.json())
.then(data => {
    if(data.status === "success"){
        document.getElementById("status").innerText = "✅ Verified";

        setTimeout(()=>{
            window.location.href = "https://t.me/Agenthuu_bot?start=verify_done";
        },1500);

    } else {
        document.getElementById("status").innerText = data.message;
    }
})
.catch(()=>{
    document.getElementById("status").innerText = "❌ Network Error";
});
</script>

</body>
</html>
"""

class handler(BaseHTTPRequestHandler):
    def do_GET(self):

        # ✅ API ROUTE
        if self.path.startswith("/api/verify"):

            try:
                query = self.path.split("?")[-1]
                params = dict(x.split("=") for x in query.split("&"))
                uid = params.get("uid", "")
            except:
                uid = ""

            # 🌍 Real IP
            ip = self.headers.get("x-forwarded-for", self.client_address[0])

            # 📱 Device
            ua = self.headers.get("User-Agent", "")

            # 🔐 Fingerprint
            raw = ip + ua
            fingerprint = hashlib.sha256(raw.encode()).hexdigest()

            key = f"device:{fingerprint}"

            # ❌ Already used
            if r.exists(key):

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()

                self.wfile.write(json.dumps({
                    "status": "error",
                    "message": "❌ Device already used"
                }).encode())
                return

            # ✅ Save
            r.set(key, uid)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            self.wfile.write(json.dumps({
                "status": "success",
                "message": "✅ Verified"
            }).encode())
            return

        # ✅ UI PAGE (FIX FOR "OK" ISSUE)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode())
