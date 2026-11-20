"""Command line interface.

    late-stream auth     --config schedule.yaml        # one-time OAuth login
    late-stream probe    FILE [FILE...]                # codec report + plan
    late-stream check    --config schedule.yaml        # validate every job
    late-stream prepare  FILE -o OUT.mp4               # optional pre-transcode
    late-stream run-job  --config schedule.yaml JOB_ID [--now] [--output URL]
    late-stream daemon   --config schedule.yaml        # run the schedule
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import timedelta
from pathlib import Path

from .config import ConfigError, load_config
from .encoder import EncoderSettings, prepare_file
from .media import ProbeError, describe, human_size, plan_delivery, probe


def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)


def _yt_factory(cfg, interactive=False):
    from .youtube import YouTubeLive, build_service, load_credentials

    def make():
        creds = load_credentials(cfg.client_secrets, cfg.token_file, interactive=interactive)
        return YouTubeLive(build_service(creds))
    return make


def cmd_auth(args) -> int:
    cfg = load_config(args.config)
    yt = _yt_factory(cfg, interactive=True)()
    resp = yt.yt.channels().list(part="snippet", mine=True).execute()
    for ch in resp.get("items", []):
        print(f"Authorized for channel: {ch['snippet']['title']} ({ch['id']})")
    print(f"Token saved to {cfg.token_file}")
    return 0


def cmd_probe(args) -> int:
    rc = 0
    settings = EncoderSettings(force_transcode=args.force_transcode)
    for f in args.files:
        try:
            info = probe(f)
            print(describe(info, plan_delivery(info, settings.force_transcode)))
        except ProbeError as e:
            print(f"{f}: ERROR {e}", file=sys.stderr)
            rc = 1
        print()
    return rc


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    ok = True
    ends = []
    for job in cfg.jobs:
        print(f"== {job.id}: '{job.title}' at {job.start.isoformat()} "
              f"[{job.privacy}, ingest={job.ingest_stream}]")
        try:
            info = probe(job.file)
            plan = plan_delivery(info, job.encoder.force_transcode, job.encoder.audio_streams)
            print(describe(info, plan))
            ends.append((job, job.start + timedelta(seconds=info.duration)))
        except ProbeError as e:
            print(f"   ERROR: {e}")
            ok = False
        if job.thumbnail and not job.thumbnail.exists():
            print(f"   ERROR: thumbnail not found: {job.thumbnail}")
            ok = False
        print()
    # Overlapping jobs must use different ingest streams.
    for i, (a, a_end) in enumerate(ends):
        for b, b_end in ends[i + 1:]:
            if a.start < b_end and b.start < a_end and a.ingest_stream == b.ingest_stream:
                print(f"ERROR: jobs {a.id} and {b.id} overlap but share ingest_stream "
                      f"'{a.ingest_stream}'. Give one of them a different ingest_stream.")
                ok = False
    print("All jobs OK" if ok else "Problems found")
    return 0 if ok else 1


def cmd_prepare(args) -> int:
    info = probe(args.file)
    settings = EncoderSettings(video_encoder=args.encoder, height=args.height,
                               preset=args.preset)
    plan = plan_delivery(info, True)
    print(describe(info, plan))
    out = Path(args.output)
    print(f"\nWriting YouTube-ready H.264/AAC file to {out} ...")
    rc = prepare_file(info, plan, settings, out)
    if rc == 0:
        print(f"Done: {out} ({human_size(out.stat().st_size)}). "
              "Point the job's `file:` at it to stream with near-zero CPU.")
    return rc


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum, frame):
        logging.getLogger("late_stream").warning("signal %s: stopping", signum)
        stop.set()
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def cmd_run_job(args) -> int:
    from .orchestrator import State, run_job
    cfg = load_config(args.config)
    _setup_logging(args.verbose, cfg.log_dir / "late-stream.log")
    job = cfg.job(args.job_id)
    yt = None if args.output else _yt_factory(cfg)()
    stop = threading.Event()
    _install_signal_handlers(stop)
    ok = run_job(job, cfg, yt, State(cfg.state_file), stop, now=args.now,
                 output_override=args.output)
    return 0 if ok else 1


def cmd_daemon(args) -> int:
    from .orchestrator import Daemon
    cfg = load_config(args.config)
    _setup_logging(args.verbose, cfg.log_dir / "late-stream.log")
    # Fail fast if not authorized.
    _yt_factory(cfg)()
    d = Daemon(Path(args.config), _yt_factory(cfg), poll=args.poll)
    _install_signal_handlers(d.stop)
    d.run_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="late-stream",
                                description="Delayed live streaming of recorded files to YouTube.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("auth", help="log in with Google once and cache the token")
    s.add_argument("--config", "-c", required=True)
    s.set_defaults(func=cmd_auth)

    s = sub.add_parser("probe", help="inspect files and show how they'd be streamed")
    s.add_argument("files", nargs="+")
    s.add_argument("--force-transcode", action="store_true")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("check", help="validate the schedule and probe every file")
    s.add_argument("--config", "-c", required=True)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("prepare", help="pre-transcode (e.g. ProRes) to a YouTube-ready MP4")
    s.add_argument("file")
    s.add_argument("--output", "-o", required=True)
    s.add_argument("--encoder", default="libx264")
    s.add_argument("--height", type=int)
    s.add_argument("--preset", help="e.g. 'medium' for better quality offline")
    s.set_defaults(func=cmd_prepare)

    s = sub.add_parser("run-job", help="run a single job from the schedule")
    s.add_argument("--config", "-c", required=True)
    s.add_argument("job_id")
    s.add_argument("--now", action="store_true", help="start immediately, ignore start time")
    s.add_argument("--output", help="send to this URL/file instead of YouTube (testing)")
    s.set_defaults(func=cmd_run_job)

    s = sub.add_parser("daemon", help="run all scheduled jobs (long-running service)")
    s.add_argument("--config", "-c", required=True)
    s.add_argument("--poll", type=float, default=30, help="seconds between config reloads")
    s.set_defaults(func=cmd_daemon)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd not in ("run-job", "daemon"):
        _setup_logging(args.verbose)
    try:
        return args.func(args)
    except (ConfigError, ProbeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
