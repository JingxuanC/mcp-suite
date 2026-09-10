"""causal-memory 租户 shim：mcphub 静态头+passthrough 会把 Authorization 拼成
"Bearer <静态>, Bearer <调用者>"，本服务取最后一个 bearer（调用者 key）转发给
causal-memory(50061) 做租户路由；无逗号时原样透传（hub 发现/健康检查用静态 admin）。
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request, urllib.error

UPSTREAM = "http://127.0.0.1:50061"
HOP = {"host", "content-length", "connection", "transfer-encoding"}

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        auth = self.headers.get("Authorization", "")
        if "," in auth:
            last = auth.split(",")[-1].strip()
            if last.lower().startswith("bearer "):
                auth = last
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        if auth:
            headers["Authorization"] = auth
        else:
            headers.pop("Authorization", None)
        req = urllib.request.Request(UPSTREAM + self.path, data=body, method=self.command, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data, code, hdrs = r.read(), r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            data, code, hdrs = e.read(), e.code, dict(e.headers)
        except Exception as e:
            self.send_response(502); self.end_headers(); self.wfile.write(str(e).encode()); return
        self.send_response(code)
        for k, v in hdrs.items():
            if k.lower() in ("content-type", "mcp-session-id", "cache-control"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    do_GET = do_POST = do_DELETE = _proxy
    def log_message(self, *a): pass

ThreadingHTTPServer(("127.0.0.1", 51061), H).serve_forever()
