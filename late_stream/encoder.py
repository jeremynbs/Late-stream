"""Build and supervise the ffmpeg process that pushes a file to YouTube."""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .media import DeliveryPlan, MediaInfo

log = logging.getLogger(__name__)

# Target bitrates (kbps) for H.264 RTMP ingest, inside YouTube's recommended
# ranges. Keyed by (output height bucket, high frame rate?).
BITRATE_TABLE = {
    (2160, True): 35000, (2160, False): 25000,
    (1440, True): 18000, (1440, False): 12000,
    (1080, True): 9000, (1080, False): 6000,
    (720, True): 6000, (720, False): 4000,
    (480, True): 2500, (480, False): 2000,
    (360, True): 1500, (360, False): 1000,
}
MAX_FPS = 60.0


@dataclass
class EncoderSettings:
    video_encoder: str = "libx264"   # libx264 | h264_nvenc | h264_qsv | h264_videotoolbox
    preset: str | None = None        # encoder preset; sensible default per encoder
    height: int | None = None        # downscale to this height (None = keep source)
    video_bitrate_kbps: int | None = None  # None = pick from BITRATE_TABLE
    keyframe_seconds: float = 2.0
    audio_bitrate_kbps: int = 128
    audio_streams: list[int] | None = None  # e.g. [0, 1] to merge two mono tracks
    force_transcode: bool = False
    threads: int | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "EncoderSettings":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"Unknown encoder settings: {sorted(unknown)}")
        return cls(**d)


def output_geometry(info: MediaInfo, s: EncoderSettings) -> tuple[int, float]:
    height = info.video.height
    if s.height and s.height < height:
        height = s.height
    fps = info.video.fps or 30.0
    if fps > MAX_FPS:
        fps = MAX_FPS
    return height, fps


def pick_bitrate(height: int, fps: float) -> int:
    hfr = fps > 31
    for bucket in (2160, 1440, 1080, 720, 480, 360):
        if height >= bucket - 40:  # tolerate e.g. 1036/1044-line sources
            return BITRATE_TABLE[(bucket, hfr)]
    return BITRATE_TABLE[(360, hfr)]


def _encoder_args(s: EncoderSettings, kbps: int, gop: int) -> list[str]:
    rate = [
        "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
        "-g", str(gop), "-keyint_min", str(gop),
    ]
    enc = s.video_encoder
    if enc == "libx264":
        args = ["-c:v", "libx264", "-preset", s.preset or "veryfast",
                "-profile:v", "high", "-sc_threshold", "0",
                "-x264-params", "nal-hrd=cbr:force-cfr=1"]
        if s.threads:
            args += ["-threads", str(s.threads)]
    elif enc == "h264_nvenc":
        args = ["-c:v", "h264_nvenc", "-preset", s.preset or "p4", "-tune", "ll",
                "-profile:v", "high", "-rc", "cbr", "-forced-idr", "1",
                "-no-scenecut", "1"]
    elif enc == "h264_qsv":
        args = ["-c:v", "h264_qsv", "-preset", s.preset or "faster",
                "-profile:v", "high", "-idr_interval", "0"]
    elif enc == "h264_videotoolbox":
        args = ["-c:v", "h264_videotoolbox", "-profile:v", "high",
                "-realtime", "1", "-allow_sw", "1"]
    else:
        raise ValueError(f"Unsupported video_encoder: {enc}")
    return args + rate


def build_ffmpeg_command(
    info: MediaInfo,
    plan: DeliveryPlan,
    settings: EncoderSettings,
    output: str,
    start_offset: float = 0.0,
    realtime: bool = True,
    output_format: str = "flv",
    input_url: str | None = None,
) -> list[str]:
    """Return the ffmpeg argv that sends ``info.path`` to ``output``.

    ``realtime`` paces reading at 1x (required for live). ``start_offset`` is
    used to resume after a dropped connection.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
           "-progress", "pipe:1", "-nostats"]
    if realtime:
        cmd += ["-re"]
    if start_offset > 0:
        cmd += ["-ss", f"{start_offset:.3f}"]
    src = input_url or str(info.path)
    if src.startswith(("http://", "https://")):
        # Survive short NAS/network hiccups without dropping the stream.
        cmd += ["-reconnect", "1", "-reconnect_on_network_error", "1",
                "-reconnect_delay_max", "10", "-rw_timeout", "30000000"]
    cmd += ["-i", src]

    silent = not info.audio
    if silent:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]

    height, fps = output_geometry(info, settings)
    gop = max(1, round(fps * settings.keyframe_seconds))

    if plan.video_copy:
        cmd += ["-map", "0:v:0", "-c:v", "copy"]
    else:
        vf = []
        if info.interlaced:
            vf.append("bwdif=mode=send_frame")
        if height != info.video.height:
            vf.append(f"scale=-2:{height}:flags=lanczos")
        if info.video.fps and info.video.fps > MAX_FPS:
            vf.append(f"fps={MAX_FPS:g}")
        vf.append("format=yuv420p")
        kbps = settings.video_bitrate_kbps or pick_bitrate(height, fps)
        cmd += ["-map", "0:v:0", "-vf", ",".join(vf)]
        cmd += _encoder_args(settings, kbps, gop)

    # ---- audio ----
    if silent:
        cmd += ["-map", "1:a:0", "-shortest"]
    else:
        streams = settings.audio_streams or [info.audio[0].index]
        if len(streams) > 1:
            inputs = "".join(f"[0:a:{i}]" for i in streams)
            cmd += ["-filter_complex",
                    f"{inputs}amerge=inputs={len(streams)}[aout]",
                    "-map", "[aout]"]
        else:
            cmd += ["-map", f"0:a:{streams[0]}"]
    if plan.audio_copy and not silent:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", f"{settings.audio_bitrate_kbps}k",
                "-ar", "48000", "-ac", "2"]

    if output_format == "flv":
        cmd += ["-f", "flv", "-flvflags", "no_duration_filesize"]
    elif output_format == "mp4":
        cmd += ["-movflags", "+faststart", "-f", "mp4"]
    cmd += [output]
    return cmd


def redact(cmd: list[str]) -> str:
    """Command line for logs, without the stream key or NAS session id."""
    from .nas import redact_url
    out = []
    for a in cmd:
        if a.startswith(("rtmp://", "rtmps://")):
            out.append(a.rsplit("/", 1)[0] + "/<stream-key>")
        else:
            out.append(redact_url(a))
    return " ".join(out)


@dataclass
class StreamResult:
    completed: bool
    position: float
    restarts: int
    message: str = ""


@dataclass
class _Progress:
    position: float = 0.0
    speed: str = ""
    last_update: float = field(default_factory=time.monotonic)


class StreamRunner:
    """Runs ffmpeg in real time and restarts it from the last position if the
    connection drops (YouTube keeps a broadcast alive through short outages)."""

    def __init__(self, info: MediaInfo, plan: DeliveryPlan, settings: EncoderSettings,
                 output: str, *, max_restarts: int = 40, restart_delay: float = 3.0,
                 stall_timeout: float = 60.0, log_dir: Path | None = None,
                 realtime: bool = True,
                 input_provider: Callable[[], str] | None = None,
                 on_progress: Callable[[float, str], None] | None = None,
                 on_restart: Callable[[int, float, int, str], None] | None = None):
        self.info, self.plan, self.settings = info, plan, settings
        self.output = output
        self.max_restarts = max_restarts
        self.restart_delay = restart_delay
        self.stall_timeout = stall_timeout
        self.log_dir = log_dir
        self.realtime = realtime
        # Called before every (re)start: lets the caller hand over a fresh
        # NAS URL (new session id) after a failure.
        self.input_provider = input_provider
        self.on_progress = on_progress
        self.on_restart = on_restart
        self.stop_event = threading.Event()
        self.progress = _Progress()
        self.proc: subprocess.Popen | None = None

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _read_progress(self, proc: subprocess.Popen, base: float) -> None:
        assert proc.stdout
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and val.lstrip("-").isdigit():
                us = int(val)
                if us >= 0:
                    self.progress.position = base + us / 1_000_000
                    self.progress.last_update = time.monotonic()
            elif key == "speed":
                self.progress.speed = val

    def run(self, start_offset: float = 0.0) -> StreamResult:
        restarts = 0
        offset = max(0.0, start_offset)
        duration = self.info.duration
        while not self.stop_event.is_set():
            try:
                input_url = self.input_provider() if self.input_provider else None
            except Exception as e:  # NAS unreachable etc.
                input_url = None
                err = f"could not get input: {e}"
                log.error(err)
                restarts += 1
                if self.on_restart:
                    self.on_restart(-1, offset, restarts, err)
                if restarts > self.max_restarts:
                    return StreamResult(False, offset, restarts, err)
                self.stop_event.wait(max(self.restart_delay, 10))
                continue
            cmd = build_ffmpeg_command(self.info, self.plan, self.settings, self.output,
                                       start_offset=offset, realtime=self.realtime,
                                       input_url=input_url)
            log.info("starting ffmpeg at %.1fs: %s", offset, redact(cmd))
            # stderr always goes to a file: an undrained pipe would block
            # ffmpeg after a few hours of warnings.
            if self.log_dir:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                log_file = open(self.log_dir / f"ffmpeg-{int(time.time())}.log", "w+")
            else:
                log_file = tempfile.TemporaryFile("w+")
            started = time.monotonic()
            self.progress = _Progress(position=offset)
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log_file,
                                         text=True, bufsize=1)
            reader = threading.Thread(target=self._read_progress,
                                      args=(self.proc, offset), daemon=True)
            reader.start()
            last_report = started
            last_cb = 0.0
            while self.proc.poll() is None:
                if self.stop_event.wait(1.0):
                    break
                now = time.monotonic()
                if self.on_progress and now - last_cb >= 5:
                    last_cb = now
                    try:
                        self.on_progress(self.progress.position, self.progress.speed)
                    except Exception:
                        log.exception("progress callback failed")
                if now - self.progress.last_update > self.stall_timeout:
                    log.error("ffmpeg made no progress for %.0fs; killing", self.stall_timeout)
                    self.proc.kill()
                    break
                if now - last_report >= 30:
                    last_report = now
                    pct = 100 * self.progress.position / duration if duration else 0
                    log.info("progress %s / %s (%.1f%%) speed=%s",
                             _hms(self.progress.position), _hms(duration), pct,
                             self.progress.speed or "?")
                    sp = _parse_speed(self.progress.speed)
                    if sp is not None and sp < 0.97 and now - started > 60:
                        log.warning("encoder running slower than real time (%.2fx): "
                                    "viewers will buffer. Use a hardware encoder, a lower "
                                    "height, or pre-transcode with `late-stream prepare`.", sp)
            if self.stop_event.is_set() and self.proc.poll() is None:
                self.proc.terminate()
            rc = self.proc.wait()
            reader.join(timeout=5)
            log_file.flush()
            log_file.seek(0)
            stderr_tail = log_file.read()[-1500:]
            log_file.close()
            from .nas import redact_url
            stderr_tail = redact_url(stderr_tail)
            pos = self.progress.position

            if self.stop_event.is_set():
                return StreamResult(False, pos, restarts, "stopped")
            # ffmpeg exits 0 only when it reached the end of the input (we
            # handled our own stop above); network failures exit non-zero.
            if rc == 0:
                log.info("file finished at %s", _hms(pos))
                return StreamResult(True, pos, restarts, "finished")
            # Ran stably for a while -> don't count it against the retry budget.
            if time.monotonic() - started > 300:
                restarts = 0
            restarts += 1
            log.warning("ffmpeg exited rc=%s at %s. %s", rc, _hms(pos), stderr_tail.strip())
            if self.on_restart:
                try:
                    self.on_restart(rc, pos, restarts, stderr_tail.strip())
                except Exception:
                    log.exception("restart callback failed")
            if restarts > self.max_restarts:
                return StreamResult(False, pos, restarts, f"giving up after {restarts - 1} restarts")
            if duration and pos >= duration - 1:
                return StreamResult(True, pos, restarts, "finished (ffmpeg rc != 0 at end)")
            offset = max(0.0, pos)
            # Back off 3 s, 6 s, 9 s ... capped at 30 s: 40 attempts ~= 18 min,
            # enough to ride out a router reboot or ISP hiccup.
            delay = min(self.restart_delay * restarts, 30.0)
            log.info("restarting in %.0fs (attempt %d/%d)", delay, restarts, self.max_restarts)
            self.stop_event.wait(delay)
        return StreamResult(False, self.progress.position, restarts, "stopped")


def _parse_speed(s: str) -> float | None:
    s = (s or "").strip().rstrip("x")
    try:
        return float(s)
    except ValueError:
        return None


def _hms(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def prepare_file(info: MediaInfo, plan: DeliveryPlan, settings: EncoderSettings,
                 output: Path) -> int:
    """Pre-transcode to a YouTube-ready H.264/AAC MP4 (offline, as fast as the
    machine allows). The result can later be streamed with ``-c copy``."""
    plan = DeliveryPlan(video_copy=False, audio_copy=False, reasons=plan.reasons)
    cmd = build_ffmpeg_command(info, plan, settings, str(output), realtime=False,
                               output_format="mp4")
    # show normal stats for an interactive offline job
    cmd = [a for a in cmd if a not in ("-nostats",)]
    i = cmd.index("-progress")
    del cmd[i:i + 2]
    cmd[cmd.index("-loglevel") + 1] = "info"
    log.info("prepare: %s", redact(cmd))
    return subprocess.call(cmd)
