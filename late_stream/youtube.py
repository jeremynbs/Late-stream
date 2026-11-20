"""Thin wrapper around the YouTube Data API v3 Live Streaming endpoints.

Quota cost of each call (default project quota is 10,000 units/day) is noted
next to every method; see README "API cost".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube"]

QUOTA = {
    "liveBroadcasts.insert": 50,
    "liveBroadcasts.update": 50,
    "liveBroadcasts.bind": 50,
    "liveBroadcasts.transition": 50,
    "liveBroadcasts.delete": 50,
    "liveBroadcasts.list": 1,
    "liveStreams.insert": 50,
    "liveStreams.update": 50,
    "liveStreams.delete": 50,
    "liveStreams.list": 1,
    "thumbnails.set": 50,
    "videos.update": 50,
}


class YouTubeError(RuntimeError):
    pass


def load_credentials(client_secrets: Path, token_file: Path, interactive: bool = True):
    """OAuth 2.0 'installed app' flow. First run opens a browser (or prints a
    URL); the refresh token is cached in ``token_file`` for unattended use."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not interactive:
            raise YouTubeError(
                f"No valid token in {token_file}. Run `late-stream auth` first.")
        if not client_secrets.exists():
            raise YouTubeError(f"OAuth client file not found: {client_secrets}")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    try:
        token_file.chmod(0o600)
    except OSError:
        pass
    return creds


def build_service(creds):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


@dataclass
class IngestInfo:
    stream_id: str
    url: str  # full RTMPS URL including stream key - keep secret


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class YouTubeLive:
    def __init__(self, service):
        self.yt = service
        self.units_used = 0

    def _exec(self, method: str, request):
        from googleapiclient.errors import HttpError
        self.units_used += QUOTA.get(method, 1)
        delay = 2.0
        for attempt in range(5):
            try:
                return request.execute()
            except HttpError as e:
                status = getattr(e.resp, "status", 0)
                if status in (500, 502, 503, 504) and attempt < 4:
                    log.warning("%s -> HTTP %s, retrying in %.0fs", method, status, delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise YouTubeError(f"{method} failed: {e}") from e
        raise YouTubeError(f"{method} failed after retries")

    # ---------------- streams (ingest points) ----------------
    def get_or_create_stream(self, title: str) -> IngestInfo:
        """Reusable ingest stream identified by title. Reusing it means the
        stream key never changes and saves 50 units per broadcast."""
        page = None
        while True:
            resp = self._exec("liveStreams.list", self.yt.liveStreams().list(
                part="id,snippet,cdn,status", mine=True, maxResults=50, pageToken=page))
            for item in resp.get("items", []):
                if item["snippet"]["title"] == title:
                    return self._ingest(item)
            page = resp.get("nextPageToken")
            if not page:
                break
        body = {
            "snippet": {"title": title},
            "cdn": {"ingestionType": "rtmp", "resolution": "variable",
                    "frameRate": "variable"},
            "contentDetails": {"isReusable": True},
        }
        item = self._exec("liveStreams.insert", self.yt.liveStreams().insert(
            part="snippet,cdn,contentDetails,status", body=body))
        log.info("created live stream '%s' (%s)", title, item["id"])
        return self._ingest(item)

    @staticmethod
    def _ingest(item: dict) -> IngestInfo:
        ing = item["cdn"]["ingestionInfo"]
        base = ing.get("rtmpsIngestionAddress") or ing["ingestionAddress"]
        return IngestInfo(stream_id=item["id"], url=f"{base}/{ing['streamName']}")

    def stream_status(self, stream_id: str) -> tuple[str, str]:
        resp = self._exec("liveStreams.list", self.yt.liveStreams().list(
            part="status", id=stream_id))
        items = resp.get("items", [])
        if not items:
            raise YouTubeError(f"stream {stream_id} not found")
        st = items[0]["status"]
        return st.get("streamStatus", ""), st.get("healthStatus", {}).get("status", "")

    # ---------------- broadcasts (the event viewers see) ----------------
    def create_broadcast(self, *, title: str, description: str, start: datetime,
                         privacy: str = "unlisted", auto_start: bool = True,
                         auto_stop: bool = True, latency: str = "normal",
                         enable_dvr: bool = True, made_for_kids: bool = False,
                         category_id: str | None = None) -> dict:
        body = {
            "snippet": {"title": title[:100], "description": description[:5000],
                        "scheduledStartTime": _iso(start)},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids},
            "contentDetails": {
                "enableAutoStart": auto_start,
                "enableAutoStop": auto_stop,
                "enableDvr": enable_dvr,
                "latencyPreference": latency,
                # Without monitor stream we can go straight ready -> live.
                "monitorStream": {"enableMonitorStream": False},
            },
        }
        b = self._exec("liveBroadcasts.insert", self.yt.liveBroadcasts().insert(
            part="snippet,status,contentDetails", body=body))
        log.info("created broadcast %s https://youtu.be/%s", b["id"], b["id"])
        if category_id:
            # Category lives on the underlying video resource.
            self._exec("videos.update", self.yt.videos().update(
                part="snippet", body={"id": b["id"], "snippet": {
                    "title": title[:100], "description": description[:5000],
                    "categoryId": category_id}}))
        return b

    def bind(self, broadcast_id: str, stream_id: str) -> None:
        self._exec("liveBroadcasts.bind", self.yt.liveBroadcasts().bind(
            part="id,contentDetails", id=broadcast_id, streamId=stream_id))

    def broadcast_status(self, broadcast_id: str) -> str:
        resp = self._exec("liveBroadcasts.list", self.yt.liveBroadcasts().list(
            part="status", id=broadcast_id))
        items = resp.get("items", [])
        if not items:
            raise YouTubeError(f"broadcast {broadcast_id} not found")
        return items[0]["status"]["lifeCycleStatus"]

    def transition(self, broadcast_id: str, status: str) -> None:
        self._exec("liveBroadcasts.transition", self.yt.liveBroadcasts().transition(
            part="status", id=broadcast_id, broadcastStatus=status))
        log.info("broadcast %s -> %s", broadcast_id, status)

    def set_thumbnail(self, broadcast_id: str, image: Path) -> None:
        from googleapiclient.http import MediaFileUpload
        self._exec("thumbnails.set", self.yt.thumbnails().set(
            videoId=broadcast_id, media_body=MediaFileUpload(str(image))))

    # ---------------- helpers ----------------
    def wait_stream_active(self, stream_id: str, timeout: float = 120,
                           poll: float = 5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, health = self.stream_status(stream_id)
            log.info("ingest stream status=%s health=%s", status, health)
            if status == "active":
                return True
            time.sleep(poll)
        return False

    def wait_broadcast(self, broadcast_id: str, wanted: set[str], timeout: float = 120,
                       poll: float = 5) -> str:
        deadline = time.monotonic() + timeout
        status = ""
        while time.monotonic() < deadline:
            status = self.broadcast_status(broadcast_id)
            if status in wanted:
                return status
            time.sleep(poll)
        return status
