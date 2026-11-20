"""SQLite storage for app settings and the stream schedule."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SETTINGS: dict = {
    # general
    "timezone": "UTC",
    "preflight_minutes": 60,       # check the NAS file this long before start
    "max_late_minutes": 15,        # if the app was down at start time, still start up to this late
    "default_rtmp_url": "rtmps://a.rtmps.youtube.com/live2",
    # NAS
    "nas_url": "",                 # e.g. https://192.168.1.10:5001
    "nas_user": "",
    "nas_password": "",
    "nas_verify_tls": False,       # Synology ships with a self-signed cert
    "nas_root": "/",               # folder the file browser opens in
    # encoder (ProRes Proxy 1080p30 -> H.264 1080p30)
    "video_encoder": "libx264",
    "preset": "veryfast",
    "height": 1080,
    "video_bitrate_kbps": 8000,
    "audio_bitrate_kbps": 160,
    "keyframe_seconds": 2,
    # email
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls",   # starttls | ssl | none
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "alert_to": "",                # comma separated
    "notify_start": True,
    "notify_finish": True,
    # YouTube (optional; only used to set the video to unlisted afterwards)
    "youtube_client_json": "",
    "youtube_token_json": "",
    "youtube_channel": "",
    # internal
    "admin_password_hash": "",
    "secret_key": "",
    "oauth_state": "",
}
SECRET_SETTINGS = {"nas_password", "smtp_password", "youtube_client_json",
                   "youtube_token_json", "admin_password_hash", "secret_key", "oauth_state"}

# Stream lifecycle
SCHEDULED = "scheduled"        # waiting
READY = "ready"                # pre-flight check passed
CHECK_FAILED = "check_failed"  # pre-flight failed (will retry until start)
LIVE = "live"
DONE = "done"
FAILED = "failed"
MISSED = "missed"
STOPPED = "stopped"
INTERRUPTED = "interrupted"    # app was restarted mid-stream
WAITING = {SCHEDULED, READY, CHECK_FAILED}
FINAL = {DONE, FAILED, MISSED, STOPPED, INTERRUPTED}


@dataclass
class Stream:
    id: int | None = None
    name: str = ""
    nas_path: str = ""
    start_utc: str = ""            # ISO 8601, UTC
    rtmp_url: str = ""
    stream_key: str = ""
    youtube_video_id: str = ""     # optional; auto-detected if YouTube is connected
    unlist_after: bool = True
    status: str = SCHEDULED
    message: str = ""
    duration: float = 0.0
    file_size: int = 0
    position: float = 0.0
    speed: str = ""
    restarts: int = 0
    heartbeat: str = ""
    started_at: str = ""
    ended_at: str = ""
    created_at: str = ""

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.start_utc)

    @property
    def end_estimate(self) -> datetime | None:
        if not self.duration:
            return None
        from datetime import timedelta
        return self.start + timedelta(seconds=self.duration)

    @property
    def output_url(self) -> str:
        return f"{self.rtmp_url.rstrip('/')}/{self.stream_key}"

    def public_dict(self) -> dict:
        d = asdict(self)
        d["stream_key"] = mask(self.stream_key)
        return d


def mask(secret: str) -> str:
    if not secret:
        return ""
    return "•" * 8 + secret[-4:] if len(secret) > 6 else "•" * 8


STREAM_FIELDS = [f.name for f in fields(Stream)]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS streams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, nas_path TEXT, start_utc TEXT, rtmp_url TEXT, stream_key TEXT,
                youtube_video_id TEXT, unlist_after INTEGER, status TEXT, message TEXT,
                duration REAL, file_size INTEGER, position REAL, speed TEXT,
                restarts INTEGER, heartbeat TEXT, started_at TEXT, ended_at TEXT,
                created_at TEXT);
            CREATE INDEX IF NOT EXISTS streams_start ON streams(start_utc);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, stream_id INTEGER, ts TEXT,
                level TEXT, message TEXT);
        """)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---------------- settings ----------------
    def settings(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        out = dict(DEFAULT_SETTINGS)
        for r in rows:
            out[r["key"]] = json.loads(r["value"])
        return out

    def get_setting(self, key: str):
        return self.settings().get(key, DEFAULT_SETTINGS.get(key))

    def set_settings(self, **values) -> None:
        with self._tx() as c:
            for k, v in values.items():
                if k not in DEFAULT_SETTINGS:
                    raise KeyError(f"unknown setting {k}")
                c.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (k, json.dumps(v)))

    # ---------------- streams ----------------
    @staticmethod
    def _row(r: sqlite3.Row) -> Stream:
        d = {k: r[k] for k in r.keys() if k in STREAM_FIELDS}
        d["unlist_after"] = bool(d.get("unlist_after"))
        for k in STREAM_FIELDS:
            if d.get(k) is None:
                d[k] = Stream.__dataclass_fields__[k].default
        return Stream(**d)

    def list_streams(self) -> list[Stream]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM streams ORDER BY start_utc").fetchall()
        return [self._row(r) for r in rows]

    def get_stream(self, sid: int) -> Stream | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM streams WHERE id=?", (sid,)).fetchone()
        return self._row(r) if r else None

    def save_stream(self, s: Stream) -> Stream:
        d = asdict(s)
        d.pop("id")
        d["unlist_after"] = int(s.unlist_after)
        with self._tx() as c:
            if s.id is None:
                d["created_at"] = d["created_at"] or utcnow_iso()
                cols = ",".join(d)
                cur = c.execute(f"INSERT INTO streams({cols}) VALUES({','.join('?' * len(d))})",
                                list(d.values()))
                s.id = cur.lastrowid
                s.created_at = d["created_at"]
            else:
                sets = ",".join(f"{k}=?" for k in d)
                c.execute(f"UPDATE streams SET {sets} WHERE id=?", [*d.values(), s.id])
        return s

    def update_stream(self, sid: int, **values) -> None:
        bad = set(values) - set(STREAM_FIELDS)
        if bad:
            raise KeyError(f"unknown stream fields {bad}")
        if "unlist_after" in values:
            values["unlist_after"] = int(values["unlist_after"])
        with self._tx() as c:
            sets = ",".join(f"{k}=?" for k in values)
            c.execute(f"UPDATE streams SET {sets} WHERE id=?", [*values.values(), sid])

    def delete_stream(self, sid: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM streams WHERE id=?", (sid,))
            c.execute("DELETE FROM events WHERE stream_id=?", (sid,))

    # ---------------- event log ----------------
    def add_event(self, stream_id: int | None, level: str, message: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO events(stream_id, ts, level, message) VALUES(?,?,?,?)",
                      (stream_id, utcnow_iso(), level, message))

    def events(self, stream_id: int | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            if stream_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE stream_id=? ORDER BY id DESC LIMIT ?",
                    (stream_id, limit)).fetchall()
        return [dict(r) for r in rows]


def find_overlap(streams: list[Stream], candidate: Stream,
                 margin_s: float = 120) -> Stream | None:
    """Return a scheduled stream whose time range overlaps ``candidate``."""
    from datetime import timedelta
    if not candidate.duration:
        return None
    c_start = candidate.start
    c_end = c_start + timedelta(seconds=candidate.duration + margin_s)
    for s in streams:
        if s.id == candidate.id or s.status in FINAL or not s.duration:
            continue
        s_end = s.start + timedelta(seconds=s.duration + margin_s)
        if c_start < s_end and s.start < c_end:
            return s
    return None
