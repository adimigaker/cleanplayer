import base64
import collections
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urljoin, quote

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    HAS_CRYPTO = True
except Exception:
    HAS_CRYPTO = False


def abyss_decrypt(media_str, seed_str):
    """AES-CTR decrypt ala player abyss (lite.bundle.js SoTrym).
    key = MD5(seed).hexdigest().encode() (32B = AES-256), iv = 16B pertama."""
    hhex = hashlib.md5(seed_str.encode()).hexdigest()
    key = hhex.encode()
    iv = hhex.encode()[:16]
    ct = media_str.encode('latin1')
    pt = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor().update(ct)
    return pt


def fetch_abyss(slug):
    if not HAS_CRYPTO:
        raise RuntimeError('cryptography belum terinstal (pip install cryptography)')
    page_url = 'https://player.abyssplayer.com/' + slug
    req = urllib.request.Request(page_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://abyss.to/',
        'Accept': 'text/html,*/*',
    })
    html = urllib.request.urlopen(req, timeout=25).read().decode('utf-8', errors='ignore')
    m = re.search(r'const datas\s*=\s*"([^"]+)"', html)
    if not m:
        raise RuntimeError('datas tidak ketemu di HTML abyss')
    txt = base64.b64decode(m.group(1)).decode('latin1')
    info = json.loads(txt)
    media = info.get('media', '')
    seed = '%s:%s:%s' % (info.get('user_id'), info.get('slug'), info.get('md5_id'))
    plain = json.loads(abyss_decrypt(media, seed).decode('utf-8'))
    out = {
        'slug': info.get('slug'),
        'md5_id': info.get('md5_id'),
        'user_id': info.get('user_id'),
        'title': info.get('slug'),
        'subtitles': (info.get('config') or {}).get('subtitles', []),
    }
    mp4 = (plain.get('mp4') or {})
    sources = []
    for s in (mp4.get('sources') or []):
        full = s.get('url', '').rstrip('/') + '/' + s.get('path', '').lstrip('/')
        sources.append({
            'label': s.get('label'),
            'res_id': s.get('res_id'),
            'size': s.get('size'),
            'url': full,
            'sub': s.get('sub'),
        })
    sources.sort(key=lambda x: (x.get('size') or 0), reverse=True)
    out['sources'] = sources
    out['domains'] = mp4.get('domains', [])
    fds = []
    for f in (mp4.get('fristDatas') or []):
        u = f.get('url', '').replace('hstps://', 'https://')
        fds.append({'res_id': f.get('res_id'), 'size': f.get('size'), 'url': u})
    out['fristDatas'] = fds
    if plain.get('hls'):
        out['hls'] = plain.get('hls')
    return out


ABYSS_FRAG = 2097152
_ABYSS_META_CACHE = {}
_ABYSS_FRAG_CACHE = collections.OrderedDict()


def _abyss_key_num(v):
    return hashlib.md5(bytes(int(c) for c in str(v))).hexdigest()


def abyss_seg_token(md5_id, res_id, size, idx):
    h = _abyss_key_num(size)
    path = '/mp4/%s/%s/%s/%s/%s' % (md5_id, res_id, size, ABYSS_FRAG, idx)
    ct = Cipher(algorithms.AES(h.encode()), modes.CTR(h.encode()[:16])).encryptor().update(path.encode())
    b1 = base64.b64encode(ct.decode('latin1').encode('latin1')).decode().replace('=', '')
    return base64.b64encode(b1.encode()).decode().replace('=', '')


def abyss_meta(slug):
    """Metadata abyss + segbase per source, host mati dibuang. Cache 10 menit."""
    now = time.time()
    hit = _ABYSS_META_CACHE.get(slug)
    if hit and now - hit[0] < 600:
        return hit[1]
    info = fetch_abyss(slug)
    dom0 = (info.get('domains') or [''])[0]
    suffix = dom0.split('.', 1)[1] if '.' in dom0 else ''
    live = []
    for s in info.get('sources', []):
        segbase = 'https://%s.%s' % (s.get('sub') or '', suffix)
        try:
            socket.gethostbyname(segbase.split('://')[1])
        except Exception:
            continue
        s['segbase'] = segbase
        live.append(s)
    info['sources'] = live
    _ABYSS_META_CACHE[slug] = (now, info)
    return info


def abyss_frag(slug, q, idx):
    key = (slug, q, idx)
    if key in _ABYSS_FRAG_CACHE:
        _ABYSS_FRAG_CACHE.move_to_end(key)
        return _ABYSS_FRAG_CACHE[key]
    meta = abyss_meta(slug)
    src = meta['sources'][q]
    tok = abyss_seg_token(meta['md5_id'], src['res_id'], src['size'], idx)
    url = '%s/sora/%s/%s' % (src['segbase'], src['size'], tok)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://abysscdn.com/',
        'Accept': '*/*',
    })
    data = urllib.request.urlopen(req, timeout=60).read()
    _ABYSS_FRAG_CACHE[key] = data
    while len(_ABYSS_FRAG_CACHE) > 24:
        _ABYSS_FRAG_CACHE.popitem(last=False)
    return data


def prefetch_abyss(slug, qi, mulai, jumlah=3):
    # Baca-duluan fragmen berikutnya di background agar playback tak jeda
    # antar-respons (khusus link lambat / tunnel). Gagal = diam.
    def jalan():
        for i in range(mulai, mulai + jumlah):
            try:
                if (slug, qi, i) in _ABYSS_FRAG_CACHE:
                    continue
                abyss_frag(slug, qi, i)
            except Exception:
                break
    threading.Thread(target=jalan, daemon=True).start()


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

GD_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'


def gdrive_dl_url(file_id):
    """URL download langsung file drive (lewati halaman virus-scan bila ada).
    File kecil -> file langsung; file besar -> confirm=t&uuid=... ."""
    warn = 'https://drive.google.com/uc?export=download&id=' + file_id
    req = urllib.request.Request(warn, headers={'User-Agent': GD_UA, 'Accept': '*/*'})
    resp = urllib.request.urlopen(req, timeout=30)
    if 'html' not in (resp.headers.get_content_type() or ''):
        return resp.geturl() or warn
    html = resp.read().decode('utf-8', errors='ignore')
    m = re.search(r'name="uuid" value="([^"]+)"', html)
    dl = ('https://drive.usercontent.google.com/download?id=' + file_id +
          '&export=download&confirm=t')
    if m:
        dl += '&uuid=' + m.group(1)
    return dl


_GSUB_CACHE = {}


def gdrive_sub_vtt(file_id):
    """Ambil subtitle pertama file drive sebagai WebVTT (cache ≤20 file)."""
    hit = _GSUB_CACHE.get(file_id)
    if hit:
        return hit
    dl = gdrive_dl_url(file_id)
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
           '-headers', 'User-Agent: ' + GD_UA + '\r\n',
           '-i', dl, '-map', '0:s:0', '-f', 'webvtt', 'pipe:1']
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=600)
    vtt = p.stdout or b''
    if not vtt.lstrip().startswith(b'WEBVTT'):
        raise RuntimeError('file ini tak ada subtitle')
    if len(_GSUB_CACHE) > 20:
        _GSUB_CACHE.pop(next(iter(_GSUB_CACHE)))
    _GSUB_CACHE[file_id] = vtt
    return vtt


_PMETA_CACHE = {}


def remote_duration(url):
    """Durasi detik file video jarak-jauh via ffprobe (baca ranged, cepat).
    Cache ≤20 URL."""
    hit = _PMETA_CACHE.get(url)
    if hit:
        return hit
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=nw=1:nk=1',
           '-headers', 'User-Agent: ' + GD_UA + '\r\n', url]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=90)
    dur = float((p.stdout or b'').decode().strip())
    if not (dur > 0):
        raise RuntimeError('durasi tak terbaca')
    if len(_PMETA_CACHE) > 20:
        _PMETA_CACHE.pop(next(iter(_PMETA_CACHE)))
    _PMETA_CACHE[url] = dur
    return dur


VFILE_DIR = '/tmp/cpvfile'
VFILE_BUDGET = 1500 * 1024 * 1024  # cache maks 1.5GB, LRU
_VFILE_JOBS = {}


def _vfile_du():
    try:
        return sum(os.path.getsize(os.path.join(VFILE_DIR, f))
                   for f in os.listdir(VFILE_DIR) if f.endswith('.mp4'))
    except Exception:
        return 0


def vfile_evict(kecuali=None):
    try:
        fs = sorted((os.path.getmtime(os.path.join(VFILE_DIR, f)), f)
                    for f in os.listdir(VFILE_DIR) if f.endswith('.mp4'))
    except Exception:
        return
    for _, f in fs:
        if _vfile_du() <= VFILE_BUDGET:
            break
        if f == (kecuali or '') + '.mp4':
            continue
        try:
            os.remove(os.path.join(VFILE_DIR, f))
        except Exception:
            pass


def vfile_start(pid, src):
    # Remux moov-depan ke disk di background. Idempoten per pid.
    job = _VFILE_JOBS.get(pid)
    if job and (job.get('done') or (job.get('proc') and job['proc'].poll() is None)):
        return job
    os.makedirs(VFILE_DIR, exist_ok=True)
    vfile_evict(pid)
    path = os.path.join(VFILE_DIR, pid + '.mp4')
    total = 0
    try:  # total via Range 0-0 (lebih andal dari HEAD)
        req = urllib.request.Request(src, headers={'User-Agent': GD_UA, 'Range': 'bytes=0-0'})
        resp = urllib.request.urlopen(req, timeout=20)
        m = re.search(r'/(\d+)', resp.headers.get('Content-Range') or '')
        if m:
            total = int(m.group(1))
    except Exception:
        pass
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
           '-headers', 'User-Agent: ' + GD_UA + '\r\n',
           '-i', src, '-map', '0:v:0', '-map', '0:a:0?',
           '-c', 'copy', '-movflags', 'faststart', '-y', path]
    proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
    job = {'proc': proc, 'path': path, 'total': total, 'done': False, 'error': ''}
    _VFILE_JOBS[pid] = job
    return job


def vfile_stat(pid):
    job = _VFILE_JOBS.get(pid)
    if not job:
        return {'ready': False, 'progress': 0, 'size': 0}
    if not job['done']:
        rc = job['proc'].poll()
        if rc is not None:
            if rc == 0 and os.path.exists(job['path']):
                job['done'] = True
            else:
                job['error'] = 'ffmpeg exit ' + str(rc)
    size = 0
    try:
        size = os.path.getsize(job['path'])
    except Exception:
        pass
    if job['done']:
        return {'ready': True, 'size': size}
    if job['error']:
        return {'ready': False, 'error': job['error'], 'size': size}
    prog = (size / job['total']) if job['total'] > 0 else 0
    return {'ready': False, 'progress': round(min(0.99, prog), 3), 'size': size}


def serve_range(handler, path):
    # File statis dengan Range penuh (seekbar natural dua arah)
    size = os.path.getsize(path)
    rng = handler.headers.get('Range')
    a, b, status = 0, size - 1, 200
    if rng:
        m = re.match(r'bytes=(\d*)-(\d*)', rng)
        if m:
            if m.group(1):
                a = int(m.group(1))
            if m.group(2):
                b = int(m.group(2))
            b = min(b, size - 1)
            if a >= size:
                handler.send_response(416)
                handler.send_header('Content-Range', 'bytes */%d' % size)
                handler.end_headers()
                return
            status = 206
    handler.send_response(status)
    handler.send_header('Content-Type', 'video/mp4')
    handler.send_header('Accept-Ranges', 'bytes')
    if status == 206:
        handler.send_header('Content-Range', 'bytes %d-%d/%d' % (a, b, size))
    handler.send_header('Content-Length', str(b - a + 1))
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Access-Control-Expose-Headers', 'Content-Range, Content-Length, Accept-Ranges')
    handler.send_header('Cache-Control', 'public, max-age=3600')
    handler.end_headers()
    with open(path, 'rb') as f:
        f.seek(a)
        sisa = b - a + 1
        while sisa > 0:
            chunk = f.read(min(65536, sisa))
            if not chunk:
                break
            handler.wfile.write(chunk)
            sisa -= len(chunk)


def serve_ffmpeg_remux(handler, dl):
    """Remux video jarak-jauh jadi MP4 fragment (moov di depan) via ffmpeg
    -c copy. Mengembalikan True bila header terkirim (streaming jalan)."""
    proc = None
    terkirim = False
    try:
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
               '-headers', 'User-Agent: ' + GD_UA + '\r\n',
               '-i', dl,
               '-map', '0:v:0', '-map', '0:a:0?',
               '-c', 'copy',
               '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
               '-f', 'mp4', 'pipe:1']
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        head = proc.stdout.read(65536)
        if not head:
            raise RuntimeError('ffmpeg gagal membuka sumber (tidak public / format aneh?)')
        handler.send_response(200)
        handler.send_header('Content-Type', 'video/mp4')
        handler.send_header('Access-Control-Allow-Origin', '*')
        handler.send_header('Cache-Control', 'no-store, max-age=0')
        handler.end_headers()
        terkirim = True
        handler.wfile.write(head)
        shutil.copyfileobj(proc.stdout, handler.wfile, length=65536)
    except Exception as e:
        if not terkirim:
            try:
                handler.send_response(502)
                handler.send_header('Content-Type', 'text/plain')
                handler.send_header('Access-Control-Allow-Origin', '*')
                handler.end_headers()
                handler.wfile.write(('remux gagal: ' + str(e)).encode())
            except Exception:
                pass
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    return terkirim


ROOT = os.path.dirname(os.path.abspath(__file__))
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
            self.send_header('Cache-Control', 'no-store, max-age=0')
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
        if parsed.path == '/abyss':
            qs = parse_qs(parsed.query)
            slug = qs.get('slug', [None])[0]
            raw = qs.get('url', [None])[0]
            if not slug and raw:
                m = re.search(r'abyssplayer\.com/([A-Za-z0-9_-]{7,17})', raw)
                if not m:
                    ms = re.findall(r'[A-Za-z0-9_-]{7,17}', raw)
                    slug = ms[-1] if ms else None
                else:
                    slug = m.group(1)
            if not slug:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(b'{"error":"isi ?slug= atau ?url=player.abyssplayer.com/..."}')
                return
            try:
                data = fetch_abyss(slug)
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store, max-age=0')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({'error': 'abyss gagal: ' + str(e)}).encode()
                self.send_response(502)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        if parsed.path == '/abyssplay':
            qs = parse_qs(parsed.query)
            slug = qs.get('slug', [None])[0]
            raw = qs.get('url', [None])[0]
            if not slug and raw:
                m = re.search(r'abyssplayer\.com/([A-Za-z0-9_-]{7,17})', raw)
                if not m:
                    ms = re.findall(r'[A-Za-z0-9_-]{7,17}', raw)
                    slug = ms[-1] if ms else None
                else:
                    slug = m.group(1)
            try:
                qi = int(qs.get('q', ['0'])[0])
            except Exception:
                qi = 0
            try:
                meta = abyss_meta(slug)
                src = meta['sources'][qi]
                total = src['size']
                rng = self.headers.get('Range')
                a, b = 0, total - 1
                status = 200
                if rng:
                    m = re.match(r'bytes=(\d*)-(\d*)', rng)
                    if m:
                        if m.group(1):
                            a = int(m.group(1))
                        if m.group(2):
                            b = int(m.group(2))
                        b = min(b, total - 1)
                        status = 206
                n0, n1 = a // ABYSS_FRAG, b // ABYSS_FRAG
                # Tanggapi cepat: satu respons dibatasi 8 fragmen (16 MB) agar
                # byte pertama langsung jalan; browser minta sisanya sendiri.
                if (n1 - n0 + 1) > 8 and rng:
                    n1 = n0 + 7
                    b = min((n1 + 1) * ABYSS_FRAG - 1, total - 1)
                buf = bytearray()
                for i in range(n0, n1 + 1):
                    buf += abyss_frag(slug, qi, i)
                prefetch_abyss(slug, qi, n1 + 1)
                body = bytes(buf[a - n0 * ABYSS_FRAG:b - n0 * ABYSS_FRAG + 1])
                self.send_response(status)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Accept-Ranges', 'bytes')
                if status == 206:
                    self.send_header('Content-Range', 'bytes %d-%d/%d' % (a, b, total))
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Expose-Headers', 'Content-Range, Content-Length, Accept-Ranges')
                self.send_header('Cache-Control', 'no-store, max-age=0')
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = ('abyssplay gagal: ' + str(e)).encode()
                self.send_response(502)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        if parsed.path == '/gdrive':
            # Google Drive -> remux on-the-fly MKV/dll jadi MP4 fragment
            # (browser tak bisa memutar container MKV) via ffmpeg -c copy.
            qs = parse_qs(parsed.query)
            fid = qs.get('id', [None])[0]
            raw = qs.get('url', [None])[0]
            if not fid and raw:
                m = (re.search(r'/file/d/([A-Za-z0-9_-]+)', raw) or
                     re.search(r'[?&]id=([A-Za-z0-9_-]+)', raw))
                fid = m.group(1) if m else None
            if not fid or not re.fullmatch(r'[A-Za-z0-9_-]+', fid):
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(b'{"error":"isi ?id=FILEID atau ?url=drive.google.com/..."}')
                return
            try:
                dl = gdrive_dl_url(fid)
            except Exception as e:
                body = ('gdrive gagal: ' + str(e)).encode()
                self.send_response(502)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            serve_ffmpeg_remux(self, dl)
            return
        if parsed.path == '/remux':
            # Remux URL video langsung (allowlist host) jadi MP4 fragment
            # untuk file yg moov-nya di belakang (browser tak mulai2).
            qs = parse_qs(parsed.query)
            target = qs.get('url', [None])[0]
            host = (urlparse(target).hostname or '') if target else ''
            if not target or host not in ('pixeldrain.com',):
                body = b'{"error":"isi ?url= pixeldrain yg valid"}'
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            serve_ffmpeg_remux(self, target)
            return
        if parsed.path == '/pmeta':
            # Durasi detik file video (allowlist host) via ffprobe cepat
            qs = parse_qs(parsed.query)
            target = qs.get('url', [None])[0]
            host = (urlparse(target).hostname or '') if target else ''
            try:
                if not target or host not in ('pixeldrain.com',):
                    raise RuntimeError('isi ?url= pixeldrain yg valid')
                body = json.dumps({'duration': remote_duration(target)}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
            except Exception as e:
                body = json.dumps({'error': 'pmeta gagal: ' + str(e)}).encode()
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == '/pdfile':
            # Versi geser: remux moov-depan sekali ke disk, serve Range penuh.
            # ?url= (allowlist) + &prepare=1 / &stat=1, tanpa itu = stream file.
            qs = parse_qs(parsed.query)
            target = qs.get('url', [None])[0]
            host = (urlparse(target).hostname or '') if target else ''
            if not target or host not in ('pixeldrain.com', 'drive.usercontent.google.com'):
                body = b'{"error":"isi ?url= pixeldrain/drive yg valid"}'
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            pid = hashlib.sha1(target.encode()).hexdigest()[:16]
            if 'prepare' in qs or 'stat' in qs:
                if 'prepare' in qs:
                    try:
                        vfile_start(pid, target)
                    except Exception as e:
                        body = json.dumps({'ready': False, 'error': str(e)}).encode()
                        self.send_response(500)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                body = json.dumps(vfile_stat(pid)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store, max-age=0')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            stt = vfile_stat(pid)
            fpath = os.path.join(VFILE_DIR, pid + '.mp4')
            if not stt.get('ready') or not os.path.exists(fpath):
                body = json.dumps(stt).encode()
                self.send_response(409)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                serve_range(self, fpath)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if parsed.path == '/gsub':
            # Subtitle pertama file drive sebagai WebVTT siap <track>
            qs = parse_qs(parsed.query)
            fid = qs.get('id', [None])[0]
            if not fid or not re.fullmatch(r'[A-Za-z0-9_-]+', fid):
                self.send_response(400)
                self.end_headers()
                return
            try:
                vtt = gdrive_sub_vtt(fid)
                self.send_response(200)
                self.send_header('Content-Type', 'text/vtt; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.send_header('Content-Length', str(len(vtt)))
                self.end_headers()
                self.wfile.write(vtt)
            except Exception as e:
                body = ('gsub gagal: ' + str(e)).encode()
                self.send_response(404)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        if parsed.path == '/proxy':
            target = parse_qs(parsed.query).get('url', [None])[0]
            if not target or not target.startswith('https://'):
                self.send_response(400)
                self.end_headers()
                return
            try:
                if 'sssrr.org' in target:
                    referer = 'https://player.abyssplayer.com/'
                elif 'iamcdn.net' in target:
                    referer = 'https://player.abyssplayer.com/'
                else:
                    referer = UPSTREAM_REFERER
                hdrs = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                    'Referer': referer,
                    'Accept': '*/*',
                }
                # Teruskan Range (wajib untuk file besar/download parsial)
                rng = self.headers.get('Range')
                if rng:
                    hdrs['Range'] = rng
                req = urllib.request.Request(target, headers=hdrs)
                try:
                    resp = urllib.request.urlopen(req, timeout=60)
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
                    self.send_header('Access-Control-Expose-Headers', 'Content-Range, Content-Length, Accept-Ranges')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    # File besar (mp4/segmen/vtt): STREAMING langsung, tanpa buffer
                    uptype = resp.headers.get('Content-Type', '')
                    if 'sssrr.org' in target and 'octet-stream' in uptype:
                        uptype = 'video/mp4'
                    self.send_response(status)
                    if uptype:
                        self.send_header('Content-Type', uptype)
                    for h in ('Content-Length', 'Content-Range', 'Accept-Ranges'):
                        hv = resp.headers.get(h)
                        if hv:
                            self.send_header(h, hv)
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Access-Control-Expose-Headers', 'Content-Range, Content-Length, Accept-Ranges')
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


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8902), H).serve_forever()
