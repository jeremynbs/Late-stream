"""Synology DSM File Station API client.

Only what we need: log in, browse folders, get file info, and build a
download URL that ffmpeg/ffprobe can read directly over HTTP(S) with Range
requests (so MOV files whose index is at the end, and resume-from-position,
both work without copying 100 GB to local disk).

Docs: "Synology File Station Official API" (DSM 6/7).
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

AUTH_ERRORS = {
    400: "wrong account or password",
    401: "account disabled",
    402: "permission denied",
    403: "2-step verification code required (use an account without 2FA for the app)",
    404: "2-step verification failed",
    406: "2-step verification is enforced for this account",
    407: "IP blocked by DSM auto-block",
    408: "password expired",
    409: "password must be changed",
    410: "password must be changed",
}
COMMON_ERRORS = {
    100: "unknown error", 101: "bad request", 102: "API does not exist",
    103: "method does not exist", 104: "API version not supported",
    105: "permission denied", 106: "session timed out", 107: "session interrupted by duplicate login",
    119: "session ID not found",
}
FS_ERRORS = {
    400: "invalid parameter", 401: "unknown file operation error", 402: "system too busy",
    407: "operation not permitted", 408: "no such file or directory",
    414: "file already exists", 415: "disk quota exceeded", 416: "no space left",
}
SESSION_ERRORS = {106, 107, 119}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mxf", ".mkv", ".ts", ".m2ts", ".mts", ".avi", ".webm"}


class NASError(RuntimeError):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


@dataclass
class NASEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: int = 0

    def as_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "is_dir": self.is_dir,
                "size": self.size, "mtime": self.mtime}


class SynologyClient:
    def __init__(self, base_url: str, account: str, password: str, *,
                 verify_tls: bool = True, otp_code: str | None = None, timeout: float = 20):
        if not base_url:
            raise NASError("NAS URL is not configured")
        self.base = base_url.rstrip("/")
        if not self.base.startswith(("http://", "https://")):
            self.base = "https://" + self.base
        self.account = account
        self.password = password
        self.otp_code = otp_code
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._ctx = ssl.create_default_context()
        if not verify_tls:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
        self._sid: str | None = None
        self._apis: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------------- low level ----------------
    def _get(self, cgi: str, params: dict) -> dict:
        url = f"{self.base}/webapi/{cgi}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout, context=self._ctx) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.URLError as e:
            raise NASError(f"cannot reach NAS at {self.base}: {e.reason}") from e
        except json.JSONDecodeError as e:
            raise NASError(f"unexpected (non-JSON) reply from {self.base}") from e

    def _api(self, name: str) -> tuple[str, int]:
        if not self._apis:
            try:
                resp = self._get("query.cgi", {
                    "api": "SYNO.API.Info", "version": 1, "method": "query",
                    "query": "SYNO.API.Auth,SYNO.FileStation.List,SYNO.FileStation.Download"})
                if resp.get("success"):
                    self._apis = resp.get("data", {})
            except NASError:
                raise
        info = self._apis.get(name, {})
        return info.get("path", "entry.cgi"), int(info.get("maxVersion", 2))

    def _call(self, api: str, method: str, version: int, errors: dict, **params) -> dict:
        path, maxv = self._api(api)
        q = {"api": api, "version": min(version, maxv), "method": method, **params}
        if api != "SYNO.API.Auth":
            q["_sid"] = self.sid
        resp = self._get(path, q)
        if not resp.get("success"):
            code = int(resp.get("error", {}).get("code", 100))
            msg = errors.get(code) or COMMON_ERRORS.get(code) or f"error code {code}"
            raise NASError(f"{api}.{method}: {msg}", code)
        return resp.get("data", {})

    def _with_relogin(self, fn):
        try:
            return fn()
        except NASError as e:
            if e.code in SESSION_ERRORS:
                log.info("NAS session expired; logging in again")
                self._sid = None
                return fn()
            raise

    # ---------------- auth ----------------
    @property
    def sid(self) -> str:
        with self._lock:
            if not self._sid:
                self._sid = self.login()
            return self._sid

    def login(self) -> str:
        params = {"account": self.account, "passwd": self.password,
                  "session": "FileStation", "format": "sid"}
        if self.otp_code:
            params["otp_code"] = self.otp_code
        data = self._call("SYNO.API.Auth", "login", 6, AUTH_ERRORS, **params)
        sid = data.get("sid")
        if not sid:
            raise NASError("login succeeded but no session id returned")
        return sid

    def logout(self) -> None:
        if self._sid:
            try:
                self._call("SYNO.API.Auth", "logout", 6, AUTH_ERRORS, session="FileStation")
            except NASError:
                pass
            self._sid = None

    # ---------------- browsing ----------------
    def list_shares(self) -> list[NASEntry]:
        data = self._with_relogin(lambda: self._call(
            "SYNO.FileStation.List", "list_share", 2, FS_ERRORS,
            additional='["time"]', sort_by="name"))
        return [NASEntry(s["name"], s["path"], True) for s in data.get("shares", [])]

    def list_folder(self, folder: str, videos_only: bool = True) -> list[NASEntry]:
        if folder in ("", "/"):
            return self.list_shares()
        data = self._with_relogin(lambda: self._call(
            "SYNO.FileStation.List", "list", 2, FS_ERRORS,
            folder_path=folder, additional='["size","time"]',
            sort_by="name", limit=2000))
        out = []
        for f in data.get("files", []):
            add = f.get("additional", {})
            e = NASEntry(f["name"], f["path"], bool(f.get("isdir")),
                         int(add.get("size", 0) or 0),
                         int(add.get("time", {}).get("mtime", 0) or 0))
            if e.is_dir or not videos_only or _is_video(e.name):
                out.append(e)
        out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return out

    def file_info(self, path: str) -> NASEntry:
        data = self._with_relogin(lambda: self._call(
            "SYNO.FileStation.List", "getinfo", 2, FS_ERRORS,
            path=path, additional='["size","time"]'))
        files = data.get("files", [])
        if not files or files[0].get("code"):
            raise NASError(f"file not found on NAS: {path}", 408)
        f = files[0]
        if f.get("isdir"):
            raise NASError(f"{path} is a folder, not a video file")
        add = f.get("additional", {})
        return NASEntry(f["name"], f["path"], False, int(add.get("size", 0) or 0),
                        int(add.get("time", {}).get("mtime", 0) or 0))

    # ---------------- download ----------------
    def download_url(self, path: str) -> str:
        """HTTP(S) URL for the file, valid while the session lives. Contains
        the session id - never log it unredacted."""
        api_path, maxv = self._api("SYNO.FileStation.Download")
        q = {"api": "SYNO.FileStation.Download", "version": min(2, maxv),
             "method": "download", "path": path, "mode": "download", "_sid": self.sid}
        return f"{self.base}/webapi/{api_path}?{urllib.parse.urlencode(q)}"

    def check_range_support(self, path: str) -> bool:
        """True if the NAS answers a byte-range request with 206 (needed for
        MOV files with the index at the end, and for resuming)."""
        req = urllib.request.Request(self.download_url(path), headers={"Range": "bytes=0-1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                ctype = r.headers.get("Content-Type", "")
                if "json" in ctype:  # API error instead of the file
                    body = json.loads(r.read().decode() or "{}")
                    code = body.get("error", {}).get("code")
                    raise NASError(f"download failed: {FS_ERRORS.get(code, code)}", code)
                return r.status == 206
        except urllib.error.URLError as e:
            raise NASError(f"download request failed: {e}") from e


def _is_video(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(ext) for ext in VIDEO_EXTS)


def redact_url(url: str) -> str:
    """Hide session ids and stream keys in log output."""
    if "_sid=" in url:
        head, _, tail = url.partition("_sid=")
        rest = tail.split("&", 1)
        url = head + "_sid=<sid>" + ("&" + rest[1] if len(rest) > 1 else "")
    return url
