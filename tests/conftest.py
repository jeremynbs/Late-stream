import shutil
import subprocess
from pathlib import Path

import pytest

from .fake_synology import FakeSynology


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def clips(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("clips")
    src = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=6"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=6:sample_rate=48000"]
    tone2 = ["-f", "lavfi", "-i", "sine=frequency=880:duration=6:sample_rate=48000"]
    out = {}

    # ProRes 422 HQ, 10-bit 4:2:2, two mono PCM tracks
    out["prores"] = d / "prores.mov"
    ff(*src, *tone, *tone2, "-map", "0:v", "-map", "1:a", "-map", "2:a",
       "-c:v", "prores_ks", "-profile:v", "3", "-c:a", "pcm_s24le", str(out["prores"]))

    # The production format: ProRes Proxy 1080p30, stereo PCM, index at the END
    # of the file (as recorders write it) - forces HTTP range requests.
    out["proxy"] = d / "proxy.mov"
    ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=6",
       "-f", "lavfi", "-i", "sine=f=440:d=6,aformat=channel_layouts=stereo",
       "-c:v", "prores_ks", "-profile:v", "0", "-c:a", "pcm_s16le", str(out["proxy"]))

    # YouTube-ready H.264 + AAC, 2 s GOP
    out["h264_ok"] = d / "h264_ok.mp4"
    ff(*src, *tone, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
       "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
       "-c:a", "aac", "-b:a", "128k", str(out["h264_ok"]))

    # H.264 with a 10 s GOP (too long for YouTube) on a 12 s clip
    out["h264_longgop"] = d / "h264_longgop.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=12", *tone,
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
       "-g", "300", "-keyint_min", "300", "-sc_threshold", "0", "-shortest",
       "-c:a", "aac", str(out["h264_longgop"]))

    # HEVC, no audio
    out["hevc_silent"] = d / "hevc.mp4"
    ff(*src, "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
       str(out["hevc_silent"]))
    return out


@pytest.fixture
def nas(tmp_path, clips):
    root = tmp_path / "nas"
    (root / "video" / "recordings").mkdir(parents=True)
    (root / "homes").mkdir()
    shutil.copy(clips["proxy"], root / "video" / "recordings" / "session1.mov")
    shutil.copy(clips["h264_ok"], root / "video" / "recordings" / "session2.mp4")
    (root / "video" / "recordings" / "notes.txt").write_text("not a video")
    fake = FakeSynology(root)
    yield fake
    fake.close()


def ffprobe_streams(path) -> str:
    return subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                           "stream=codec_name,codec_type,width,height,pix_fmt,channels",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
