"""Раздел «Видео»: сбор популярных роликов и страницы."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from gapp import collector, config, db, videos


def api_item(video_id, hours_ago, views, title="Ролик", channel="Канал", duration="PT3M5S", live="none"):
    published = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": video_id, "snippet": {"title": title, "channelTitle": channel, "channelId": "UC1",
                                         "publishedAt": published, "liveBroadcastContent": live},
            "statistics": {"viewCount": str(views), "likeCount": "5"}, "contentDetails": {"duration": duration}}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "TESTKEY")
    c = db.connect()
    yield c
    c.close()


def api_client(pages, calls):
    def handler(request):
        assert "TESTKEY" not in str(request.url)               # ключ не должен попасть в адрес (и в журнал)
        assert request.headers["x-goog-api-key"] == "TESTKEY"
        calls.append(dict(request.url.params))
        category = request.url.params.get("videoCategoryId", "")
        if category == "20":
            return httpx.Response(400, json={"error": {"errors": [{"reason": "videoChartNotFound"}]}})
        return httpx.Response(200, json=pages.get(category, {"items": []}))
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_duration_and_items():
    assert videos.parse_duration("PT1H2M3S") == 3723 and videos.parse_duration("PT45S") == 45
    assert videos.parse_duration("P0D") == 0 and videos.parse_duration("junk") == 0
    payload = {"items": [api_item("abcdefghijk", 2, 1500, title="Заголовок <b>с тегом</b>"),
                         api_item("bad id", 2, 1), api_item("zzzzzzzzzzz", 1, 1, live="upcoming")]}
    parsed = videos.parse_videos(payload)
    assert len(parsed) == 1 and parsed[0]["title"] == "Заголовок с тегом" and parsed[0]["duration"] == 185


def test_no_key_means_no_requests(conn, monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "")
    calls = []
    stats = videos.collect_videos(conn, api_client({}, calls), datetime.now(timezone.utc))
    assert stats["videos"] == 0 and calls == []


def test_collect_saves_videos_and_respects_interval(conn):
    calls = []
    pages = {"": {"items": [api_item("aaaaaaaaaaa", 3, 9000), api_item("bbbbbbbbbbb", 30, 800000)]},
             "10": {"items": [api_item("ccccccccccc", 5, 120)]}}
    now = datetime.now(timezone.utc)
    stats = videos.collect_videos(conn, api_client(pages, calls), now)
    assert stats["videos"] == 3 and stats["video_errors"] == 0
    assert {c["regionCode"] for c in calls} == {"AZ"} and all(c["chart"] == "mostPopular" for c in calls)
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 3
    again = []
    assert videos.collect_videos(conn, api_client(pages, again), now + timedelta(minutes=10))["videos"] == 0
    assert again == []
    assert videos.collect_videos(conn, api_client(pages, again), now + timedelta(minutes=61))["videos"] == 3


def test_key_never_reaches_the_log(conn, caplog):
    calls = []
    with caplog.at_level("INFO"):  # на уровне INFO библиотека httpx пишет адрес каждого запроса
        videos.collect_videos(conn, api_client({"": {"items": []}}, calls), datetime.now(timezone.utc))
    assert calls and "HTTP Request" in caplog.text            # журнал httpx действительно перехвачен
    assert "TESTKEY" not in caplog.text and "key=" not in caplog.text


def test_bad_key_stops_without_leaking_it(conn, caplog):
    def handler(request):
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level("INFO"):
        stats = videos.collect_videos(conn, client, datetime.now(timezone.utc))
    assert stats["video_errors"] == 1 and "TESTKEY" not in caplog.text


def test_video_pages(conn):
    now = datetime.now(timezone.utc)
    videos.save_videos(conn, videos.parse_videos({"items": [
        api_item("aaaaaaaaaaa", 3, 9000, title="Свежий хит"),
        api_item("bbbbbbbbbbb", 30, 800000, title="Старый ролик"),
        api_item("ccccccccccc", 2, 1500000, title="Главный ролик", channel="Канал Z")]}), now)
    conn.commit()
    from gapp.web import app
    client = TestClient(app)
    page = client.get("/video")
    assert page.status_code == 200 and "Главный ролик" in page.text and "Свежий хит" in page.text
    assert "Старый ролик" not in page.text                       # старше 24 часов
    assert page.text.index("Главный ролик") < page.text.index("Свежий хит")   # по просмотрам
    assert "1,5 млн просмотров" in page.text and "https://i.ytimg.com/vi/aaaaaaaaaaa/mqdefault.jpg" in page.text
    assert 'href="/video"' in client.get("/").text and 'href="/video"' in client.get("/az").text
    item = client.get("/video/aaaaaaaaaaa")
    assert item.status_code == 200 and "youtube-nocookie.com/embed/aaaaaaaaaaa" in item.text
    assert 'content="noindex, follow"' in item.text
    assert client.get("/video/nothing12345").status_code == 404 and client.get("/video/x").status_code == 404
    assert "/video</loc>" in client.get("/sitemap.xml").text


def test_video_page_empty(conn):
    from gapp.web import app
    assert "Пока нет данных" in TestClient(app).get("/video").text
