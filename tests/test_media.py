import subprocess

from late_stream.encoder import EncoderSettings, StreamRunner, build_ffmpeg_command, redact
from late_stream.media import plan_delivery, probe


def _streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_name,codec_type,pix_fmt,channels",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return out.stdout


def test_prores_is_transcoded(clips):
    info = probe(clips["prores"])
    assert info.video.codec == "prores"
    assert len(info.audio) == 2
    plan = plan_delivery(info)
    assert not plan.video_copy and not plan.audio_copy


def test_ready_h264_is_copied(clips):
    info = probe(clips["h264_ok"])
    assert info.max_keyframe_interval is not None and info.max_keyframe_interval <= 2.1
    plan = plan_delivery(info)
    assert plan.video_copy and plan.audio_copy
    assert not plan_delivery(info, force_transcode=True).video_copy


def test_long_gop_h264_is_transcoded(clips):
    info = probe(clips["h264_longgop"])
    plan = plan_delivery(info)
    assert not plan.video_copy
    assert any("keyframe" in r for r in plan.reasons)


def test_hevc_without_audio_gets_silence(clips):
    info = probe(clips["hevc_silent"])
    plan = plan_delivery(info)
    assert not plan.video_copy
    cmd = build_ffmpeg_command(info, plan, EncoderSettings(), "out.flv")
    assert "anullsrc=r=48000:cl=stereo" in " ".join(cmd)


def test_command_resume_and_redaction(clips):
    info = probe(clips["prores"])
    plan = plan_delivery(info)
    url = "rtmps://a.rtmps.youtube.com/live2/abcd-efgh-ijkl"
    cmd = build_ffmpeg_command(info, plan, EncoderSettings(audio_streams=[0, 1]), url,
                               start_offset=12.5)
    assert cmd[cmd.index("-ss") + 1] == "12.500"
    assert cmd.index("-ss") < cmd.index("-i")          # fast input seek
    assert "amerge=inputs=2" in " ".join(cmd)
    assert "-g" in cmd and cmd[cmd.index("-g") + 1] == "60"  # 2 s @ 30 fps
    assert "abcd-efgh-ijkl" not in redact(cmd)


def _run_to_file(info, plan, settings, out, realtime=False):
    r = StreamRunner(info, plan, settings, str(out), realtime=realtime, max_restarts=0)
    return r.run()


def test_prores_end_to_end_to_flv(clips, tmp_path):
    info = probe(clips["prores"])
    plan = plan_delivery(info, audio_streams=[0, 1])
    out = tmp_path / "prores.flv"
    res = _run_to_file(info, plan, EncoderSettings(audio_streams=[0, 1]), out)
    assert res.completed, res
    s = _streams(out)
    assert "h264,video,yuv420p" in s and "aac,audio" in s and ",2" in s


def test_copy_end_to_end_realtime(clips, tmp_path):
    info = probe(clips["h264_ok"])
    plan = plan_delivery(info)
    out = tmp_path / "copy.flv"
    import time
    t0 = time.monotonic()
    res = _run_to_file(info, plan, EncoderSettings(), out, realtime=True)
    assert res.completed
    assert time.monotonic() - t0 >= 5  # paced at 1x for a 6 s clip
    assert "h264,video" in _streams(out)


def test_hevc_downscaled_end_to_end(clips, tmp_path):
    info = probe(clips["hevc_silent"])
    plan = plan_delivery(info)
    out = tmp_path / "hevc.flv"
    res = _run_to_file(info, plan, EncoderSettings(height=240), out)
    assert res.completed
    s = _streams(out)
    assert "h264,video" in s and "aac,audio" in s
    h = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=height", "-of", "csv=p=0", str(out)],
                       capture_output=True, text=True).stdout.strip()
    assert h == "240"


def test_resume_after_crash(clips, tmp_path):
    """Kill ffmpeg mid-stream; the runner must resume from the last position."""
    import threading
    import time
    info = probe(clips["prores"])
    plan = plan_delivery(info)
    r = StreamRunner(info, plan, EncoderSettings(), str(tmp_path / "r.flv"),
                     restart_delay=0.5, realtime=True)
    threading.Thread(target=lambda: (time.sleep(3), r.proc.kill()), daemon=True).start()
    res = r.run()
    assert res.completed and res.restarts == 1
    assert res.position >= info.duration - 0.5
