"""Inspect source files with ffprobe and decide how to deliver them to YouTube.

YouTube's RTMP(S) ingest expects H.264 video (8-bit 4:2:0) + AAC audio in FLV,
with a keyframe at least every 4 seconds (2 s recommended). Anything else
(ProRes, DNxHD, HEVC in RTMP, 10-bit/4:2:2 H.264, long-GOP H.264, PCM audio,
...) must be transcoded on the fly. A file that already matches can be sent
with ``-c:v copy``, which needs almost no CPU.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

# Codecs we know ffmpeg can decode and we've thought about. Anything ffprobe
# can read will still be attempted; this list is only for the probe report.
KNOWN_VIDEO_CODECS = {
    "prores", "h264", "hevc", "dnxhd", "mpeg2video", "mpeg4", "vp9", "av1",
    "mjpeg", "cfhd", "rawvideo", "v210",
}

COPY_SAFE_PIX_FMTS = {"yuv420p", "yuvj420p"}
MAX_KEYFRAME_INTERVAL_S = 4.0
COPY_SAFE_AUDIO = {"aac"}


class ProbeError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    codec: str
    profile: str | None
    pix_fmt: str | None
    width: int
    height: int
    fps: float
    field_order: str | None
    bit_rate: int | None


@dataclass
class AudioInfo:
    index: int  # index among audio streams (0:a:N)
    codec: str
    channels: int
    sample_rate: int


@dataclass
class MediaInfo:
    path: Path | str  # local path or URL
    duration: float
    size_bytes: int
    format_name: str
    video: VideoInfo
    audio: list[AudioInfo] = field(default_factory=list)
    max_keyframe_interval: float | None = None  # seconds, sampled
    display_name: str | None = None  # what to show instead of a (secret) URL

    @property
    def interlaced(self) -> bool:
        return bool(self.video.field_order) and self.video.field_order not in (
            "progressive", "unknown")


@dataclass
class DeliveryPlan:
    video_copy: bool
    audio_copy: bool
    reasons: list[str]

    @property
    def mode(self) -> str:
        return "copy" if self.video_copy else "transcode"


def _run_json(cmd: list[str]) -> dict:
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ProbeError("ffprobe not found on PATH; install ffmpeg") from e
    except subprocess.CalledProcessError as e:
        raise ProbeError(f"ffprobe failed: {e.stderr.strip()}") from e
    return json.loads(out.stdout or "{}")


def _parse_rate(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return 0.0


def sample_keyframe_interval(path: Path | str, seconds: float = 120.0) -> float | None:
    """Largest gap between keyframes in the first ``seconds`` of the file."""
    data = _run_json([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey", "-read_intervals", f"%+{seconds}",
        "-show_entries", "frame=pts_time,best_effort_timestamp_time",
        "-of", "json", str(path),
    ])
    times = []
    for f in data.get("frames", []):
        t = f.get("pts_time") or f.get("best_effort_timestamp_time")
        if t not in (None, "N/A"):
            times.append(float(t))
    if len(times) < 2:
        return None
    times.sort()
    return max(b - a for a, b in zip(times, times[1:]))


def is_url(src) -> bool:
    return str(src).startswith(("http://", "https://"))


def probe(path: str | Path, check_gop: bool = True, size_bytes: int | None = None,
          display_name: str | None = None) -> MediaInfo:
    """Probe a local file or an HTTP(S) URL (e.g. a NAS download link)."""
    src = str(path)
    if not is_url(src):
        path = Path(path)
        if not path.is_file():
            raise ProbeError(f"File not found: {path}")
    data = _run_json([
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", src,
    ])
    streams = data.get("streams", [])
    vstreams = [s for s in streams if s.get("codec_type") == "video"
                and not s.get("disposition", {}).get("attached_pic")]
    if not vstreams:
        raise ProbeError(f"No video stream in {path}")
    v = vstreams[0]
    fps = _parse_rate(v.get("avg_frame_rate")) or _parse_rate(v.get("r_frame_rate"))
    video = VideoInfo(
        codec=v.get("codec_name", "unknown"),
        profile=v.get("profile"),
        pix_fmt=v.get("pix_fmt"),
        width=int(v.get("width", 0)),
        height=int(v.get("height", 0)),
        fps=round(fps, 3) if fps else 0.0,
        field_order=v.get("field_order"),
        bit_rate=int(v["bit_rate"]) if str(v.get("bit_rate", "")).isdigit() else None,
    )
    audio = []
    for i, s in enumerate(s for s in streams if s.get("codec_type") == "audio"):
        audio.append(AudioInfo(
            index=i,
            codec=s.get("codec_name", "unknown"),
            channels=int(s.get("channels", 0)),
            sample_rate=int(s.get("sample_rate", 0) or 0),
        ))
    fmt = data.get("format", {})
    size = size_bytes or int(fmt.get("size", 0) or 0)
    if not size and not is_url(src):
        size = Path(src).stat().st_size
    info = MediaInfo(
        path=path if not is_url(src) else src,  # type: ignore[arg-type]
        display_name=display_name,
        duration=float(fmt.get("duration", 0) or 0),
        size_bytes=size,
        format_name=fmt.get("format_name", ""),
        video=video,
        audio=audio,
    )
    # Intra-only codecs (ProRes, DNxHD...) are transcoded anyway, so skip the
    # GOP scan for them - it would read every frame header.
    if check_gop and video.codec == "h264":
        info.max_keyframe_interval = sample_keyframe_interval(path)
    return info


def plan_delivery(info: MediaInfo, force_transcode: bool = False,
                  audio_streams: list[int] | None = None) -> DeliveryPlan:
    reasons: list[str] = []
    v = info.video
    video_copy = True
    if force_transcode:
        video_copy = False
        reasons.append("transcode forced by config")
    if v.codec != "h264":
        video_copy = False
        reasons.append(f"video codec is {v.codec}; YouTube RTMP ingest needs H.264")
    if v.pix_fmt not in COPY_SAFE_PIX_FMTS:
        video_copy = False
        reasons.append(f"pixel format {v.pix_fmt} is not 8-bit 4:2:0")
    if info.interlaced:
        video_copy = False
        reasons.append(f"source is interlaced ({v.field_order}); will deinterlace")
    if v.codec == "h264":
        kfi = info.max_keyframe_interval
        if kfi is None:
            video_copy = False
            reasons.append("could not verify keyframe interval")
        elif kfi > MAX_KEYFRAME_INTERVAL_S + 0.05:
            video_copy = False
            reasons.append(
                f"keyframe interval {kfi:.1f}s > {MAX_KEYFRAME_INTERVAL_S:.0f}s")
    if video_copy:
        reasons.append("source is YouTube-ready H.264; sending without re-encoding")

    selected = _selected_audio(info, audio_streams)
    audio_copy = (video_copy and len(selected) == 1
                  and selected[0].codec in COPY_SAFE_AUDIO
                  and selected[0].channels in (1, 2)
                  and selected[0].sample_rate in (44100, 48000))
    if not info.audio:
        reasons.append("no audio track; a silent track will be added")
    return DeliveryPlan(video_copy=video_copy, audio_copy=audio_copy, reasons=reasons)


def _selected_audio(info: MediaInfo, audio_streams: list[int] | None) -> list[AudioInfo]:
    if not info.audio:
        return []
    if audio_streams:
        by_idx = {a.index: a for a in info.audio}
        missing = [i for i in audio_streams if i not in by_idx]
        if missing:
            raise ProbeError(
                f"audio_streams {missing} not present; file has {len(info.audio)} audio stream(s)")
        return [by_idx[i] for i in audio_streams]
    return [info.audio[0]]


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def describe(info: MediaInfo, plan: DeliveryPlan) -> str:
    v = info.video
    lines = [
        f"File:       {info.display_name or info.path}",
        f"Size:       {human_size(info.size_bytes)}",
        f"Duration:   {info.duration / 3600:.2f} h ({info.duration:.0f} s)",
        f"Container:  {info.format_name}",
        f"Video:      {v.codec} {v.profile or ''} {v.width}x{v.height} @ {v.fps} fps, "
        f"{v.pix_fmt}, {v.field_order or 'progressive?'}",
    ]
    if v.codec not in KNOWN_VIDEO_CODECS:
        lines.append(f"            (uncommon codec '{v.codec}' - will try to decode with ffmpeg)")
    if info.max_keyframe_interval is not None:
        lines.append(f"Keyframes:  max interval {info.max_keyframe_interval:.2f} s (first 120 s)")
    for a in info.audio:
        lines.append(f"Audio #{a.index}:   {a.codec} {a.channels}ch {a.sample_rate} Hz")
    if not info.audio:
        lines.append("Audio:      none")
    lines.append(f"Plan:       {plan.mode.upper()} video, "
                 f"{'copy' if plan.audio_copy else 'encode AAC'} audio")
    for r in plan.reasons:
        lines.append(f"            - {r}")
    return "\n".join(lines)
