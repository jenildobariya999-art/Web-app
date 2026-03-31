from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import redis
import os
import hashlib

app = FastAPI()

REDIS_URL = os.getenv("REDIS_URL")
r = redis.from_url(REDIS_URL, decode_responses=True)

# 🔥 FRONTEND PAGE
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
    <body>
    <h2>Verifying Device...</h2>

    <script>
    function getCanvasFingerprint() {
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        ctx.textBaseline = "top";
        ctx.font = "14px Arial";
        ctx.fillText("fingerprint", 2, 2);
        return canvas.toDataURL();
    }

    async function getAudioFingerprint() {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const analyser = ctx.createAnalyser();

        osc.type = "triangle";
        osc.connect(analyser);
        analyser.connect(ctx.destination);
        osc.start(0);

        const data = new Uint8Array(analyser.frequencyBinCount);
        analyser.getByteFrequencyData(data);

        osc.stop();

        return Array.from(data).slice(0, 20).join(",");
    }

    async function verify() {
        const canvas = getCanvasFingerprint();
        const audio = await getAudioFingerprint();

        const res = await fetch("/verify", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ canvas, audio })
        });

        const data = await res.json();
        document.body.innerHTML = "<h2>" + JSON.stringify(data) + "</h2>";
    }

    verify();
    </script>

    </body>
    </html>
    """

# 🔥 VERIFY API
@app.post("/verify")
async def verify(request: Request):
    data = await request.json()

    ip = request.client.host
    ua = request.headers.get("user-agent", "")
    canvas = data.get("canvas", "")
    audio = data.get("audio", "")

    raw = ip + ua + canvas + audio
    fp = hashlib.sha256(raw.encode()).hexdigest()

    if r.get(fp):
        return {"status": "blocked"}

    r.set(fp, "1")

    return {"status": "verified"}
