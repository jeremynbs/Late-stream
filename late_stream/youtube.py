"""Optional YouTube Data API v3 integration.

The stream itself only needs the RTMP URL + stream key. The API is used only
for things a stream key can't do:

* find the broadcast (video) that the stream key is bound to,
* make sure it goes live if "Auto-start" was left off in YouTube Studio,
* end it, and switch it from public to **unlisted** after the stream.

Quota cost per stream is ~60 units of the free 10,000/day (see README).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
QUOTA = {
    "liveStreams.list": 1, "liveBroadcasts.list": 1, "liveBroadcasts.transition": 50,
    "videos.list": 1, "videos.update": 50, "channels.list": 1,
}
# Fields videos.update(part=status) accepts. Anything we don't send is reset,
# so we copy the current values back.
WRITABLE_STATUS = ("privacyStatus", "embeddable", "license", "publicStatsViewable",
                   "selfDeclaredMadeForKids", "containsSyntheticMedia")

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


class YouTubeError(RuntimeError):
    pass


# ---------------------------------------------------------------- OAuth
def parse_client_json(text: str) -> dict:
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        raise YouTubeError("OAuth client file is not valid JSON") from e
    if not ({"installed", "web"} & set(cfg)):
        raise YouTubeError("not an OAuth client file (expected an 'installed' or 'web' key); "
                           "download it from Google Cloud Console > Credentials")
    return cfg


def start_auth(client_json: str, redirect_uri: str) -> tuple[str, dict]:
    """Return (consent URL, pending state to keep until the callback)."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(parse_client_json(client_json), SCOPES,
                                   redirect_uri=redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent",
                                        include_granted_scopes="true")
    return url, {"state": state, "verifier": flow.code_verifier, "redirect_uri": redirect_uri}


def finish_auth(client_json: str, pending: dict, callback_url_or_code: str) -> str:
    """Exchange the code (or the full redirected URL the user pasted) for a
    token. Returns the token JSON to store."""
    from google_auth_oauthlib.flow import Flow
    text = callback_url_or_code.strip()
    code = text
    if "code=" in text:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        if q.get("state", [pending.get("state")])[0] != pending.get("state"):
            raise YouTubeError("OAuth state mismatch; start the connection again")
        if "error" in q:
            raise YouTubeError(f"Google returned an error: {q['error'][0]}")
        code = q["code"][0]
    flow = Flow.from_client_config(parse_client_json(client_json), SCOPES,
                                   redirect_uri=pending["redirect_uri"],
                                   code_verifier=pending.get("verifier"),
                                   autogenerate_code_verifier=False)
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        raise YouTubeError(f"could not get token from Google: {e}") from e
    if not flow.credentials.refresh_token:
        raise YouTubeError("Google did not return a refresh token; remove the app's access at "
                           "myaccount.google.com/permissions and connect again")
    return flow.credentials.to_json()


def build_client(token_json: str, on_refresh=None) -> "YouTube":
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not token_json:
        raise YouTubeError("YouTube is not connected")
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise YouTubeError(
                f"YouTube token refresh failed ({e}). If the Google Cloud OAuth app is in "
                "'Testing' mode tokens expire after 7 days - publish it, then reconnect.") from e
        if on_refresh:
            on_refresh(creds.to_json())
    return YouTube(build("youtube", "v3", credentials=creds, cache_discovery=False))


# ---------------------------------------------------------------- API calls
class YouTube:
    def __init__(self, service):
        self.yt = service
        self.units_used = 0

    def _exec(self, method: str, request):
        from googleapiclient.errors import HttpError
        self.units_used += QUOTA.get(method, 1)
        delay = 2.0
        for attempt in range(4):
            try:
                return request.execute()
            except HttpError as e:
                status = getattr(e.resp, "status", 0)
                if status in (500, 502, 503, 504) and attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise YouTubeError(f"{method} failed: {_reason(e)}") from e

    def channel_title(self) -> str:
        r = self._exec("channels.list", self.yt.channels().list(part="snippet", mine=True))
        items = r.get("items", [])
        return items[0]["snippet"]["title"] if items else "(no channel)"

    def stream_id_for_key(self, stream_key: str) -> str | None:
        page = None
        while True:
            r = self._exec("liveStreams.list", self.yt.liveStreams().list(
                part="id,cdn", mine=True, maxResults=50, pageToken=page))
            for item in r.get("items", []):
                if item.get("cdn", {}).get("ingestionInfo", {}).get("streamName") == stream_key:
                    return item["id"]
            page = r.get("nextPageToken")
            if not page:
                return None

    def find_broadcast(self, stream_key: str,
                       statuses=("active", "upcoming")) -> str | None:
        """Video id of the active (or else next upcoming) broadcast bound to
        this stream key."""
        sid = self.stream_id_for_key(stream_key)
        if not sid:
            return None
        for status in statuses:
            r = self._exec("liveBroadcasts.list", self.yt.liveBroadcasts().list(
                part="id,snippet,contentDetails", broadcastStatus=status,
                broadcastType="all", maxResults=50))
            matches = [b for b in r.get("items", [])
                       if b.get("contentDetails", {}).get("boundStreamId") == sid]
            if matches:
                # upcoming: the soonest; completed: the most recent
                matches.sort(key=lambda b: b["snippet"].get("scheduledStartTime", ""),
                             reverse=(status == "completed"))
                return matches[0]["id"]
        return None

    def broadcast(self, video_id: str) -> dict:
        r = self._exec("liveBroadcasts.list", self.yt.liveBroadcasts().list(
            part="id,status,contentDetails", id=video_id))
        items = r.get("items", [])
        if not items:
            raise YouTubeError(f"broadcast {video_id} not found on this channel")
        return items[0]

    def lifecycle(self, video_id: str) -> str:
        return self.broadcast(video_id)["status"]["lifeCycleStatus"]

    def transition(self, video_id: str, to: str) -> None:
        self._exec("liveBroadcasts.transition", self.yt.liveBroadcasts().transition(
            part="status", id=video_id, broadcastStatus=to))
        log.info("broadcast %s -> %s", video_id, to)

    def ensure_live(self, video_id: str, timeout: float = 120) -> str:
        """If Auto-start is off in Studio, move the broadcast to live."""
        b = self.broadcast(video_id)
        status = b["status"]["lifeCycleStatus"]
        if status in ("live", "liveStarting"):
            return status
        if status == "ready":
            monitor = b.get("contentDetails", {}).get("monitorStream", {}) \
                .get("enableMonitorStream", False)
            if monitor:
                self.transition(video_id, "testing")
                status = self._wait(video_id, {"testing"}, timeout)
            else:
                self.transition(video_id, "live")
                return self._wait(video_id, {"live"}, timeout)
        if status == "testing":
            self.transition(video_id, "live")
            return self._wait(video_id, {"live"}, timeout)
        return status

    def complete(self, video_id: str) -> str:
        status = self.lifecycle(video_id)
        if status in ("live", "liveStarting", "testing"):
            self.transition(video_id, "complete")
            status = "complete"
        return status

    def wait_complete(self, video_id: str, timeout: float = 90) -> str:
        """With Auto-stop on, YouTube ends the broadcast by itself shortly
        after video stops. Wait a bit for that, then end it ourselves."""
        status = self._wait(video_id, {"complete", "revoked"}, timeout)
        if status not in ("complete", "revoked"):
            status = self.complete(video_id)
        return status

    def set_privacy(self, video_id: str, privacy: str) -> None:
        r = self._exec("videos.list", self.yt.videos().list(part="status", id=video_id))
        items = r.get("items", [])
        if not items:
            raise YouTubeError(f"video {video_id} not found")
        current = items[0]["status"]
        if current.get("privacyStatus") == privacy:
            return
        status = {k: current[k] for k in WRITABLE_STATUS if k in current}
        status["privacyStatus"] = privacy
        self._exec("videos.update", self.yt.videos().update(
            part="status", body={"id": video_id, "status": status}))
        log.info("video %s set to %s", video_id, privacy)

    def _wait(self, video_id: str, wanted: set[str], timeout: float, poll: float = 10) -> str:
        deadline = time.monotonic() + timeout
        status = ""
        while True:
            status = self.lifecycle(video_id)
            if status in wanted or time.monotonic() >= deadline:
                return status
            time.sleep(poll)


def parse_video_id(text: str) -> str:
    """Accept a bare id or any YouTube/Studio URL form."""
    t = (text or "").strip()
    if not t:
        return ""
    if "://" not in t and "/" not in t:
        return t
    u = urllib.parse.urlparse(t if "://" in t else "https://" + t)
    q = urllib.parse.parse_qs(u.query)
    if "v" in q:
        return q["v"][0]
    parts = [p for p in u.path.split("/") if p]
    for marker in ("live", "video", "embed", "shorts"):
        if marker in parts and parts.index(marker) + 1 < len(parts):
            return parts[parts.index(marker) + 1]
    if u.netloc.endswith("youtu.be") and parts:
        return parts[0]
    return parts[-1] if parts else ""


def _reason(e) -> str:
    try:
        data = json.loads(e.content.decode())
        err = data["error"]
        reasons = ",".join(x.get("reason", "") for x in err.get("errors", []))
        return f"{err.get('message', '')} ({reasons})"
    except Exception:
        return str(e)
