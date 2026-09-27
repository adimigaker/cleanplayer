// Cloudflare Worker — OriginProxy (abyss + seeks/generic) — pengganti utama proxy VM.
// Endpoint identik dengan proxy VM agar player cukup ganti base URL:
//   /abyss?slug=SLUG          → metadata abyss (decrypt ala player)
//   /abyssplay?slug=&q=N      → stream MP4 per-fragmen (Range aware, 206)
//   /proxy?url=ENCODED        → generic proxy (seeks m3u8/dl) + rewrite m3u8
// Deploy: wrangler deploy (lihat wrangler.toml) atau paste ke dashboard CF.

const FRAG = 2097152;           // 2MB — sama dengan proxy VM
const ABYSS_REFERER = 'https://abysscdn.com/';
const PAGE_REFERER = 'https://abyss.to/';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)';
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Range, Content-Type',
  'Access-Control-Expose-Headers': 'Content-Range, Content-Length, Accept-Ranges',
};

// ── MD5 via WebCrypto (bawaan Cloudflare Workers & Node modern) ──
async function md5Hex(x) {
  const data = typeof x === 'string' ? te.encode(x) : x;
  const buf = await crypto.subtle.digest('MD5', data);
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('');
}
const md5hex = md5Hex;

// ── helper bytes ──
const te = new TextEncoder();
const td = new TextDecoder('utf-8');

function ascii(s) { return te.encode(s); }

// string ↔ bytes interpretasi latin1 (charCode & 0xFF) — utk media ter-encrypt
function latin1bytes(s) {
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i) & 0xFF;
  return out;
}
function latin1str(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return s;
}
function b64nopad(bytes) {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/=+$/, '');
}

// b64 decode → byte mentah
function b64decodeBytes(s) {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function aesCtr(bytes, keyBytes, ivBytes) {
  const key = await crypto.subtle.importKey('raw', keyBytes, 'AES-CTR', false, ['encrypt', 'decrypt']);
  const buf = await crypto.subtle.encrypt(
    { name: 'AES-CTR', counter: ivBytes, length: 128 }, key, bytes);
  return new Uint8Array(buf);
}

// Decrypt media ala player abyss: key = MD5(seed) ASCII 32B, iv = 16B pertama
async function abyssDecrypt(mediaStr, seedStr) {
  const hhex = await md5hex(seedStr);
  const key = ascii(hhex);
  const iv = ascii(hhex.slice(0, 16));
  const ct = latin1bytes(mediaStr);
  const pt = await aesCtr(ct, key, iv);
  return latin1str(pt); // latin1 → string JSON
}

// Token fragmen: kunci = MD5 dari BYTE per digit angka (bukan MD5 string biasa)
// sesuai _abyss_key_num() di proxy VM: md5(bytes(int(c) for c in str(v)))
function segKey(size) {
  const b = new Uint8Array(String(size).length);
  for (let i = 0; i < b.length; i++) b[i] = String(size).charCodeAt(i) - 48;
  return md5hex(b);
}

// Token fragmen: AES-CTR(MD5(size), path) → b64 tanpa '=' → b64 lagi
async function segToken(md5id, resId, size, idx) {
  const h = await segKey(size);
  const path = `/mp4/${md5id}/${resId}/${size}/${FRAG}/${idx}`;
  const ct = await aesCtr(ascii(path), ascii(h), ascii(h.slice(0, 16)));
  const b1 = b64nopad(ct);
  return b64nopad(ascii(b1));
}

// ── metadata abyss (setara fetch_abyss VM) ──
const META_CACHE = new Map(); // slug → {exp, data}
async function fetchAbyss(slug) {
  const hit = META_CACHE.get(slug);
  if (hit && hit.exp > Date.now()) return hit.data;

  const page = await fetch(`https://player.abyssplayer.com/${slug}`, {
    headers: { 'User-Agent': UA, 'Referer': PAGE_REFERER, 'Accept': 'text/html,*/*' },
  });
  if (!page.ok) throw new Error('halaman abyss ' + page.status);
  const html = await page.text();
  const m = html.match(/const datas\s*=\s*"([^"]+)"/);
  if (!m) throw new Error('datas tidak ketemu di HTML abyss');
  const info = JSON.parse(latin1str(b64decodeBytes(m[1])));
  const seed = `${info.user_id}:${info.slug}:${info.md5_id}`;
  const plain = JSON.parse(await abyssDecrypt(info.media || '', seed));

  const mp4 = plain.mp4 || {};
  const sources = (mp4.sources || []).map((s) => ({
    label: s.label, res_id: s.res_id, size: s.size,
    url: (s.url || '').replace(/\/+$/, '') + '/' + (s.path || '').replace(/^\/+/, ''),
    sub: s.sub,
  })).sort((a, b) => (b.size || 0) - (a.size || 0));

  const data = {
    slug: info.slug, md5_id: info.md5_id, user_id: info.user_id, title: info.slug,
    subtitles: (info.config || {}).subtitles || [],
    sources, domains: mp4.domains || [],
    fristDatas: (mp4.fristDatas || []).map((f) => ({
      res_id: f.res_id, size: f.size, url: (f.url || '').replace('hstps://', 'https://'),
    })),
  };
  if (plain.hls) data.hls = plain.hls;
  META_CACHE.set(slug, { exp: Date.now() + 600000, data });
  if (META_CACHE.size > 60) META_CACHE.delete(META_CACHE.keys().next().value);
  return data;
}

// segbase per source (suffix dari domain pertama)
function segbaseOf(meta, src) {
  const dom0 = (meta.domains || [''])[0];
  const suffix = dom0.includes('.') ? dom0.split('.').slice(1).join('.') : '';
  return `https://${src.sub || ''}.${suffix}`;
}

// ── /abyssplay: stream MP4 per-fragmen (Range aware) ──
async function handleAbyssplay(url) {
  const slug = url.searchParams.get('slug');
  const qi = parseInt(url.searchParams.get('q') || '0', 10) || 0;
  if (!slug || !/^[A-Za-z0-9_-]{7,17}$/.test(slug)) return json({ error: 'slug tidak valid' }, 400);
  const meta = await fetchAbyss(slug);
  const src = meta.sources[qi];
  if (!src) return json({ error: 'kualitas tidak ada' }, 404);
  const total = src.size;
  const segbase = segbaseOf(meta, src);

  let a = 0, b = total - 1, status = 200;
  const rng = (url.searchParams.get('range') || '').match(/^bytes=(\d*)-(\d*)$/);
  if (rng && (rng[1] || rng[2])) {
    if (rng[1]) a = parseInt(rng[1], 10);
    if (rng[2]) b = Math.min(parseInt(rng[2], 10), total - 1);
    status = 206;
  }
  let n0 = Math.floor(a / FRAG), n1 = Math.floor(b / FRAG);
  if (n1 - n0 + 1 > 8) { n1 = n0 + 7; b = Math.min((n1 + 1) * FRAG - 1, total - 1); status = 206; }

  const parts = [];
  for (let i = n0; i <= n1; i++) {
    const tok = await segToken(meta.md5_id, src.res_id, src.size, i);
    const furl = `${segbase}/sora/${src.size}/${tok}`;
    const fr = await fetch(furl, {
      headers: { 'User-Agent': UA, 'Referer': ABYSS_REFERER, 'Accept': '*/*' },
    });
    if (!fr.ok) return new Response('fragmen gagal: ' + fr.status, { status: 502, headers: CORS });
    parts.push(new Uint8Array(await fr.arrayBuffer()));
  }
  let joined = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let off = 0;
  for (const p of parts) { joined.set(p, off); off += p.length; }
  const slice = joined.subarray(a - n0 * FRAG, b - n0 * FRAG + 1);

  const h = { ...CORS, 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes', 'Content-Length': String(slice.length) };
  if (status === 206) h['Content-Range'] = `bytes ${a}-${b}/${total}`;
  return new Response(slice, { status, headers: h });
}

// ── /proxy: generic proxy + rewrite m3u8 (seeks) ──
function rewritePlaylist(text, base, selfBase) {
  return text.split('\n').map((line) => {
    const s = line.trim();
    const m = s.match(/^(.*URI=")([^"]+)(".*)$/);
    if (m) return m[1] + selfBase + encodeURIComponent(new URL(m[2], base).href) + m[3];
    if (s && !s.startsWith('#')) return selfBase + encodeURIComponent(new URL(s, base).href);
    return line;
  }).join('\n');
}

async function handleProxy(url) {
  const target = url.searchParams.get('url');
  if (!target || !target.startsWith('https://')) return json({ error: 'pakai ?url=https://...' }, 400);
  const selfBase = url.origin + url.pathname + '?url=';
  const referer = /abyss|sssrr|abysscdn/.test(target) ? ABYSS_REFERER
    : /seeks/.test(target) ? 'https://ps21.seeks.cloud/'
    : new URL(target).origin + '/';

  const init = { headers: { 'User-Agent': UA, 'Referer': referer, 'Accept': '*/*' } };
  const reqHdr = new Headers(init.headers);
  const range = url.searchParams.get('r');
  if (range) reqHdr.set('Range', range);
  const up = await fetch(target, { headers: reqHdr, redirect: 'follow' });

  const ct = up.headers.get('content-type') || 'application/octet-stream';
  if (/m3u8|\.txt/.test(target) || ct.includes('mpegurl')) {
    const txt = await up.text();
    const body = rewritePlaylist(txt, target, selfBase);
    return new Response(body, {
      status: up.status,
      headers: { 'Content-Type': 'application/vnd.apple.mpegurl', ...CORS },
    });
  }
  const h = new Headers(CORS);
  for (const k of ['content-type', 'content-length', 'content-range', 'accept-ranges']) {
    const v = up.headers.get(k);
    if (v) h.set(k, v);
  }
  return new Response(up.body, { status: up.status, headers: h });
}

// ── /pb: health/stats ──
function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status, headers: { 'Content-Type': 'application/json', ...CORS },
  });
}

addEventListener('fetch', (event) => {
  event.respondWith(handle(event.request));
});

async function handle(req) {
  const url = new URL(req.url);
  const p = url.pathname;
  try {
    if (p === '/abyss') {
      let slug = url.searchParams.get('slug');
      const raw = url.searchParams.get('url');
      if (!slug && raw) {
        const m = raw.match(/abyssplayer\.com\/([A-Za-z0-9_-]{7,17})/);
        slug = m ? m[1] : (raw.match(/[A-Za-z0-9_-]{7,17}/g) || []).pop();
      }
      if (!slug || !/^[A-Za-z0-9_-]{7,17}$/.test(slug)) return json({ error: 'isi ?slug= atau ?url=...' }, 400);
      const data = await fetchAbyss(slug);
      return new Response(JSON.stringify(data), {
        headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...CORS },
      });
    }
    if (p === '/abyssplay') return await handleAbyssplay(url);
    if (p === '/proxy') return await handleProxy(url);
    if (p === '/' || p === '/health') return json({ ok: true, svc: 'originproxy', frag: FRAG });
    return json({ error: 'tidak dikenal — pakai /abyss, /abyssplay, /proxy, /health' }, 404);
  } catch (e) {
    return json({ error: String((e && e.message) || e) }, 502);
  }
}
