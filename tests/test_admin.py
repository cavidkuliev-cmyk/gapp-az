"""Админка: вход, настройки источников, скрытие новостей."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gapp import admin, collector, config, db
from gapp.collector import ingest

PASSWORD = "correct-horse-battery"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", PASSWORD)
    admin._failures.clear()
    conn = db.connect()
    conn.execute("INSERT INTO sources(id, name, site) VALUES(1, 'Источник', 'https://a.az')")
    conn.commit()
    ingest(conn, 1, {"title": "Центробанк Азербайджана объявил курс маната на завтра", "url": "https://a.az/1",
                     "summary": "", "image": "", "rubric": "ekonomika",
                     "published_at": datetime.now(timezone.utc) - timedelta(minutes=5)})
    conn.commit()
    from gapp.web import app
    yield conn, TestClient(app)
    conn.close()


def login(client):
    response = client.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    return client.get("/admin")


def token(page):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)


def test_admin_is_off_without_password(env, monkeypatch):
    conn, client = env
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "short")
    assert client.get("/admin").status_code == 404
    assert client.post("/admin/login", data={"password": "short"}).status_code == 404


def test_login_required_and_wrong_password(env):
    conn, client = env
    page = client.get("/admin")
    assert page.status_code == 200 and "Пароль" in page.text and "Источники" not in page.text
    assert client.post("/admin/login", data={"password": "nope"}).status_code == 401
    assert client.post("/admin/source/1/active", data={"value": "0"}).status_code == 401
    home = login(client)
    assert "Источники" in home.text and "Источник" in home.text
    assert home.headers["x-robots-tag"] == "noindex, nofollow"


def test_login_attempts_are_limited(env):
    conn, client = env
    for _ in range(5):
        assert client.post("/admin/login", data={"password": "bad"}).status_code == 401
    assert client.post("/admin/login", data={"password": PASSWORD}).status_code == 429


def test_post_needs_csrf_token(env):
    conn, client = env
    login(client)
    assert client.post("/admin/source/1/active", data={"value": "0"}).status_code == 403
    assert client.post("/admin/source/1/active", data={"value": "0", "csrf": "x"}).status_code == 403
    assert conn.execute("SELECT active FROM sources WHERE id = 1").fetchone()[0] == 1


def test_source_switches_survive_sync(env, tmp_path):
    conn, client = env
    csrf = token(login(client))
    client.post("/admin/source/1/active", data={"value": "0", "csrf": csrf})
    client.post("/admin/source/1/text", data={"value": "0", "csrf": csrf})
    row = conn.execute("SELECT active, show_text, admin_active, admin_show_text FROM sources").fetchone()
    assert tuple(row) == (0, 0, 0, 0)
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources:\n  - name: Источник\n    site: https://a.az\n    feeds: [https://a.az/rss]\n",
                       encoding="utf-8")
    collector.sync_sources(conn, sources)
    assert tuple(conn.execute("SELECT active, show_text FROM sources").fetchone()) == (0, 0)
    client.post("/admin/source/1/active", data={"value": "1", "csrf": csrf})
    assert conn.execute("SELECT active FROM sources WHERE id = 1").fetchone()[0] == 1


def test_hide_and_restore_story(env):
    conn, client = env
    story = conn.execute("SELECT id, slug FROM stories").fetchone()
    path = f"/novost/{story['slug']}-{story['id']}"
    assert client.get(path).status_code == 200 and "Центробанк" in client.get("/").text
    csrf = token(login(client))
    client.post(f"/admin/story/{story['id']}/hide", data={"csrf": csrf, "q": ""})
    assert client.get(path).status_code == 404
    for url in ("/", "/rubrika/ekonomika", "/rss.xml", "/istochnik/istochnik", "/sitemap.xml"):
        assert "Центробанк" not in client.get(url).text and f"-{story['id']}<" not in client.get(url).text
    assert "курс маната" not in client.get("/poisk", params={"q": "Центробанк"}).text
    assert "скрыта" in client.get("/admin").text
    client.post(f"/admin/story/{story['id']}/unhide", data={"csrf": csrf})
    assert client.get(path).status_code == 200


def test_robots_blocks_admin(env):
    conn, client = env
    assert "Disallow: /admin" in client.get("/robots.txt").text
