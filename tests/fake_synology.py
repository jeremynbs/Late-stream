"""A tiny fake of the Synology File Station API for tests (DSM 7 style)."""

import json
import os
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeSynology:
    def __init__(self, root: Path, user="streamer", password="pw", range_support=True):
        self.root = root            # NAS path "/video/x.mov" -> root/"video"/"x.mov"
        self.user, self.password = user, password
        self.range_support = range_support
        self.sids: set[str] = set()
        self.logins = 0
        self.downloads = 0
        self.fail_downloads = 0     # make the next N downloads drop mid-file
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    def expire_sessions(self):
        self.sids.clear()

    def local(self, nas_path: str) -> Path:
        return self.root / nas_path.lstrip("/")

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _err(self, code):
                self._json({"success": False, "error": {"code": code}})

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                api, method = q.get("api"), q.get("method")
                if u.path == "/webapi/query.cgi":
                    return self._json({"success": True, "data": {
                        "SYNO.API.Auth": {"path": "entry.cgi", "maxVersion": 7, "minVersion": 1},
                        "SYNO.FileStation.List": {"path": "entry.cgi", "maxVersion": 2, "minVersion": 1},
                        "SYNO.FileStation.Download": {"path": "entry.cgi", "maxVersion": 2, "minVersion": 1},
                    }})
                if u.path != "/webapi/entry.cgi":
                    return self._json({}, 404)
                if api == "SYNO.API.Auth":
                    if method == "login":
                        if q.get("account") != fake.user or q.get("passwd") != fake.password:
                            return self._err(400)
                        sid = secrets.token_hex(8)
                        fake.sids.add(sid)
                        fake.logins += 1
                        return self._json({"success": True, "data": {"sid": sid}})
                    if method == "logout":
                        fake.sids.discard(q.get("_sid"))
                        return self._json({"success": True})
                if q.get("_sid") not in fake.sids:
                    return self._err(119)
                if api == "SYNO.FileStation.List":
                    if method == "list_share":
                        shares = [{"name": p.name, "path": "/" + p.name, "isdir": True}
                                  for p in sorted(fake.root.iterdir()) if p.is_dir()]
                        return self._json({"success": True, "data": {"shares": shares, "total": len(shares)}})
                    if method == "list":
                        d = fake.local(q["folder_path"])
                        if not d.is_dir():
                            return self._err(408)
                        files = [self._entry(q["folder_path"].rstrip("/") + "/" + p.name, p)
                                 for p in sorted(d.iterdir())]
                        return self._json({"success": True, "data": {"files": files, "total": len(files)}})
                    if method == "getinfo":
                        p = fake.local(q["path"])
                        if not p.exists():
                            return self._json({"success": True, "data": {"files": [
                                {"code": 408, "path": q["path"]}]}})
                        return self._json({"success": True, "data": {"files": [self._entry(q["path"], p)]}})
                if api == "SYNO.FileStation.Download":
                    return self._download(fake.local(q["path"]))
                return self._err(103)

            def _entry(self, nas_path, p: Path):
                st = p.stat()
                return {"name": p.name, "path": nas_path, "isdir": p.is_dir(),
                        "additional": {"size": st.st_size, "time": {"mtime": int(st.st_mtime)}}}

            def _download(self, p: Path):
                if not p.is_file():
                    return self._err(408)
                fake.downloads += 1
                size = p.stat().st_size
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                if rng and fake.range_support:
                    a, _, b = rng.replace("bytes=", "").partition("-")
                    start = int(a) if a else 0
                    end = int(b) if b else size - 1
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes" if fake.range_support else "none")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                drop = False
                if fake.fail_downloads and (end - start) > 1_000_000:
                    fake.fail_downloads -= 1
                    drop = True
                try:
                    with open(p, "rb") as f:
                        f.seek(start)
                        left = end - start + 1
                        sent = 0
                        while left > 0:
                            chunk = f.read(min(256 * 1024, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                            sent += len(chunk)
                            if drop and sent > (end - start) // 3:
                                self.connection.shutdown(2)
                                return
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        return H
