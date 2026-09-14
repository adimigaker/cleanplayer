#!/bin/bash
# Supervisi cleanplayer: proxy + tunnel trycloudflare + proxy-url.txt auto-update.
# Dijalankan via systemd (cleanplayer-supervisor.service). Loop selamanya.
REPO=/home/ubuntu/cleanplayer
CLOUDFLARED=/home/ubuntu/bin/cloudflared
LOG=/home/ubuntu/cleanplayer_supervisor.log
TUNLOG=/home/ubuntu/cleanplayer_tunnel.log

log() { echo "$(date -Iseconds) $*" >> "$LOG"; }
log "supervisor mulai"

while true; do
  # 0. Domain statis (tanpa tunnel) diutamakan bila sehat
  STATIC=$(cat "$REPO/static-domain.txt" 2>/dev/null | tr -d ' \n')
  if [ -n "$STATIC" ]; then
    SCODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 25 "$STATIC/abyss?slug=FwgRBdn_d" 2>/dev/null || echo 000)
    if [ "$SCODE" = "200" ]; then
      WANT="${STATIC}/proxy?url="
      if [ "$(cat "$REPO/proxy-url.txt" 2>/dev/null)" != "$WANT" ]; then
        log "domain statis sehat, pakai $STATIC"
        echo "$WANT" > "$REPO/proxy-url.txt"
        cd "$REPO" && git add proxy-url.txt && git commit -m "pakai domain statis $STATIC" >>"$LOG" 2>&1 && git push origin main >>"$LOG" 2>&1 && log "proxy-url.txt push OK" || log "push gagal"
      fi
      sleep 60
      continue
    fi
    log "domain statis mati (kode $SCODE), fallback tunnel"
  fi
  # 1. Pastikan proxy hidup
  if ! curl -s -o /dev/null --max-time 8 http://127.0.0.1:8902/ ; then
    log "proxy mati, mulai ulang"
    pkill -f proxy_server.py 2>/dev/null
    sleep 1
    nohup python3 "$REPO/proxy_server.py" >>"$LOG" 2>&1 &
    sleep 3
  fi
  # 2. Tes URL publik dari proxy-url.txt
  BASE=$(cat "$REPO/proxy-url.txt" 2>/dev/null | sed 's|/proxy?url=$||')
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 25 "$BASE/abyss?slug=FwgRBdn_d" 2>/dev/null || echo 000)
  if [ "$CODE" = "200" ]; then
    sleep 60
    continue
  fi
  # 3. Tunnel mati -> buat baru, update proxy-url.txt + push
  log "tunnel mati (kode $CODE), buat baru"
  pkill -f "cloudflared tunnel" 2>/dev/null
  sleep 1
  rm -f "$TUNLOG"
  nohup "$CLOUDFLARED" tunnel --url http://127.0.0.1:8902 >"$TUNLOG" 2>&1 &
  NEW=""
  for i in $(seq 1 18); do
    sleep 5
    NEW=$(grep -o -E 'https://[A-Za-z0-9-]+\.trycloudflare\.com' "$TUNLOG" 2>/dev/null | head -n 1)
    [ -n "$NEW" ] && break
  done
  if [ -z "$NEW" ]; then log "gagal dapat URL tunnel"; sleep 60; continue; fi
  log "tunnel baru: $NEW"
  echo "${NEW}/proxy?url=" > "$REPO/proxy-url.txt"
  cd "$REPO" && git add proxy-url.txt && git commit -m "tunnel baru $(date -Iseconds) (auto-supervisi)" >>"$LOG" 2>&1 && git push origin main >>"$LOG" 2>&1 && log "proxy-url.txt push OK" || log "push gagal"
  sleep 60
done
