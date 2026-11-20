import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from late_stream.config import ConfigError, load_config
from late_stream.orchestrator import State, run_job
from late_stream.youtube import IngestInfo


class FakeYT:
    def __init__(self, sink):
        self.sink = sink
        self.calls = []
        self.status = "ready"
        self.units_used = 0

    def get_or_create_stream(self, title):
        self.calls.append(("stream", title))
        return IngestInfo("S1", str(self.sink))

    def create_broadcast(self, **kw):
        self.calls.append(("broadcast", kw["title"], kw["privacy"]))
        return {"id": "B1"}

    def bind(self, b, s):
        self.calls.append(("bind", b, s))

    def broadcast_status(self, b):
        return self.status

    def transition(self, b, st):
        self.calls.append(("transition", st))

    def set_thumbnail(self, b, p):
        self.calls.append(("thumb",))


def write_cfg(tmp_path, clip, start, extra=""):
    p = tmp_path / "s.yaml"
    p.write_text(textwrap.dedent(f"""
        state_file: ./state.json
        log_dir: ./logs
        defaults:
          privacy: unlisted
          encoder: {{video_encoder: libx264}}
        jobs:
          - id: j1
            file: {clip}
            start: {start}
            title: Test
        {extra}
    """))
    return load_config(p)


def test_config_requires_timezone(tmp_path, clips):
    with pytest.raises(ConfigError):
        write_cfg(tmp_path, clips["h264_ok"], "2026-11-21T09:00:00")


def test_run_job_flow(tmp_path, clips):
    start = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    cfg = write_cfg(tmp_path, clips["h264_ok"], start)
    sink = tmp_path / "out.flv"
    yt = FakeYT(sink)
    state = State(cfg.state_file)
    yt.status = "live"
    assert run_job(cfg.jobs[0], cfg, yt, state)
    kinds = [c[0] for c in yt.calls]
    assert kinds == ["stream", "broadcast", "bind", "transition"]
    assert yt.calls[-1] == ("transition", "complete")
    st = state.get("j1")
    assert st["status"] == "done" and st["broadcast_id"] == "B1"
    assert sink.stat().st_size > 0

    # Re-running reuses the broadcast instead of creating a duplicate.
    yt2 = FakeYT(tmp_path / "out2.flv")
    yt2.status = "ready"
    assert run_job(cfg.jobs[0], cfg, yt2, State(cfg.state_file), now=True)
    assert "broadcast" not in [c[0] for c in yt2.calls]


def test_missed_job_is_skipped(tmp_path, clips):
    start = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    cfg = write_cfg(tmp_path, clips["h264_ok"], start)
    yt = FakeYT(tmp_path / "x.flv")
    state = State(cfg.state_file)
    assert not run_job(cfg.jobs[0], cfg, yt, state)
    assert state.get("j1")["status"] == "missed"
