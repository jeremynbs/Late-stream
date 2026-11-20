import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from late_stream import db as D
from late_stream import notify
from late_stream.db import DB, Stream
from late_stream.service import Service

from .conftest import ffprobe_streams


class FakeYT:
    def __init__(self):
        self.calls = []
        self.status = "ready"

    def find_broadcast(self, key, statuses=("active", "upcoming")):
        self.calls.append(("find", key))
        return "VID123"

    def ensure_live(self, vid, timeout=0):
        self.calls.append(("ensure_live", vid))
        self.status = "live"
        return "live"

    def wait_complete(self, vid, timeout=0):
        self.calls.append(("wait_complete", vid))
        return "complete"

    def set_privacy(self, vid, privacy):
        self.calls.append(("privacy", vid, privacy))


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda s, subj, body: sent.append((subj, body)))
    return sent


def make_service(tmp_path, nas, yt=None, **kw):
    db = DB(tmp_path / "data" / "db.sqlite")
    db.set_settings(nas_url=nas.url, nas_user="streamer", nas_password="pw",
                    smtp_host="smtp.example", alert_to="ops@example.com",
                    youtube_token_json="x" if yt else "", timezone="Asia/Jakarta")
    svc = Service(db, tmp_path / "data", youtube_factory=(lambda s: yt) if yt else None,
                  tick_seconds=0.5, restart_delay=0.5, youtube_go_live_delay=1,
                  youtube_poll=0.1, **kw)
    return db, svc


def add_stream(db, out_dir, start_in=2.0, path="/video/recordings/session1.mov", **kw):
    s = Stream(name="Morning", nas_path=path,
               start_utc=(datetime.now(timezone.utc) + timedelta(seconds=start_in))
               .isoformat(timespec="seconds"),
               rtmp_url=str(out_dir), stream_key="out.flv", **kw)
    return db.save_stream(s)


def wait_status(db, sid, statuses, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = db.get_stream(sid)
        if s.status in statuses:
            return s
        time.sleep(0.25)
    raise AssertionError(f"status stayed {db.get_stream(sid).status}")


def test_scheduled_stream_end_to_end(tmp_path, nas, emails):
    yt = FakeYT()
    db, svc = make_service(tmp_path, nas, yt)
    s = add_stream(db, tmp_path, start_in=2)
    svc.start()
    try:
        s = wait_status(db, s.id, {D.READY, D.LIVE}, 10)      # pre-flight ran
        s = wait_status(db, s.id, {D.LIVE}, 10)
        assert datetime.now(timezone.utc) >= s.start            # not before start time
        s = wait_status(db, s.id, D.FINAL, 60)
    finally:
        svc.shutdown()
    assert s.status == D.DONE, s.message
    assert s.youtube_video_id == "VID123"
    assert ("privacy", "VID123", "unlisted") in yt.calls
    assert ("ensure_live", "VID123") in yt.calls
    out = ffprobe_streams(tmp_path / "out.flv")
    assert "h264,video,1920,1080,yuv420p" in out and "aac,audio" in out
    subjects = [e[0] for e in emails]
    assert subjects[0] == "LIVE: Morning" and subjects[-1] == "FINISHED: Morning"
    assert "UNLISTED" in emails[-1][1]


def test_preflight_failure_emails_once(tmp_path, nas, emails):
    db, svc = make_service(tmp_path, nas)
    s = add_stream(db, tmp_path, start_in=600, path="/video/recordings/missing.mov")
    svc.tick()
    s = wait_status(db, s.id, {D.CHECK_FAILED}, 10)
    assert "not found" in s.message
    svc.preflight(s.id, notify=True)
    assert [e[0] for e in emails] == ["CHECK FAILED: Morning"]


def test_missed_stream(tmp_path, nas, emails):
    db, svc = make_service(tmp_path, nas)
    s = add_stream(db, tmp_path, start_in=-3600)
    svc.tick()
    assert db.get_stream(s.id).status == D.MISSED
    assert emails[0][0] == "MISSED: Morning"


def test_no_youtube_reminds_to_unlist(tmp_path, nas, emails):
    db, svc = make_service(tmp_path, nas, yt=None)
    s = add_stream(db, tmp_path, start_in=0, path="/video/recordings/session2.mp4")
    svc.launch(s.id).join(30)
    assert db.get_stream(s.id).status == D.DONE
    assert "NOT set to unlisted" in emails[-1][1]


def test_recover_after_app_restart(tmp_path, nas, emails):
    db, svc = make_service(tmp_path, nas)
    recent = add_stream(db, tmp_path, start_in=-60, path="/video/recordings/session2.mp4")
    db.update_stream(recent.id, status=D.LIVE, position=2.0,
                     heartbeat=datetime.now(timezone.utc).isoformat())
    old = add_stream(db, tmp_path / "x", start_in=-7200)
    db.update_stream(old.id, status=D.LIVE, position=100.0,
                     heartbeat=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    svc.recover()
    assert db.get_stream(old.id).status == D.INTERRUPTED
    svc.workers[recent.id].join(30)
    s = db.get_stream(recent.id)
    assert s.status == D.DONE and s.position >= 5.5
    assert any(e[0] == "INTERRUPTED: Morning" for e in emails)


def test_stop_from_ui(tmp_path, nas, emails):
    db, svc = make_service(tmp_path, nas)
    s = add_stream(db, tmp_path, start_in=0)
    svc.launch(s.id)
    wait_status(db, s.id, {D.LIVE}, 10)
    time.sleep(2)
    assert svc.stop_stream(s.id)
    svc.workers[s.id].join(15)
    s = db.get_stream(s.id)
    assert s.status == D.STOPPED and 0 < s.position < 6


def _free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def test_real_rtmp_push(tmp_path, nas, emails):
    """Push to an actual RTMP server (ffmpeg in listen mode) like YouTube ingest."""
    port = _free_port()
    received = tmp_path / "received.flv"
    server = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-listen", "1", "-timeout", "30",
         "-i", f"rtmp://127.0.0.1:{port}/live2/abcd-1234", "-c", "copy", "-y", str(received)])
    time.sleep(1)
    db, svc = make_service(tmp_path, nas)
    s = db.save_stream(Stream(
        name="RTMP", nas_path="/video/recordings/session1.mov",
        start_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        rtmp_url=f"rtmp://127.0.0.1:{port}/live2", stream_key="abcd-1234"))
    svc.launch(s.id).join(60)
    server.wait(30)
    assert db.get_stream(s.id).status == D.DONE
    assert "h264,video,1920,1080" in ffprobe_streams(received)
