"""Lab-only logging proxy in front of metadata.provider.plex.tv; can override /markers answers."""
import json, sys, time, urllib.parse, requests
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
UP="https://metadata.provider.plex.tv"; LOG=open(sys.argv[2],"a"); OVR=json.load(open(sys.argv[3])) if len(sys.argv)>3 else {}
class H(BaseHTTPRequestHandler):
    def _go(self):
        n=int(self.headers.get("Content-Length") or 0); body=self.rfile.read(n) if n else None
        u=urllib.parse.urlparse(self.path); q=urllib.parse.parse_qs(u.query)
        hdr={k:v for k,v in self.headers.items() if k.lower() not in ("host","accept-encoding","connection")}
        entry={"t":time.strftime("%H:%M:%S"),"method":self.command,"path":u.path,"query":{k:v for k,v in q.items() if "token" not in k.lower()},"body":(body or b"")[:500].decode("utf-8","replace")}
        h=q.get("hash",[None])[0]
        if u.path.endswith("/markers") and h in OVR:
            data=json.dumps(OVR[h]).encode(); status=200; ctype="application/json"; entry["override"]=True
        else:
            r=requests.request(self.command, UP+self.path, headers=hdr, data=body, timeout=30)
            data=r.content; status=r.status_code; ctype=r.headers.get("content-type","")
        entry.update(status=status, resp=data[:800].decode("utf-8","replace"))
        LOG.write(json.dumps(entry)+"\n"); LOG.flush()
        self.send_response(status); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    do_GET=do_POST=do_PUT=do_DELETE=_go
    def log_message(self,*a): pass
ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), H).serve_forever()
