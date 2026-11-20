"""Web UI: settings, stream setup (with NAS file browser) and live status."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as D
from .db import DB, Stream, find_overlap, mask
from .encoder import _hms
from .media import human_size
from .nas import NASError
from .notify import EmailError, send_email
from .service import Service, check_source, encoder_settings, fmt_local

log = logging.getLogger(__name__)

STATUS_LABEL = {
    D.SCHEDULED: "Scheduled", D.READY: "Ready", D.CHECK_FAILED: "Check failed",
    D.LIVE: "LIVE", D.DONE: "Done", D.FAILED: "Failed", D.MISSED: "Missed",
    D.STOPPED: "Stopped", D.INTERRUPTED: "Interrupted",
}
ENCODERS = [("libx264", "CPU (libx264)"), ("h264_nvenc", "NVIDIA GPU (NVENC)"),
            ("h264_qsv", "Intel Quick Sync"), ("h264_videotoolbox", "Apple VideoToolbox (Mac)")]
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]


def create_app(db: DB, service: Service, *, port: int = 8080) -> Flask:
    app = Flask(__name__)
    if not db.get_setting("secret_key"):
        db.set_settings(secret_key=secrets.token_hex(32))
    app.secret_key = db.get_setting("secret_key")
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    app.config["PORT"] = port

    # ------------------------------------------------------------ helpers
    def settings() -> dict:
        return db.settings()

    def tzname() -> str:
        return settings().get("timezone") or "UTC"

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    @app.before_request
    def guard():
        if request.endpoint in ("static", "setup", "login", "oauth_callback"):
            return
        if not db.get_setting("admin_password_hash"):
            return redirect(url_for("setup"))
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(error="login required"), 401
            return redirect(url_for("login", next=request.path))
        if request.method == "POST":
            tok = request.form.get("csrf") or request.headers.get("X-CSRF")
            if not tok or tok != session.get("csrf"):
                abort(400, "invalid form token; reload the page")

    @app.context_processor
    def inject():
        return {"csrf_token": csrf_token, "tzname": tzname(), "STATUS_LABEL": STATUS_LABEL,
                "hms": _hms, "human_size": human_size, "mask": mask,
                "local": lambda iso: fmt_local(iso, tzname()) if iso else ""}

    def to_utc(date_s: str, time_s: str) -> str:
        local = datetime.fromisoformat(f"{date_s}T{time_s}")
        return local.replace(tzinfo=ZoneInfo(tzname())).astimezone(timezone.utc) \
            .isoformat(timespec="seconds")

    def to_local_parts(iso: str) -> tuple[str, str]:
        dt = datetime.fromisoformat(iso).astimezone(ZoneInfo(tzname()))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")

    def stream_json(s: Stream) -> dict:
        d = s.public_dict()
        d.update(status_label=STATUS_LABEL.get(s.status, s.status),
                 start_local=fmt_local(s.start_utc, tzname()),
                 position_hms=_hms(s.position), duration_hms=_hms(s.duration),
                 percent=round(100 * s.position / s.duration, 1) if s.duration else 0,
                 running=service.is_running(s.id))
        return d

    # ------------------------------------------------------------ auth
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if db.get_setting("admin_password_hash"):
            return redirect(url_for("index"))
        if request.method == "POST":
            pw = request.form.get("password", "")
            if len(pw) < 8 or pw != request.form.get("password2"):
                flash("Password must be at least 8 characters and both fields must match.", "error")
            else:
                tz = request.form.get("timezone") or "UTC"
                db.set_settings(admin_password_hash=generate_password_hash(pw),
                                timezone=tz if tz in available_timezones() else "UTC")
                session["user"] = "admin"
                flash("Welcome! Configure the NAS and email first.", "ok")
                return redirect(url_for("settings_page"))
        return render_template("setup.html", timezones=sorted(available_timezones()))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            h = db.get_setting("admin_password_hash")
            if h and check_password_hash(h, request.form.get("password", "")):
                session.clear()
                session["user"] = "admin"
                nxt = request.args.get("next", "/")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            flash("Wrong password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------ streams
    @app.get("/")
    def index():
        streams = db.list_streams()
        upcoming = [s for s in streams if s.status not in D.FINAL]
        past = sorted([s for s in streams if s.status in D.FINAL],
                      key=lambda s: s.start_utc, reverse=True)[:50]
        st = settings()
        warnings = []
        if not st.get("nas_url"):
            warnings.append("NAS is not configured.")
        if not (st.get("smtp_host") and st.get("alert_to")):
            warnings.append("Email alerts are not configured.")
        if not st.get("youtube_token_json"):
            warnings.append("YouTube is not connected: videos will not be set to unlisted "
                            "automatically after the stream.")
        return render_template("index.html", upcoming=upcoming, past=past,
                               running={s.id for s in streams if service.is_running(s.id)},
                               warnings=warnings, now=fmt_local(datetime.now(timezone.utc), tzname()))

    @app.get("/api/streams")
    def api_streams():
        return jsonify([stream_json(s) for s in db.list_streams()])

    @app.get("/streams/new")
    def stream_new():
        s = Stream(rtmp_url=settings()["default_rtmp_url"], unlist_after=True)
        copy_id = request.args.get("copy", type=int)
        if copy_id and (src := db.get_stream(copy_id)):
            s = Stream(name=src.name, rtmp_url=src.rtmp_url, stream_key=src.stream_key,
                       unlist_after=src.unlist_after, start_utc=src.start_utc)
        date_s, time_s = to_local_parts(s.start_utc) if s.start_utc else ("", "")
        return render_template("stream_form.html", s=s, date_s=date_s, time_s=time_s,
                               copy_id=copy_id)

    @app.get("/streams/<int:sid>/edit")
    def stream_edit(sid):
        s = db.get_stream(sid) or abort(404)
        if service.is_running(sid):
            flash("A live stream can't be edited. Stop it first.", "error")
            return redirect(url_for("stream_detail", sid=sid))
        date_s, time_s = to_local_parts(s.start_utc)
        return render_template("stream_form.html", s=s, date_s=date_s, time_s=time_s)

    @app.post("/streams")
    @app.post("/streams/<int:sid>")
    def stream_save(sid=None):
        from .youtube import parse_video_id
        f = request.form
        existing = db.get_stream(sid) if sid else None
        if sid and not existing:
            abort(404)
        if existing and service.is_running(sid):
            flash("A live stream can't be edited.", "error")
            return redirect(url_for("stream_detail", sid=sid))
        s = existing or Stream()
        s.name = f.get("name", "").strip()
        s.nas_path = f.get("nas_path", "").strip()
        s.rtmp_url = f.get("rtmp_url", "").strip()
        key = f.get("stream_key", "").strip()
        if key:
            s.stream_key = key
        elif not s.stream_key and (cid := f.get("copy_id", type=int)):
            src = db.get_stream(cid)
            s.stream_key = src.stream_key if src else ""
        s.youtube_video_id = parse_video_id(f.get("youtube_video", ""))
        s.unlist_after = bool(f.get("unlist_after"))
        errors = []
        if not s.name:
            errors.append("Name is required.")
        if not s.nas_path:
            errors.append("Choose a video file on the NAS.")
        if not s.rtmp_url.startswith(("rtmp://", "rtmps://")):
            errors.append("Server URL must start with rtmp:// or rtmps://")
        if not s.stream_key:
            errors.append("Stream key is required.")
        try:
            s.start_utc = to_utc(f.get("date", ""), f.get("time", ""))
            if not existing or existing.start_utc != s.start_utc or existing.status in D.FINAL:
                if s.start < datetime.now(timezone.utc):
                    errors.append("Start time is in the past. (Use 'Start now' to stream immediately.)")
        except ValueError:
            errors.append("Enter a valid date and time.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("stream_form.html", s=s, date_s=f.get("date", ""),
                                   time_s=f.get("time", ""), copy_id=f.get("copy_id")), 400

        # Check the file now so we know its length (for overlap checks).
        warning = None
        st = settings()
        if st.get("nas_url"):
            ok, msg, duration, size = check_source(service.nas_factory(st), s.nas_path,
                                                   encoder_settings(st))
            if ok:
                s.duration, s.file_size, s.message = duration, size, msg
            else:
                warning = msg
        other = find_overlap(db.list_streams(), s)
        if other:
            flash(f"This overlaps with '{other.name}' "
                  f"({fmt_local(other.start_utc, tzname())}, {_hms(other.duration)} long). "
                  "Streams can't overlap.", "error")
            return render_template("stream_form.html", s=s, date_s=f.get("date", ""),
                                   time_s=f.get("time", ""), copy_id=f.get("copy_id")), 400
        if s.status in D.FINAL or not existing:
            s.status, s.position, s.restarts = D.SCHEDULED, 0.0, 0
            s.started_at = s.ended_at = s.heartbeat = ""
        if warning:
            s.status, s.message = D.CHECK_FAILED, warning
            flash(f"Saved, but the file check failed: {warning}", "error")
        else:
            if s.status == D.CHECK_FAILED:
                s.status = D.READY
            flash("Stream saved.", "ok")
        db.save_stream(s)
        db.add_event(s.id, "info", "stream settings saved")
        return redirect(url_for("index"))

    @app.get("/streams/<int:sid>")
    def stream_detail(sid):
        s = db.get_stream(sid) or abort(404)
        return render_template("stream_detail.html", s=s, events=db.events(sid),
                               ffmpeg_log=_ffmpeg_log(service.data_dir, sid),
                               running=service.is_running(sid))

    @app.post("/streams/<int:sid>/delete")
    def stream_delete(sid):
        if service.is_running(sid):
            flash("Stop the stream before deleting it.", "error")
        else:
            db.delete_stream(sid)
            flash("Stream deleted.", "ok")
        return redirect(url_for("index"))

    @app.post("/streams/<int:sid>/start")
    def stream_start(sid):
        s = db.get_stream(sid) or abort(404)
        if service.is_running(sid):
            flash("Already running.", "error")
        else:
            db.add_event(sid, "info", "started manually from the web UI")
            service.launch(sid)
            flash(f"Starting '{s.name}' now.", "ok")
        return redirect(url_for("stream_detail", sid=sid))

    @app.post("/streams/<int:sid>/resume")
    def stream_resume(sid):
        s = db.get_stream(sid) or abort(404)
        if service.is_running(sid):
            flash("Already running.", "error")
        else:
            offset = max(0.0, s.position - 5)
            db.add_event(sid, "info", f"resumed manually at {_hms(offset)}")
            service.launch(sid, offset=offset)
            flash(f"Resuming '{s.name}' at {_hms(offset)}.", "ok")
        return redirect(url_for("stream_detail", sid=sid))

    @app.post("/streams/<int:sid>/stop")
    def stream_stop(sid):
        if service.stop_stream(sid):
            flash("Stopping stream.", "ok")
        else:
            flash("Stream is not running.", "error")
        return redirect(url_for("stream_detail", sid=sid))

    @app.post("/streams/<int:sid>/check")
    def stream_check(sid):
        s = db.get_stream(sid) or abort(404)
        if s.status not in D.WAITING:
            ok, msg, *_ = check_source(service.nas_factory(settings()), s.nas_path,
                                       encoder_settings(settings()))
        else:
            ok, msg = service.preflight(sid)
        flash(("File OK: " if ok else "File check failed: ") + msg, "ok" if ok else "error")
        return redirect(request.referrer or url_for("index"))

    # ------------------------------------------------------------ NAS browser
    @app.get("/api/nas/list")
    def api_nas_list():
        path = request.args.get("path") or settings().get("nas_root") or "/"
        try:
            nas = service.nas_factory(settings())
            entries = nas.list_folder(path)
        except NASError as e:
            return jsonify(error=str(e), path=path), 400
        parent = "/" if path.count("/") <= 1 else path.rsplit("/", 1)[0]
        return jsonify(path=path, parent=parent if path != "/" else None,
                       entries=[dict(e.as_dict(), size_h=human_size(e.size)) for e in entries])

    # ------------------------------------------------------------ settings
    @app.get("/settings")
    def settings_page():
        st = settings()
        yt_client = ""
        if st.get("youtube_client_json"):
            try:
                c = json.loads(st["youtube_client_json"])
                yt_client = (c.get("installed") or c.get("web") or {}).get("client_id", "")
            except ValueError:
                pass
        return render_template("settings.html", st=st, encoders=ENCODERS, presets=PRESETS,
                               timezones=sorted(available_timezones()), yt_client=yt_client,
                               redirect_uri=_redirect_uri())

    def _redirect_uri() -> str:
        return f"http://localhost:{app.config['PORT']}/oauth/callback"

    def _bool(name):
        return bool(request.form.get(name))

    @app.post("/settings/<section>")
    def settings_save(section):
        f = request.form
        st = settings()
        try:
            if section == "general":
                tz = f.get("timezone", "UTC")
                if tz not in available_timezones():
                    raise ValueError("unknown time zone")
                db.set_settings(timezone=tz,
                                preflight_minutes=int(f.get("preflight_minutes", 60)),
                                max_late_minutes=int(f.get("max_late_minutes", 15)),
                                default_rtmp_url=f.get("default_rtmp_url", "").strip()
                                or st["default_rtmp_url"])
                if f.get("new_password"):
                    if len(f["new_password"]) < 8:
                        raise ValueError("password must be at least 8 characters")
                    db.set_settings(admin_password_hash=generate_password_hash(f["new_password"]))
            elif section == "nas":
                vals = dict(nas_url=f.get("nas_url", "").strip(),
                            nas_user=f.get("nas_user", "").strip(),
                            nas_verify_tls=_bool("nas_verify_tls"),
                            nas_root=f.get("nas_root", "/").strip() or "/")
                if f.get("nas_password"):
                    vals["nas_password"] = f["nas_password"]
                db.set_settings(**vals)
                if f.get("action") == "test":
                    nas = service.nas_factory(settings())
                    shares = nas.list_shares()
                    nas.logout()
                    flash("NAS connection OK. Shared folders: "
                          + ", ".join(s.name for s in shares), "ok")
                    return redirect(url_for("settings_page") + "#nas")
            elif section == "encoder":
                db.set_settings(video_encoder=f.get("video_encoder", "libx264"),
                                preset=f.get("preset", "veryfast"),
                                height=int(f.get("height") or 1080),
                                video_bitrate_kbps=int(f.get("video_bitrate_kbps") or 8000),
                                audio_bitrate_kbps=int(f.get("audio_bitrate_kbps") or 160),
                                keyframe_seconds=float(f.get("keyframe_seconds") or 2))
            elif section == "email":
                vals = dict(smtp_host=f.get("smtp_host", "").strip(),
                            smtp_port=int(f.get("smtp_port") or 587),
                            smtp_security=f.get("smtp_security", "starttls"),
                            smtp_user=f.get("smtp_user", "").strip(),
                            smtp_from=f.get("smtp_from", "").strip(),
                            alert_to=f.get("alert_to", "").strip(),
                            notify_start=_bool("notify_start"),
                            notify_finish=_bool("notify_finish"))
                if f.get("smtp_password"):
                    vals["smtp_password"] = f["smtp_password"]
                db.set_settings(**vals)
                if f.get("action") == "test":
                    send_email(settings(), "Test email",
                               "Email alerts from late-stream are working.")
                    flash("Test email sent to " + settings()["alert_to"], "ok")
                    return redirect(url_for("settings_page") + "#email")
            else:
                abort(404)
            flash("Settings saved.", "ok")
        except (ValueError, NASError, EmailError) as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("settings_page") + f"#{section}")

    # ------------------------------------------------------------ YouTube OAuth
    @app.post("/youtube/connect")
    def youtube_connect():
        from .youtube import YouTubeError, parse_client_json, start_auth
        text = request.form.get("client_json", "").strip()
        upload = request.files.get("client_file")
        if upload and upload.filename:
            text = upload.read().decode("utf-8", "replace")
        text = text or settings().get("youtube_client_json", "")
        try:
            parse_client_json(text)
            url, pending = start_auth(text, _redirect_uri())
        except YouTubeError as e:
            flash(f"Error: {e}", "error")
            return redirect(url_for("settings_page") + "#youtube")
        db.set_settings(youtube_client_json=text, oauth_state=json.dumps(pending))
        return redirect(url)

    def _finish_oauth(callback: str):
        from .youtube import YouTubeError, build_client, finish_auth
        st = settings()
        try:
            pending = json.loads(st.get("oauth_state") or "{}")
            if not pending:
                raise YouTubeError("no connection in progress; press Connect again")
            token = finish_auth(st["youtube_client_json"], pending, callback)
            title = build_client(token).channel_title()
            db.set_settings(youtube_token_json=token, youtube_channel=title, oauth_state="")
            flash(f"YouTube connected: {title}", "ok")
        except YouTubeError as e:
            flash(f"Error: {e}", "error")
        return redirect(url_for("settings_page") + "#youtube")

    @app.get("/oauth/callback")
    def oauth_callback():
        # No login needed: the one-time OAuth state must match what we stored.
        return _finish_oauth(request.url)

    @app.post("/youtube/finish")
    def youtube_finish():
        return _finish_oauth(request.form.get("callback_url", ""))

    @app.post("/youtube/disconnect")
    def youtube_disconnect():
        db.set_settings(youtube_token_json="", youtube_channel="")
        flash("YouTube disconnected.", "ok")
        return redirect(url_for("settings_page") + "#youtube")

    return app


def _ffmpeg_log(data_dir: Path, sid: int, max_chars: int = 6000) -> str:
    from .nas import redact_url
    d = data_dir / "logs" / f"stream-{sid}"
    if not d.exists():
        return ""
    files = sorted(d.glob("ffmpeg-*.log"))
    if not files:
        return ""
    text = files[-1].read_text(errors="replace")
    return redact_url(text[-max_chars:])
