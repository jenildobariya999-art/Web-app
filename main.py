from http.server import BaseHTTPRequestHandler
import json
import redis
import hashlib

# 🔴 Redis (PASTE YOUR URL HERE)
r = redis.Redis.from_url("redis://default:gQAAAAAAAVxuAAIncDIwZWZkNjIwN2QyOTU0YTQ1YWZmMGE5NmE0OWJlMTBmYXAyODkxOTg@climbing-lizard-89198.upstash.io:6379")

class handler(BaseHTTPRequestHandler):
    def do_GET(self):

        if self.path.startswith("/api/verify"):

            try:
                query = self.path.split("?")[-1]
                params = dict(x.split("=") for x in query.split("&"))
                uid = params.get("uid", "")
            except:
                uid = ""

            # 🌍 Real IP
            ip = self.headers.get("x-forwarded-for", self.client_address[0])

            # 📱 Device (User-Agent)
            ua = self.headers.get("User-Agent", "")

            # 🔐 Fingerprint
            raw = ip + ua
            fingerprint = hashlib.sha256(raw.encode()).hexdigest()

            key = f"device:{fingerprint}"

            # ❌ Already used device
            if r.exists(key):

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()

                self.wfile.write(json.dumps({
                    "status": "error",
                    "message": "❌ Device already used"
                }).encode())
                return

            # ✅ Save device
            r.set(key, uid)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            self.wfile.write(json.dumps({
                "status": "success",
                "message": "✅ Verification successful",
                "uid": uid
            }).encode())

            return

        # Default route
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
