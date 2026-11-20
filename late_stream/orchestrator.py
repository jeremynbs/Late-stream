"""Run one scheduled job end to end, and the daemon that runs them all."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, Job, load_config
from .encoder import StreamRunner
from .media import describe, plan_delivery, probe

log = logging.getLogger(__name__)


class State:
    """Small JSON file so a restarted daemon doesn't create duplicate
    broadcasts or re-run finished jobs."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = {}
        if path.exists():
            self.data = json.loads(path.read_text() or "{}")

    def get(self, job_id: str) -> dict:
        with self._lock:
            return dict(self.data.get(job_id, {}))

    def update(self, job_id: str, **kw) -> None:
        with self._lock:
            self.data.setdefault(job_id, {}).update(kw)
            tmp = self.path.with_suffix(".tmp")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.data, indent=2, default=str))
            tmp.replace(self.path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sleep_until(when: datetime, stop: threading.Event) -> bool:
    """Sleep until ``when``. Returns False if stopped early."""
    while True:
        remaining = (when - _now()).total_seconds()
        if remaining <= 0:
            return True
        if stop.wait(min(remaining, 30)):
            return False


def prepare_broadcast(job: Job, yt, state: State) -> tuple[str, str, str]:
    """Create (or recover from state) the broadcast + ingest stream and bind
    them. Returns (broadcast_id, stream_id, ingest_url)."""
    st = state.get(job.id)
    ingest = yt.get_or_create_stream(job.ingest_stream)
    bid = st.get("broadcast_id")
    if bid:
        status = yt.broadcast_status(bid)
        if status in ("complete", "revoked"):
            raise RuntimeError(f"broadcast {bid} for job {job.id} is already {status}")
        log.info("reusing broadcast %s (status %s)", bid, status)
    else:
        b = yt.create_broadcast(
            title=job.title, description=job.description, start=job.start,
            privacy=job.privacy, auto_start=job.auto_start, auto_stop=job.auto_stop,
            latency=job.latency, enable_dvr=job.enable_dvr,
            made_for_kids=job.made_for_kids, category_id=job.category_id)
        bid = b["id"]
        state.update(job.id, broadcast_id=bid, watch_url=f"https://youtu.be/{bid}",
                     status="scheduled")
        if job.thumbnail:
            try:
                yt.set_thumbnail(bid, job.thumbnail)
            except Exception as e:  # thumbnail is cosmetic; don't fail the stream
                log.warning("thumbnail upload failed: %s", e)
    if st.get("bound_stream") != ingest.stream_id:
        yt.bind(bid, ingest.stream_id)
        state.update(job.id, bound_stream=ingest.stream_id)
    return bid, ingest.stream_id, ingest.url


def run_job(job: Job, cfg: Config, yt, state: State, stop: threading.Event | None = None,
            *, now: bool = False, output_override: str | None = None) -> bool:
    """Probe, announce, wait for start time, stream, finish. Returns success."""
    stop = stop or threading.Event()
    jl = logging.getLogger(f"late_stream.job.{job.id}")
    try:
        info = probe(job.file)
        plan = plan_delivery(info, job.encoder.force_transcode, job.encoder.audio_streams)
        jl.info("\n%s", describe(info, plan))
        state.update(job.id, duration=info.duration, mode=plan.mode)

        if output_override:
            bid, stream_id, url = None, None, output_override
        else:
            bid, stream_id, url = prepare_broadcast(job, yt, state)
            jl.info("broadcast ready: https://youtu.be/%s (starts %s)", bid,
                    job.start.isoformat())

        if not now:
            jl.info("waiting for start time %s", job.start.isoformat())
            if not _sleep_until(job.start, stop):
                state.update(job.id, status="cancelled")
                return False

        late = (_now() - job.start).total_seconds() / 60
        if not now and late > job.max_late_minutes:
            jl.error("missed start by %.0f min (max_late_minutes=%s); skipping",
                     late, job.max_late_minutes)
            state.update(job.id, status="missed")
            return False

        runner = StreamRunner(info, plan, job.encoder, url,
                              log_dir=cfg.log_dir / job.id)
        stop_watch = threading.Thread(target=lambda: (stop.wait(), runner.stop()), daemon=True)
        stop_watch.start()

        if bid and not job.auto_start:
            threading.Thread(target=_go_live_when_ready, args=(yt, bid, stream_id, jl),
                             daemon=True).start()

        state.update(job.id, status="live", started_at=_now().isoformat())
        result = runner.run()
        jl.info("stream ended: %s (position %.0fs, restarts %d)", result.message,
                result.position, result.restarts)

        if bid:
            _finish(yt, bid, jl)
        state.update(job.id, status="done" if result.completed else "failed",
                     ended_at=_now().isoformat(), result=result.message,
                     api_units_used=getattr(yt, "units_used", None))
        return result.completed
    except Exception as e:
        jl.exception("job failed: %s", e)
        state.update(job.id, status="failed", error=str(e))
        return False


def _go_live_when_ready(yt, bid: str, stream_id: str, jl) -> None:
    if not yt.wait_stream_active(stream_id, timeout=180):
        jl.error("ingest never became active; broadcast not started")
        return
    status = yt.broadcast_status(bid)
    if status == "ready":
        yt.transition(bid, "live")


def _finish(yt, bid: str, jl) -> None:
    try:
        status = yt.broadcast_status(bid)
        if status in ("live", "liveStarting", "testing"):
            yt.transition(bid, "complete")
    except Exception as e:
        jl.warning("could not complete broadcast %s: %s", bid, e)


class Daemon:
    """Re-reads the config every ``poll`` seconds and launches each job
    ``announce_minutes`` before its start time."""

    def __init__(self, config_path: Path, yt_factory, poll: float = 30):
        self.config_path = config_path
        self.yt_factory = yt_factory
        self.poll = poll
        self.stop = threading.Event()
        self.threads: dict[str, threading.Thread] = {}

    def tick(self) -> None:
        cfg = load_config(self.config_path)
        state = self._state(cfg)
        for job in cfg.jobs:
            if job.id in self.threads and self.threads[job.id].is_alive():
                continue
            st = state.get(job.id).get("status")
            if st in ("done", "failed", "missed", "cancelled"):
                continue
            if job.id in self.threads:  # thread finished but state not final
                continue
            announce_at = job.start - timedelta(minutes=job.announce_minutes)
            if _now() >= announce_at:
                if (_now() - job.start).total_seconds() / 60 > job.max_late_minutes:
                    if st != "missed":
                        log.error("job %s start %s already passed; marking missed",
                                  job.id, job.start)
                        state.update(job.id, status="missed")
                    continue
                log.info("launching job %s (%s)", job.id, job.title)
                t = threading.Thread(target=run_job,
                                     args=(job, cfg, self.yt_factory(), state, self.stop),
                                     name=f"job-{job.id}", daemon=True)
                self.threads[job.id] = t
                t.start()

    _state_obj: State | None = None

    def _state(self, cfg: Config) -> State:
        if self._state_obj is None or self._state_obj.path != cfg.state_file:
            self._state_obj = State(cfg.state_file)
        return self._state_obj

    def run_forever(self) -> None:
        log.info("daemon started; watching %s", self.config_path)
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log.exception("scheduler tick failed: %s", e)
            self.stop.wait(self.poll)
        for t in self.threads.values():
            t.join(timeout=30)

    def shutdown(self) -> None:
        self.stop.set()
