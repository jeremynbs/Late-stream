import pytest

from late_stream.encoder import EncoderSettings, StreamRunner
from late_stream.media import plan_delivery, probe
from late_stream.nas import NASError, SynologyClient, redact_url

from .conftest import ffprobe_streams


def client(nas, **kw):
    return SynologyClient(nas.url, kw.get("user", "streamer"), kw.get("pw", "pw"))


def test_wrong_password(nas):
    with pytest.raises(NASError, match="wrong account or password"):
        client(nas, pw="nope").list_shares()


def test_browse(nas):
    c = client(nas)
    assert [s.path for s in c.list_shares()] == ["/homes", "/video"]
    names = [e.name for e in c.list_folder("/video/recordings")]
    assert names == ["session1.mov", "session2.mp4"]   # .txt filtered out
    info = c.file_info("/video/recordings/session1.mov")
    assert info.size > 1_000_000
    with pytest.raises(NASError, match="not found"):
        c.file_info("/video/recordings/missing.mov")


def test_session_expiry_relogin(nas):
    c = client(nas)
    c.list_shares()
    nas.expire_sessions()
    c.list_shares()            # transparently logs in again
    assert nas.logins == 2


def test_range_and_probe_over_http(nas):
    c = client(nas)
    path = "/video/recordings/session1.mov"
    assert c.check_range_support(path)
    url = c.download_url(path)
    assert "_sid=<sid>" in redact_url(url)
    info = probe(url, display_name=path)
    assert info.video.codec == "prores" and info.video.profile == "Proxy"
    assert (info.video.width, info.video.height, info.video.fps) == (1920, 1080, 30.0)
    assert info.audio[0].channels == 2


def test_no_range_support_detected(nas):
    nas.range_support = False
    assert not client(nas).check_range_support("/video/recordings/session1.mov")


def test_stream_from_nas_survives_drop(nas, tmp_path):
    """NAS connection drops mid-file; ffmpeg's own reconnect or our restart
    (with a fresh login) must still deliver the whole file."""
    c = client(nas)
    path = "/video/recordings/session1.mov"
    info = probe(c.download_url(path))
    plan = plan_delivery(info)
    nas.fail_downloads = 1
    out = tmp_path / "out.flv"

    def provider():
        c._sid = None
        return c.download_url(path)
    r = StreamRunner(info, plan, EncoderSettings(height=1080, video_bitrate_kbps=8000),
                     str(out), realtime=False, input_provider=provider, restart_delay=0.5)
    res = r.run()
    assert res.completed, res
    s = ffprobe_streams(out)
    assert "h264,video,1920,1080,yuv420p" in s and "aac,audio" in s
