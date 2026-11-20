"""Command line interface.

    late-stream serve   [--data-dir ./data] [--host 0.0.0.0] [--port 8080]
    late-stream probe   FILE_OR_URL [...]       # codec report + streaming plan
    late-stream prepare FILE -o OUT.mp4         # optional offline pre-transcode
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from .encoder import EncoderSettings, prepare_file
from .media import ProbeError, describe, human_size, plan_delivery, probe


def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10_000_000, backupCount=5))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=handlers)
    for noisy in ("googleapiclient", "waitress", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_serve(args) -> int:
    from .db import DB
    from .service import Service
    from .web import create_app

    data = Path(args.data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data, 0o700)
    except OSError:
        pass
    _setup_logging(args.verbose, data / "logs" / "app.log")
    log = logging.getLogger("late_stream")
    db = DB(data / "late-stream.db")
    service = Service(db, data)
    app = create_app(db, service, port=args.port)
    service.start()

    def shutdown(signum, frame):
        log.warning("signal %s: stopping streams and exiting", signum)
        service.shutdown()
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("web UI on http://%s:%d  (data: %s)", args.host, args.port, data)
    from waitress import serve
    serve(app, host=args.host, port=args.port, threads=8, ident="late-stream")
    return 0


def cmd_probe(args) -> int:
    rc = 0
    for f in args.files:
        try:
            info = probe(f)
            print(describe(info, plan_delivery(info, args.force_transcode)))
        except ProbeError as e:
            print(f"{f}: ERROR {e}", file=sys.stderr)
            rc = 1
        print()
    return rc


def cmd_prepare(args) -> int:
    info = probe(args.file)
    settings = EncoderSettings(video_encoder=args.encoder, height=args.height,
                               preset=args.preset, video_bitrate_kbps=args.bitrate)
    plan = plan_delivery(info, True)
    print(describe(info, plan))
    out = Path(args.output)
    print(f"\nWriting YouTube-ready H.264/AAC file to {out} ...")
    rc = prepare_file(info, plan, settings, out)
    if rc == 0:
        print(f"Done: {out} ({human_size(out.stat().st_size)})")
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="late-stream",
                                description="Scheduled RTMP streaming of recorded files to YouTube.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web UI and the scheduler")
    s.add_argument("--data-dir", default=os.environ.get("LATE_STREAM_DATA", "./data"))
    s.add_argument("--host", default=os.environ.get("LATE_STREAM_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("LATE_STREAM_PORT", "8080")))
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("probe", help="inspect files/URLs and show how they'd be streamed")
    s.add_argument("files", nargs="+")
    s.add_argument("--force-transcode", action="store_true")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("prepare", help="pre-transcode to a YouTube-ready MP4 (offline)")
    s.add_argument("file")
    s.add_argument("--output", "-o", required=True)
    s.add_argument("--encoder", default="libx264")
    s.add_argument("--height", type=int)
    s.add_argument("--bitrate", type=int, help="video kbps")
    s.add_argument("--preset", help="e.g. 'medium' for better quality offline")
    s.set_defaults(func=cmd_prepare)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd != "serve":
        _setup_logging(args.verbose)
    try:
        return args.func(args)
    except ProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
