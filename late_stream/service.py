"""Background scheduler: pre-flight checks, starting streams on time,
recovery, email alerts and post-stream YouTube actions."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db as D
from .db import DB, Stream, utcnow_iso
from .encoder import EncoderSettings, StreamRunner, _hms
from .media import ProbeError, plan_delivery, probe
from .nas import NASError, SynologyClient
from .notify import Notifier

log = logging.getLogger(__name__)

CHECK_RETRY_S = 300          # re-run a failed pre-flight check every 5 min
RESUME_AFTER_RESTART_S = 300  # auto-resume a stream if the app was down < 5 min
RESTART_EMAIL_EVERY_S = 900


def default_nas_factory(settings: dict) -> SynologyClient:
    return SynologyClient(settings["nas_url"], settings["nas_user"], settings["nas_password"],
                          verify_tls=bool(settings.get("nas_verify_tls")))


def default_youtube_factory(db: DB):
    def make(settings: dict):
        if not settings.get("youtube_token_json"):
            return None
        from .youtube import build_client
        return build_client(settings["youtube_token_json"],
                            on_refresh=lambda tok: db.set_settings(youtube_token_json=tok))
    return make


def encoder_settings(s: dict) -> EncoderSettings:
    return EncoderSettings(
        video_encoder=s.get("video_encoder") or "libx264",
        preset=s.get("preset") or None,
        height=int(s["height"]) if s.get("height") else None,
        video_bitrate_kbps=int(s["video_bitrate_kbps"]) if s.get("video_bitrate_kbps") else None,
        audio_bitrate_kbps=int(s.get("audio_bitrate_kbps") or 160),
        keyframe_seconds=float(s.get("keyframe_seconds") or 2),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_local(iso_or_dt, tz: str) -> str:
    dt = datetime.fromisoformat(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
    try:
        z = ZoneInfo(tz or "UTC")
    except Exception:
        z = ZoneInfo("UTC")
    return dt.astimezone(z).strftime("%Y-%m-%d %H:%M %Z")


class Service:
    def __init__(self, db: DB, data_dir: Path, *, nas_factory=None, youtube_factory=None,
                 tick_seconds: float = 5, restart_delay: float = 3.0,
                 youtube_go_live_delay: float = 45, youtube_poll: float = 10):
        self.db = db
        self.data_dir = data_dir
        self.nas_factory = nas_factory or default_nas_factory
        self.youtube_factory = youtube_factory or default_youtube_factory(db)
        self.tick_seconds = tick_seconds
        self.restart_delay = restart_delay
        self.youtube_go_live_delay = youtube_go_live_delay
        self.youtube_poll = youtube_poll
        self.notifier = Notifier(db.settings, db.add_event)
        self.workers: dict[int, "Worker"] = {}
        self._checking: dict[int, float] = {}
        self._active_checks: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        for w in list(self.workers.values()):
            w.stop(reason="app shutting down", final_status=D.INTERRUPTED)
        for w in list(self.workers.values()):
            w.join(timeout=20)

    def _loop(self) -> None:
        log.info("scheduler started")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(self.tick_seconds)

    def event(self, sid: int | None, level: str, msg: str) -> None:
        getattr(log, level if level in ("error", "warning") else "info")("stream %s: %s", sid, msg)
        self.db.add_event(sid, level, msg)

    # ------------------------------------------------------------ scheduling
    def tick(self) -> None:
        settings = self.db.settings()
        now = _now()
        lead = timedelta(minutes=float(settings.get("preflight_minutes") or 60))
        max_late = timedelta(minutes=float(settings.get("max_late_minutes") or 15))
        for s in self.db.list_streams():
            if s.status not in D.WAITING or self.is_running(s.id):
                continue
            start = s.start
            if now >= start:
                if now - start > max_late:
                    self.db.update_stream(s.id, status=D.MISSED,
                                          message="app was not running at start time")
                    self.event(s.id, "error", "missed start time")
                    self.notifier.send(
                        f"MISSED: {s.name}",
                        f"The stream '{s.name}' scheduled for "
                        f"{fmt_local(start, settings['timezone'])} did not start: the app was "
                        f"not running within {max_late} of the start time.\n\n"
                        "Open the app and press 'Start now' if you still want to stream it.",
                        s.id)
                    continue
                self.launch(s.id)
            elif now >= start - lead:
                last = self._checking.get(s.id)
                due = s.status == D.SCHEDULED or (
                    s.status == D.CHECK_FAILED and (last is None or time.monotonic() - last > CHECK_RETRY_S))
                if due and s.id not in self._checking_active():
                    self._checking[s.id] = time.monotonic()
                    threading.Thread(target=self.preflight, args=(s.id, True),
                                     daemon=True).start()

    def _checking_active(self) -> set:
        return self._active_checks

    def preflight(self, sid: int, notify: bool = False) -> tuple[bool, str]:
        """Check the NAS file can be read and decoded; store its duration."""
        self._active_checks.add(sid)
        try:
            s = self.db.get_stream(sid)
            if not s:
                return False, "stream deleted"
            settings = self.db.settings()
            try:
                ok, msg, duration, size = check_source(self.nas_factory(settings), s.nas_path,
                                                       encoder_settings(settings))
            except Exception as e:
                ok, msg, duration, size = False, f"unexpected error: {e}", 0, 0
            cur = self.db.get_stream(sid)
            if not cur or cur.status not in D.WAITING:
                return ok, msg
            if ok:
                self.db.update_stream(sid, status=D.READY, message=msg, duration=duration,
                                      file_size=size)
                self.event(sid, "info", f"pre-flight OK: {msg}")
            else:
                first_failure = cur.status != D.CHECK_FAILED
                self.db.update_stream(sid, status=D.CHECK_FAILED, message=msg)
                self.event(sid, "error", f"pre-flight failed: {msg}")
                if notify and first_failure:
                    self.notifier.send(
                        f"CHECK FAILED: {s.name}",
                        f"The file for '{s.name}' (starts "
                        f"{fmt_local(s.start, settings['timezone'])}) could not be read:\n\n"
                        f"  {s.nas_path}\n  {msg}\n\nThe app retries every 5 minutes "
                        "and will still try to start on time.", sid)
            return ok, msg
        finally:
            self._active_checks.discard(sid)

    # ------------------------------------------------------------ control
    def is_running(self, sid: int | None) -> bool:
        w = self.workers.get(sid) if sid is not None else None
        return bool(w and w.is_alive())

    def launch(self, sid: int, offset: float = 0.0) -> "Worker":
        with self._lock:
            if self.is_running(sid):
                return self.workers[sid]
            w = Worker(self, sid, offset)
            self.workers[sid] = w
            w.start()
            return w

    def stop_stream(self, sid: int) -> bool:
        w = self.workers.get(sid)
        if w and w.is_alive():
            w.stop(reason="stopped from the web UI", final_status=D.STOPPED)
            return True
        return False

    def recover(self) -> None:
        """Called at app start: streams marked live were cut off by a restart."""
        settings = self.db.settings()
        for s in self.db.list_streams():
            if s.status != D.LIVE:
                continue
            hb = datetime.fromisoformat(s.heartbeat) if s.heartbeat else None
            if hb and (_now() - hb).total_seconds() < RESUME_AFTER_RESTART_S:
                self.event(s.id, "warning", f"app restarted mid-stream; resuming at {_hms(s.position)}")
                self.launch(s.id, offset=s.position)
            else:
                self.db.update_stream(s.id, status=D.INTERRUPTED,
                                      message=f"app stopped at {_hms(s.position)}")
                self.event(s.id, "error", "stream interrupted by app restart")
                self.notifier.send(
                    f"INTERRUPTED: {s.name}",
                    f"The app was stopped while '{s.name}' was live (at {_hms(s.position)}). "
                    "Open the app and press 'Resume' to continue from that point.\n"
                    f"Time zone: {settings['timezone']}", s.id)


def check_source(nas: SynologyClient, path: str, enc: EncoderSettings
                 ) -> tuple[bool, str, float, int]:
    try:
        entry = nas.file_info(path)
        if not nas.check_range_support(path):
            return False, ("NAS did not accept byte-range requests; cannot seek in the file "
                           "or resume after a drop"), 0, entry.size
        info = probe(nas.download_url(path), size_bytes=entry.size, display_name=path)
    except (NASError, ProbeError) as e:
        return False, str(e), 0, 0
    plan = plan_delivery(info, enc.force_transcode, enc.audio_streams)
    v = info.video
    target_h = min(v.height, enc.height or v.height)
    desc = (f"{v.codec} {v.profile or ''} {v.width}x{v.height} {v.fps:g}fps, "
            f"{len(info.audio)} audio track(s), {_hms(info.duration)} -> "
            + ("sent as-is" if plan.video_copy else
               f"H.264 {target_h}p {enc.video_bitrate_kbps or 'auto'} kbps"))
    return True, desc, info.duration, entry.size


class Worker(threading.Thread):
    def __init__(self, svc: Service, sid: int, offset: float = 0.0):
        super().__init__(name=f"stream-{sid}", daemon=True)
        self.svc, self.sid, self.offset = svc, sid, offset
        self.runner: StreamRunner | None = None
        self._stop_reason: str | None = None
        self._final_status: str | None = None
        self._stopped = threading.Event()
        self._last_restart_email = 0.0

    def stop(self, reason: str, final_status: str) -> None:
        self._stop_reason, self._final_status = reason, final_status
        self._stopped.set()
        if self.runner:
            self.runner.stop()

    # -------------------------------------------------------------- main
    def run(self) -> None:
        svc, db, sid = self.svc, self.svc.db, self.sid
        s = db.get_stream(sid)
        if not s:
            return
        settings = db.settings()
        tz = settings["timezone"]
        enc = encoder_settings(settings)
        try:
            nas = svc.nas_factory(settings)
            entry = nas.file_info(s.nas_path)
            url = nas.download_url(s.nas_path)
            info = probe(url, size_bytes=entry.size, display_name=s.nas_path)
            plan = plan_delivery(info, enc.force_transcode, enc.audio_streams)
        except Exception as e:
            msg = f"could not open the file on the NAS: {e}"
            db.update_stream(sid, status=D.FAILED, message=msg, ended_at=utcnow_iso())
            svc.event(sid, "error", msg)
            svc.notifier.send(f"FAILED TO START: {s.name}",
                              f"'{s.name}' could not start.\n\nFile: {s.nas_path}\n{msg}", sid)
            return

        db.update_stream(sid, status=D.LIVE, duration=info.duration, file_size=entry.size,
                         started_at=s.started_at if self.offset else utcnow_iso(),
                         ended_at="", position=self.offset, heartbeat=utcnow_iso(),
                         message=f"{plan.mode}: {'; '.join(plan.reasons)}")
        svc.event(sid, "info", f"starting stream at {_hms(self.offset)} ({plan.mode})")
        end_eta = _now() + timedelta(seconds=max(0, info.duration - self.offset))
        svc.notifier.send(
            f"LIVE: {s.name}",
            f"'{s.name}' started streaming at {fmt_local(_now(), tz)}.\n"
            f"File: {s.nas_path}\nLength: {_hms(info.duration)}"
            + (f" (resuming at {_hms(self.offset)})" if self.offset else "")
            + f"\nExpected end: {fmt_local(end_eta, tz)}\nDestination: {s.rtmp_url}",
            sid, kind="start")

        calls = {"n": 0}

        def input_provider() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                return url
            nas._sid = None  # fresh login: the old session may be what failed
            return nas.download_url(s.nas_path)

        def on_progress(pos: float, speed: str) -> None:
            db.update_stream(sid, position=pos, speed=speed, heartbeat=utcnow_iso())

        def on_restart(rc: int, pos: float, restarts: int, err: str) -> None:
            db.update_stream(sid, restarts=restarts, position=pos)
            svc.event(sid, "warning",
                      f"stream dropped at {_hms(pos)} (rc={rc}); reconnecting, attempt {restarts}. "
                      f"{err[-300:]}")
            if time.monotonic() - self._last_restart_email > RESTART_EMAIL_EVERY_S:
                self._last_restart_email = time.monotonic()
                svc.notifier.send(f"RECONNECTING: {s.name}",
                                  f"The stream dropped at {_hms(pos)} and is reconnecting "
                                  f"(attempt {restarts}).\n\nffmpeg said:\n{err[-1000:]}", sid)

        self.runner = StreamRunner(
            info, plan, enc, s.output_url, log_dir=svc.data_dir / "logs" / f"stream-{sid}",
            restart_delay=svc.restart_delay, input_provider=input_provider,
            on_progress=on_progress, on_restart=on_restart)
        if self._stopped.is_set():
            self.runner.stop_event.set()

        if s.youtube_video_id or settings.get("youtube_token_json"):
            threading.Thread(target=self._youtube_go_live, daemon=True).start()

        result = self.runner.run(start_offset=self.offset)

        if result.completed:
            status, msg = D.DONE, f"finished ({_hms(result.position)}, {result.restarts} reconnects)"
        elif self._stopped.is_set():
            status, msg = self._final_status or D.STOPPED, \
                f"{self._stop_reason} at {_hms(result.position)}"
        else:
            status, msg = D.FAILED, f"{result.message} at {_hms(result.position)}"
        db.update_stream(sid, status=status, message=msg, position=result.position,
                         restarts=result.restarts, ended_at=utcnow_iso(), heartbeat=utcnow_iso())
        svc.event(sid, "info" if status == D.DONE else "error", msg)

        yt_note = ""
        if status == D.DONE:
            yt_note = self._youtube_after()
            svc.notifier.send(f"FINISHED: {s.name}",
                              f"'{s.name}' finished at {fmt_local(_now(), tz)}.\n{msg}\n{yt_note}",
                              sid, kind="finish")
        elif status == D.FAILED:
            svc.notifier.send(
                f"FAILED: {s.name}",
                f"'{s.name}' stopped at {_hms(result.position)} of {_hms(info.duration)} and "
                f"could not reconnect.\n{msg}\n\nOpen the app and press 'Resume' to continue "
                "from that point (the YouTube broadcast may need to be restarted in Studio).",
                sid)
        elif status == D.STOPPED:
            svc.notifier.send(f"STOPPED: {s.name}", f"'{s.name}' was stopped manually. {msg}", sid)

    # -------------------------------------------------------------- YouTube
    def _yt(self):
        try:
            return self.svc.youtube_factory(self.svc.db.settings())
        except Exception as e:
            self.svc.event(self.sid, "error", f"YouTube: {e}")
            return None

    def _video_id(self, yt, statuses=("active", "upcoming")) -> str:
        s = self.svc.db.get_stream(self.sid)
        if s.youtube_video_id:
            return s.youtube_video_id
        vid = yt.find_broadcast(s.stream_key, statuses=statuses)
        if vid:
            self.svc.db.update_stream(self.sid, youtube_video_id=vid)
            self.svc.event(self.sid, "info", f"YouTube broadcast found: https://youtu.be/{vid}")
        return vid or ""

    def _youtube_go_live(self) -> None:
        """Safety net if Auto-start is off in Studio."""
        if self._stopped.wait(self.svc.youtube_go_live_delay):
            return
        yt = self._yt()
        if not yt:
            return
        try:
            vid = self._video_id(yt)
            if not vid:
                self.svc.event(self.sid, "warning",
                               "YouTube: no broadcast bound to this stream key was found")
                return
            status = yt.ensure_live(vid, timeout=120)
            self.svc.event(self.sid, "info", f"YouTube broadcast status: {status}")
            if status not in ("live", "liveStarting"):
                self.svc.notifier.send(
                    f"NOT LIVE ON YOUTUBE: {self.svc.db.get_stream(self.sid).name}",
                    f"Video is being sent but the YouTube broadcast is '{status}'. "
                    f"Check YouTube Studio: https://studio.youtube.com/video/{vid}/livestreaming",
                    self.sid)
        except Exception as e:
            self.svc.event(self.sid, "error", f"YouTube go-live check failed: {e}")

    def _youtube_after(self) -> str:
        s = self.svc.db.get_stream(self.sid)
        if not s.unlist_after:
            return ""
        yt = self._yt()
        if not yt:
            return ("YouTube is not connected, so the video was NOT set to unlisted. "
                    "Change it in YouTube Studio.")
        try:
            vid = self._video_id(yt, statuses=("active", "upcoming", "completed"))
            if not vid:
                raise RuntimeError("could not find the broadcast for this stream key")
            yt.wait_complete(vid, timeout=self.svc.youtube_poll * 9)
            yt.set_privacy(vid, "unlisted")
            self.svc.event(self.sid, "info", f"YouTube video {vid} set to unlisted")
            return f"YouTube video https://youtu.be/{vid} is now UNLISTED."
        except Exception as e:
            self.svc.event(self.sid, "error", f"could not set video to unlisted: {e}")
            self.svc.notifier.send(f"UNLIST FAILED: {s.name}",
                                   f"The stream finished but the video could not be set to "
                                   f"unlisted: {e}\nPlease change it in YouTube Studio.", self.sid)
            return f"Setting the video to unlisted FAILED: {e}"
