# app.py — tiny HTTP server just to pass Render "port scan"
from http.server import BaseHTTPRequestHandler, HTTPServer
import os

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    # listen on 0.0.0.0:PORT (Render expects this)
    HTTPServer(("", port), H).serve_forever()
