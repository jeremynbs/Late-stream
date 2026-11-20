import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from late_stream.db import DB
from late_stream.service import Service
from late_stream.web import create_app


@pytest.fixture
def app_client(tmp_path, nas):
    db = DB(tmp_path / "db.sqlite")
    svc = Service(db, tmp_path)
    app = create_app(db, svc)
    app.config["TESTING"] = True
    c = app.test_client()
    return c, db, svc


def csrf(c, path="/"):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def login(c):
    assert c.get("/").status_code == 302
    r = c.post("/setup", data={"password": "secret123", "password2": "secret123",
                               "timezone": "Asia/Jakarta"})
    assert r.status_code == 302


def local_parts(minutes_from_now, tz="Asia/Jakarta"):
    t = datetime.now(ZoneInfo(tz)) + timedelta(minutes=minutes_from_now)
    return t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S")


def test_requires_login(app_client):
    c, db, _ = app_client
    assert c.get("/").headers["Location"].endswith("/setup")
    login(c)
    assert c.get("/").status_code == 200
    c2 = c.application.test_client()
    assert "/login" in c2.get("/settings").headers["Location"]
    assert c2.get("/api/streams").status_code == 401
    r = c2.post("/login", data={"password": "wrong"})
    assert b"Wrong password" in r.data


def test_csrf_enforced(app_client):
    c, _, _ = app_client
    login(c)
    assert c.post("/settings/general", data={"timezone": "UTC"}).status_code == 400


def test_settings_nas_browse_and_create_stream(app_client, nas):
    c, db, _ = app_client
    login(c)
    tok = csrf(c, "/settings")
    r = c.post("/settings/nas", data={"csrf": tok, "nas_url": nas.url, "nas_user": "streamer",
                                      "nas_password": "pw", "action": "test"},
               follow_redirects=True)
    assert b"NAS connection OK" in r.data
    # password kept when the field is left empty
    c.post("/settings/nas", data={"csrf": tok, "nas_url": nas.url, "nas_user": "streamer"})
    assert db.get_setting("nas_password") == "pw"

    j = c.get("/api/nas/list?path=/video/recordings").get_json()
    assert [e["name"] for e in j["entries"]] == ["session1.mov", "session2.mp4"]
    assert j["parent"] == "/video"

    d, t = local_parts(120)
    form = {"csrf": tok, "name": "Session 1", "nas_path": "/video/recordings/session1.mov",
            "date": d, "time": t, "rtmp_url": "rtmps://a.rtmps.youtube.com/live2",
            "stream_key": "abcd-efgh-ijkl-mnop", "youtube_video": "", "unlist_after": "on"}
    r = c.post("/streams", data=form, follow_redirects=True)
    assert b"Stream saved" in r.data, r.data
    s = db.list_streams()[0]
    assert s.status == "scheduled" and s.duration > 5 and s.stream_key == "abcd-efgh-ijkl-mnop"
    # Asia/Jakarta is UTC+7
    assert s.start.utcoffset().total_seconds() == 0
    assert s.start.astimezone(ZoneInfo("Asia/Jakarta")).strftime("%H:%M:%S") == t

    api = c.get("/api/streams").get_json()[0]
    assert "abcd-efgh" not in str(api) and api["stream_key"].endswith("mnop")

    # overlapping stream is rejected (file is 6 s; +2 min margin)
    form2 = dict(form, name="Overlap", nas_path="/video/recordings/session2.mp4")
    r = c.post("/streams", data=form2)
    assert r.status_code == 400 and b"overlaps" in r.data

    # non-overlapping is fine; editing without re-entering the key keeps it
    d2, t2 = local_parts(300)
    c.post("/streams", data=dict(form2, date=d2, time=t2))
    assert len(db.list_streams()) == 2
    sid = db.list_streams()[1].id
    c.post(f"/streams/{sid}", data=dict(form2, date=d2, time=t2, stream_key="", name="Renamed"))
    assert db.get_stream(sid).name == "Renamed"
    assert db.get_stream(sid).stream_key == "abcd-efgh-ijkl-mnop"


def test_past_time_rejected(app_client, nas):
    c, db, _ = app_client
    login(c)
    tok = csrf(c)
    d, t = local_parts(-10)
    r = c.post("/streams", data={"csrf": tok, "name": "x", "nas_path": "/a.mov", "date": d,
                                 "time": t, "rtmp_url": "rtmp://x/live2", "stream_key": "k"})
    assert r.status_code == 400 and b"in the past" in r.data


def test_pages_render(app_client, nas):
    c, db, _ = app_client
    login(c)
    for p in ["/", "/settings", "/streams/new"]:
        assert c.get(p).status_code == 200, p
