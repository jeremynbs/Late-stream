from late_stream.youtube import YouTube, parse_video_id


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class Res:
    def __init__(self, svc, name):
        self.svc, self.name = svc, name

    def __getattr__(self, method):
        def call(**kw):
            self.svc.log.append((self.name, method, kw))
            return Req(lambda: self.svc.handle(self.name, method, kw))
        return call


class FakeService:
    def __init__(self):
        self.log = []
        self.video_status = {"privacyStatus": "public", "uploadStatus": "processed",
                             "embeddable": False, "license": "youtube",
                             "publicStatsViewable": True, "selfDeclaredMadeForKids": False}
        self.lifecycle = "ready"
        self.monitor = True

    def liveStreams(self):
        return Res(self, "liveStreams")

    def liveBroadcasts(self):
        return Res(self, "liveBroadcasts")

    def videos(self):
        return Res(self, "videos")

    def handle(self, res, method, kw):
        if (res, method) == ("liveStreams", "list"):
            return {"items": [
                {"id": "S-other", "cdn": {"ingestionInfo": {"streamName": "zzzz"}}},
                {"id": "S1", "cdn": {"ingestionInfo": {"streamName": "my-key"}}}]}
        if (res, method) == ("liveBroadcasts", "list"):
            if "id" in kw:
                return {"items": [{"id": kw["id"], "status": {"lifeCycleStatus": self.lifecycle},
                                   "contentDetails": {"monitorStream": {"enableMonitorStream": self.monitor}}}]}
            if kw["broadcastStatus"] == "upcoming":
                return {"items": [
                    {"id": "B-late", "snippet": {"scheduledStartTime": "2026-11-22T00:00:00Z"},
                     "contentDetails": {"boundStreamId": "S1"}},
                    {"id": "B-soon", "snippet": {"scheduledStartTime": "2026-11-21T00:00:00Z"},
                     "contentDetails": {"boundStreamId": "S1"}},
                    {"id": "B-x", "snippet": {}, "contentDetails": {"boundStreamId": "S-other"}}]}
            return {"items": []}
        if (res, method) == ("liveBroadcasts", "transition"):
            self.lifecycle = kw["broadcastStatus"]
            return {}
        if (res, method) == ("videos", "list"):
            return {"items": [{"id": kw["id"], "status": dict(self.video_status)}]}
        if (res, method) == ("videos", "update"):
            self.video_status.update(kw["body"]["status"])
            return kw["body"]
        raise AssertionError((res, method))


def test_find_broadcast_by_key():
    yt = YouTube(FakeService())
    assert yt.find_broadcast("my-key") == "B-soon"
    assert yt.find_broadcast("unknown") is None


def test_set_unlisted_keeps_other_status_fields():
    svc = FakeService()
    YouTube(svc).set_privacy("B1", "unlisted")
    update = [l for l in svc.log if l[:2] == ("videos", "update")][0][2]
    sent = update["body"]["status"]
    assert sent["privacyStatus"] == "unlisted"
    assert sent["embeddable"] is False and sent["license"] == "youtube"
    assert "uploadStatus" not in sent          # read-only field not sent back
    assert update["part"] == "status"


def test_ensure_live_through_testing_when_monitor_enabled():
    svc = FakeService()
    yt = YouTube(svc)
    yt._wait = lambda vid, wanted, timeout, poll=0: svc.lifecycle
    assert yt.ensure_live("B1") == "live"
    transitions = [l[2]["broadcastStatus"] for l in svc.log if l[1] == "transition"]
    assert transitions == ["testing", "live"]


def test_quota_used_per_stream_is_small():
    svc = FakeService()
    yt = YouTube(svc)
    yt.find_broadcast("my-key")
    svc.lifecycle = "complete"
    yt.wait_complete("B-soon", timeout=0)
    yt.set_privacy("B-soon", "unlisted")
    assert yt.units_used <= 110


def test_parse_video_id():
    for t in ["abcDEF12345", "https://youtu.be/abcDEF12345", "https://www.youtube.com/watch?v=abcDEF12345",
              "https://youtube.com/live/abcDEF12345?feature=share",
              "https://studio.youtube.com/video/abcDEF12345/livestreaming"]:
        assert parse_video_id(t) == "abcDEF12345", t
    assert parse_video_id("") == ""
