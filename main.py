from http.server import BaseHTTPRequestHandler
import json
import redis
import hashlib

# 🔴 Redis config
r = redis.Redis.from_url("redis://default:gQAAAAAAAVxuAAIncDIwZWZkNjIwN2QyOTU0YTQ1YWZmMGE5NmE0OWJlMTBmYXAyODkxOTg@climbing-lizard-89198.upstash.io:6379"
                         ")

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/verify"):

            query = self.path.split("?")[-1]
            params = dict(x.split("=") for x in query.split("&"))

            uid = params.get("uid", "")
            ip = self.client_address[0]
            ua = self.headers.get("User-Agent")

            # 🔐 fingerprint
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
                    "message": "Device already used"
                }).encode())
                return

            # ✅ Save device
            r.set(key, uid)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            self.wfile.write(json.dumps({
                "status": "success",
                "uid": uid
            }).encode())
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
