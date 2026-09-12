#!/bin/bash
# Supervisi proxy cleanplayer: hidup terus, restart otomatis bila mati.
while true; do
  if ! curl -s -o /dev/null --max-time 8 http://127.0.0.1:8902/ ; then
    pkill -f proxy_server.py 2>/dev/null
    sleep 1
    nohup python3 /tmp/cleanplayer/proxy_server.py >/tmp/proxy.log 2>&1 &
  fi
  sleep 20
done
