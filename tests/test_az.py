"""Азербайджанская версия: язык, склейка сюжетов, страницы /az."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gapp import collector, config, db
from gapp.collector import entry_to_item, ingest
from gapp.textutil import detect_rubric, slugify, text_lang, title_tokens


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    c.execute("INSERT INTO sources(id, name, site) VALUES(1, 'Источник', 'https://a.az')")
    c.execute("INSERT INTO sources(id, name, site, lang, title) "
              "VALUES(2, 'Mənbə 1 (az)', 'https://b.az', 'az', 'Mənbə 1')")
    c.execute("INSERT INTO sources(id, name, site, lang) VALUES(3, 'Mənbə 2', 'https://c.az', 'az')")
    c.commit()
    yield c
    c.close()


def item(title, url):
    return {
        "title": title, "url": url, "summary": "", "image": "", "rubric": "ekonomika",
        "published_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }


def test_text_lang():
    assert text_lang("В Баку открыли новую дорогу") == "ru"
    assert text_lang("Bakıda yeni yol açıldı") == "az"
    assert text_lang("New road opened in Baku") == ""
    assert text_lang("Bakıda в Баку şəhər открыли дорогу") == ""


def test_az_tokens_slug_rubric():
    assert title_tokens("İLHAM ƏLİYEV Şuşada çıxış edib") == {"ilham", "əliyev", "şuşada", "çıxış"}
    assert slugify("Mərkəzi Bank məzənnəni açıqladı") == "merkezi-bank-mezenneni-achiqladi"
    assert detect_rubric(["İdman"]) == "sport"
    assert detect_rubric([], "https://x.az/iqtisadiyyat/123") == "ekonomika"


def test_entry_filter_by_source_language():
    now = datetime.now(timezone.utc)
    az = {"title": "Bakıda yeni yol açıldı", "link": "https://b.az/1"}
    ru = {"title": "В Баку открыли новую дорогу", "link": "https://b.az/2"}
    assert entry_to_item(az, now, "az")["title"] == "Bakıda yeni yol açıldı"
    assert entry_to_item(ru, now, "az") is None
    assert entry_to_item(az, now) is None


def test_az_pages_are_separate_from_russian(conn):
    ingest(conn, 1, item("Центробанк Азербайджана объявил курс маната на завтра", "https://a.az/1"))
    assert ingest(conn, 2, item("Mərkəzi Bank sabah üçün manatın məzənnəsini açıqladı", "https://b.az/1")) == "new"
    assert ingest(conn, 3, item("Mərkəzi Bank manatın sabah üçün rəsmi məzənnəsini açıqladı", "https://c.az/1")) == "joined"
    conn.commit()
    from gapp.web import app

    client = TestClient(app)
    home = client.get("/az")
    assert home.status_code == 200 and '<html lang="az">' in home.text
    assert "Əsas xəbərlər" in home.text and "Mərkəzi Bank" in home.text
    assert "daha 1 mənbə" in home.text and "Mənbə 1" in home.text and "(az)" not in home.text
    assert "Центробанк" not in home.text and "Источник" not in home.text
    assert 'hreflang="ru"' in home.text and 'hreflang="az"' in home.text

    ru_home = client.get("/").text
    assert "Центробанк" in ru_home and "Mərkəzi" not in ru_home and "Mənbə" not in ru_home
    assert 'href="/az"' in ru_home  # переключатель языка

    story = conn.execute("SELECT id, slug FROM stories WHERE lang = 'az'").fetchone()
    path = f"/az/xeber/{story['slug']}-{story['id']}"
    page = client.get(path)
    assert page.status_code == 200 and "Bu barədə kim yazır" in page.text
    assert 'content="index, follow' in page.text
    moved = client.get(f"/novost/{story['slug']}-{story['id']}", follow_redirects=False)
    assert moved.status_code == 301 and moved.headers["location"] == path

    assert client.get("/az/rubrika/ekonomika").status_code == 200
    assert "Mərkəzi" in client.get("/az/axtar", params={"q": "manatın"}).text
    assert "Mərkəzi" not in client.get("/poisk", params={"q": "manatın"}).text
    assert client.get("/az/haqqinda").status_code == 200
    assert "Mərkəzi" in client.get("/az/rss.xml").text and "Mərkəzi" not in client.get("/rss.xml").text
    sitemap = client.get("/sitemap.xml").text
    assert path in sitemap and "/az/rubrika/sport" in sitemap
    assert "<news:language>az</news:language>" in client.get("/news-sitemap.xml").text


def test_sync_sources_reads_language(tmp_path):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    path = tmp_path / "s.yaml"
    path.write_text(
        "sources:\n  - {name: A, site: 'https://a.az', feeds: ['https://a.az/rss']}\n"
        "  - {name: A (az), title: A, lang: az, site: 'https://a.az/az', feeds: ['https://a.az/az/rss']}\n",
        encoding="utf-8",
    )
    collector.sync_sources(conn, path)
    rows = conn.execute("SELECT name, lang, title FROM sources ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("A", "ru", ""), ("A (az)", "az", "A")]
