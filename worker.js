// Cloudflare Worker — proxy seeks/stellar (gratis, 2 menit deploy).
// Pakai: https://<worker>.workers.dev/?url=<ENCODED_UPSTREAM>
const REFERER = 'https://ps21.seeks.cloud/';

function rewrite(text, base, selfBase) {
  return text.split('\n').map((line) => {
    const s = line.trim();
    const m = s.match(/^(.*URI=")([^"]+)(".*)$/);
    if (m) return m[1] + selfBase + encodeURIComponent(new URL(m[2], base).href) + m[3];
    if (s && !s.startsWith('#')) return selfBase + encodeURIComponent(new URL(s, base).href);
    return line;
  }).join('\n');
}

addEventListener('fetch', (event) => {
  event.respondWith(handle(event.request));
});

async function handle(req) {
  const target = new URL(req.url).searchParams.get('url');
  if (!target || !target.startsWith('https://')) {
    return new Response('pakai ?url=https://...', { status: 400 });
  }
  const selfBase = new URL(req.url).origin + new URL(req.url).pathname + '?url=';
  const up = await fetch(target, {
    headers: { Referer: REFERER, 'User-Agent': 'Mozilla/5.0', Accept: '*/*' },
  });
  // Rewrite .m3u8 di sini (server); client idempoten (hanya absolutkan yg sudah proxy).
  let body = await up.arrayBuffer();
  let ct = up.headers.get('content-type') || 'application/octet-stream';
  if (/m3u8|\.txt/.test(target) || ct.includes('mpegurl')) {
    body = new TextEncoder().encode(
      rewrite(new TextDecoder().decode(body), target, selfBase)
    );
    ct = 'application/vnd.apple.mpegurl';
  }
  return new Response(body, {
    status: up.status,
    headers: { 'Content-Type': ct, 'Access-Control-Allow-Origin': '*' },
  });
}
