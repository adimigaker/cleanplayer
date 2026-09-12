#!/bin/bash
cd /tmp/cleanplayer
git add .
git commit -m "auto: $(date -Iseconds)" || true
git push || true
echo "Auto-pushed $(date)"
