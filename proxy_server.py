import json
import re
import shutil
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urljoin, quote


def rewrite_playlist(text, base):
    out = []
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^(.*URI=")([^"]+)(".*)$', s)
        if m:
            out.append(m.group(1) + '/proxy?url=' + quote(urljoin(base, m.group(2)), safe='') + m.group(3))
        elif s and not s.startswith('#'):
            out.append('/proxy?url=' + quote(urljoin(base, s), safe=''))
        else:
            out.append(line)
    return '\n'.join(out) + '\n'

ROOT = '/tmp/cleanplayer'
UPSTREAM_REFERER = 'https://ps21.seeks.cloud/'

class H(BaseHTTPRequestHandler):
    def _serve_file(self, path):
        if path == '/':
            path = '/index.html'
        fp = ROOT + path.split('?')[0]
        try:
            with open(fp, 'rb') as f:
                body = f.read()
            ct = 'text/html'
            if fp.endswith('.js'):
                ct = 'text/javascript'
            elif fp.endswith('.vtt'):
                ct = 'text/vtt'
            elif fp.endswith('.m3u8'):
                ct = 'application/vnd.apple.mpegurl'
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _route(self):
        parsed = urlparse(self.path)
        if parsed.path == '/proxy':
            target = parse_qs(parsed.query).get('url', [None])[0]
            if not target or not target.startswith('https://'):
                self.send_response(400)
                self.end_headers()
                return
            try:
                hdrs = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                    'Referer': UPSTREAM_REFERER,
                    'Accept': '*/*',
                }
                # Teruskan Range (wajib untuk file besar/download parsial)
                rng = self.headers.get('Range')
                if rng:
                    hdrs['Range'] = rng
                req = urllib.request.Request(target, headers=hdrs)
                try:
                    resp = urllib.request.urlopen(req, timeout=25)
                    status = resp.status
                except urllib.error.HTTPError as he:
                    resp = he
                    status = he.code
                ctype = resp.headers.get_content_type()
                # Rewrite .m3u8 di SINI (varian/segmen absolut -> /proxy?url= relatif).
                # Client (index.html) idempoten: baris yg sudah proxy hanya diabsolutkan.
                if 'm3u8' in target or target.endswith('.txt') or 'mpegurl' in ctype:
                    body = resp.read()
                    try:
                        body = rewrite_playlist(body.decode('utf-8', errors='ignore'), target).encode()
                        ctype = 'application/vnd.apple.mpegurl'
                    except Exception:
                        pass
                    self.send_response(status)
                    self.send_header('Content-Type', ctype)
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    # File besar (mp4/segmen/vtt): STREAMING langsung, tanpa buffer
                    self.send_response(status)
                    for h in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                        hv = resp.headers.get(h)
                        if hv:
                            self.send_header(h, hv)
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    shutil.copyfileobj(resp, self.wfile, length=65536)
            except Exception as e:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(('proxy error: ' + str(e)).encode())
            return
        self._serve_file(parsed.path)

    def log_message(self, *a):
        pass

ThreadingHTTPServer(('0.0.0.0', 8902), H).serve_forever()
