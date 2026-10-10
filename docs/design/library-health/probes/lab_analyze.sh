#!/bin/bash
TOK=$(docker run --rm -v mlab_app_config_captions:/c:ro alpine cat /c/settings.json | python3 -c '
import json,sys
for s in json.load(sys.stdin)["media_servers"]:
    if s["type"]=="plex": print((s.get("auth") or {}).get("token") or s.get("token","")); break')
B=http://127.0.0.1:32402
flag() { curl -s -H "X-Plex-Token: $TOK" "$B/library/metadata/$1" | grep -oE '<Part [^>]*' | grep -oE 'indexes="[^"]*"' || echo "no indexes attr"; }
echo "before 1368: $(flag 1368)"; echo "before 1378: $(flag 1378)"
curl -s -o /dev/null -w "analyze PUT -> %{http_code}\n" -X PUT -H "X-Plex-Token: $TOK" "$B/library/metadata/1368/analyze"
for i in $(seq 1 20); do sleep 3; f=$(flag 1368); [ "$f" != "no indexes attr" ] && break; done
echo "after ~${i}x3s 1368: $f"; echo "untouched 1378: $(flag 1378)"
