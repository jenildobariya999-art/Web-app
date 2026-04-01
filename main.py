from http.server import BaseHTTPRequestHandler
import json, redis, hashlib, urllib.parse

r = redis.Redis.from_url("redis://default:gQAAAAAAAVxuAAIncDIwZWZkNjIwN2QyOTU0YTQ1YWZmMGE5NmE0OWJlMTBmYXAyODkxOTg@climbing-lizard-89198.upstash.io:6379")

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Verification</title>
</head>
<body style="background:#111;color:white;text-align:center;padding-top:100px">

<h2>🔐 Device Verification</h2>
<p id="s">Checking...</p>

<script>

// SIMPLE fingerprint
let fp = navigator.userAgent + screen.width + screen.height;

// IMPORTANT: FULL URL use karo
fetch("https://webapp1-ashen.vercel.app/api/verify?fp=" + encodeURIComponent(fp))
.then(r => r.json())
.then(d => {
    document.getElementById("s").innerText = d.message;

    if(d.status === "success"){
        setTimeout(()=>{
            window.location.href = "https://t.me/TestingWithAll_bot?start=verify_done";
        },1000);
    }
})
.catch(()=>{
    document.getElementById("s").innerText = "❌ Network Error";
});

</script>

</body>
</html>
"""

class handler(BaseHTTPRequestHandler):

    def do_GET(self):

        # 🔐 VERIFY API
        if self.path.startswith("/api/verify"):

            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            fp = params.get("fp", [""])[0]

            ip = self.headers.get("x-forwarded-for", self.client_address[0])
            ua = self.headers.get("User-Agent","")

            raw = ip + ua + fp
            final_fp = hashlib.sha256(raw.encode()).hexdigest()

            key = "dev:" + final_fp

            if r.exists(key):
                self.send_json("❌ Device already used","error")
                return

            r.set(key,"1")

            self.send_json("✅ Verified Successfully","success")
            return

        # 🌐 UI
        self.send_response(200)
        self.send_header("Content-Type","text/html")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def send_json(self,msg,status):
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status":status,
            "message":msg
        }).encode())
