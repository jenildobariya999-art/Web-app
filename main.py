from http.server import BaseHTTPRequestHandler
import json, redis, hashlib

r = redis.Redis.from_url("redis://default:gQAAAAAAAVxuAAIncDIwZWZkNjIwN2QyOTU0YTQ1YWZmMGE5NmE0OWJlMTBmYXAyODkxOTg@climbing-lizard-89198.upstash.io:6379")

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Secure Verify</title>
<style>
body{margin:0;background:#0f2027;color:#fff;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif}
.box{background:rgba(255,255,255,0.1);padding:25px;border-radius:15px;text-align:center}
</style>
</head>
<body>

<div class="box">
<h2>🔐 Ultra Verification</h2>
<p id="s">Scanning...</p>
</div>

<script>

// 🎨 Canvas
function canvasFP(){
 let c=document.createElement("canvas");
 let x=c.getContext("2d");
 x.fillText("secure",2,2);
 return c.toDataURL();
}

// 🧠 WebGL
function webglFP(){
 try{
   let c=document.createElement("canvas");
   let gl=c.getContext("webgl");
   let debug=gl.getExtension('WEBGL_debug_renderer_info');
   return gl.getParameter(debug.UNMASKED_RENDERER_WEBGL);
 }catch{return "no_webgl";}
}

// 🔊 Audio
async function audioFP(){
 try{
  let ctx=new AudioContext();
  let osc=ctx.createOscillator();
  let analyser=ctx.createAnalyser();
  osc.connect(analyser);
  analyser.connect(ctx.destination);
  osc.start(0);
  let data=new Float32Array(analyser.frequencyBinCount);
  analyser.getFloatFrequencyData(data);
  return data.slice(0,10).join(",");
 }catch{return "no_audio";}
}

(async ()=>{
 const fp = {
  canvas: canvasFP(),
  webgl: webglFP(),
  audio: await audioFP(),
  screen: screen.width+"x"+screen.height,
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  ua: navigator.userAgent
 };

 fetch("/api/verify",{
   method:"POST",
   headers:{"Content-Type":"application/json"},
   body:JSON.stringify(fp)
 })
 .then(r=>r.json())
 .then(d=>{
   document.getElementById("s").innerText = d.message;
 })
 .catch(()=>{
   document.getElementById("s").innerText = "❌ Network Error";
 });

})();
</script>

</body>
</html>
"""

class handler(BaseHTTPRequestHandler):

    def do_POST(self):

        if self.path == "/api/verify":

            length = int(self.headers.get('Content-Length',0))
            data = self.rfile.read(length)

            try:
                body = json.loads(data)
            except:
                body = {}

            ip = self.headers.get("x-forwarded-for", self.client_address[0])
            ua = self.headers.get("User-Agent","")

            # 🚫 VPN basic detect
            if "vpn" in ua.lower() or "proxy" in ua.lower():
                self.send( "🚫 VPN/Proxy detected", "error")
                return

            # 🔐 SUPER FINGERPRINT
            raw = ip + ua + json.dumps(body)
            fp = hashlib.sha256(raw.encode()).hexdigest()

            key = "dev:" + fp

            if r.exists(key):
                self.send("❌ Already used device","error")
                return

            r.set(key,"1")

            self.send("✅ Verified Successfully","success")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type","text/html")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def send(self,msg,status):
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status":status,
            "message":msg
        }).encode())
