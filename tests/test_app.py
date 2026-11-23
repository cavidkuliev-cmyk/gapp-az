from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gapp import config, db
from gapp.collector import entry_to_item, ingest


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    c.execute("INSERT INTO sources(id, name, site) VALUES(1, 'Источник 1', 'https://a.az')")
    c.execute("INSERT INTO sources(id, name, site) VALUES(2, 'Источник 2', 'https://b.az')")
    c.commit()
    yield c
    c.close()


def item(title, url, minutes_ago=5):
    return {
        "title": title, "url": url, "summary": "Короткий анонс новости.", "image": "",
        "rubric": "ekonomika",
        "published_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    }


def test_exact_duplicates_are_rejected(conn):
    first = item("Центробанк объявил курс маната на завтра", "https://a.az/1")
    assert ingest(conn, 1, first) == "new"
    assert ingest(conn, 1, first) == "duplicate"  # та же ссылка
    same_title = item("Центробанк объявил курс маната на завтра!", "https://a.az/1-copy")
    assert ingest(conn, 1, same_title) == "duplicate"  # тот же заголовок у того же источника
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1


def test_similar_news_join_one_story(conn):
    assert ingest(conn, 1, item("Центробанк Азербайджана объявил курс маната на завтра", "https://a.az/1")) == "new"
    assert ingest(conn, 2, item("Центробанк объявил официальный курс маната на завтра", "https://b.az/9")) == "joined"
    assert ingest(conn, 2, item("Сборная Азербайджана назвала состав на матч", "https://b.az/10")) == "new"
    stories = conn.execute("SELECT source_count, article_count FROM stories ORDER BY id").fetchall()
    assert [tuple(s) for s in stories] == [(2, 2), (1, 1)]


def test_old_news_do_not_join(conn):
    ingest(conn, 1, item("Центробанк объявил курс маната на завтра", "https://a.az/1", minutes_ago=60 * 72))
    assert ingest(conn, 2, item("Центробанк объявил курс маната на завтра", "https://b.az/2")) == "new"


def test_entry_filter_keeps_only_russian():
    now = datetime.now(timezone.utc)
    assert entry_to_item({"title": "Bakıda yeni yol açıldı", "link": "https://a.az/1"}, now) is None
    assert entry_to_item({"title": "В Баку открыли новую дорогу", "link": "ftp://a.az/1"}, now) is None
    good = entry_to_item({"title": "В Баку <b>открыли</b> новую дорогу", "link": "https://a.az/1?utm_source=x"}, now)
    assert good["title"] == "В Баку открыли новую дорогу" and good["url"] == "https://a.az/1"


def test_pages(conn):
    ingest(conn, 1, item("Центробанк Азербайджана объявил курс маната на завтра", "https://a.az/1"))
    ingest(conn, 2, item("Центробанк объявил официальный курс маната на завтра", "https://b.az/9"))
    ingest(conn, 2, item("Сборная <Азербайджана> назвала состав на матч", "https://b.az/10"))
    conn.commit()
    from gapp.web import app

    client = TestClient(app)
    home = client.get("/")
    assert home.status_code == 200
    assert "Главные новости" in home.text and "ещё 1 источник" in home.text
    assert "<Азербайджана>" not in home.text  # разметка из заголовков экранируется

    multi = client.get("/novost/tsentrobank-azerbaydzhana-obyavil-kurs-manata-na-zavtra-1")
    assert multi.status_code == 200
    assert 'content="index, follow' in multi.text and "https://b.az/9" in multi.text
    assert "NewsArticle" in multi.text

    single = conn.execute("SELECT id, slug FROM stories WHERE source_count = 1").fetchone()
    page = client.get(f"/novost/{single['slug']}-{single['id']}")
    assert 'content="noindex, follow"' in page.text

    wrong = client.get("/novost/staryi-adres-1", follow_redirects=False)
    assert wrong.status_code == 301
    assert client.get("/novost/net-takoy-999").status_code == 404
    assert client.get("/rubrika/ekonomika").status_code == 200
    assert client.get("/rubrika/unknown").status_code == 404
    assert "Центробанк" in client.get("/poisk", params={"q": "маната"}).text
    assert client.get("/poisk", params={"q": "100%"}).status_code == 200

    sitemap = client.get("/sitemap.xml").text
    assert "/novost/tsentrobank" in sitemap and f"-{single['id']}</loc>" not in sitemap
    assert client.get("/news-sitemap.xml").status_code == 200
    assert client.get("/rss.xml").status_code == 200
    assert "Sitemap:" in client.get("/robots.txt").text
    assert client.get("/healthz").text == "ok"
    assert client.get("/o-proekte").status_code == 200


def test_sync_sources_drops_disabled_feeds(tmp_path):
    import sqlite3

    from gapp import collector

    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    path = tmp_path / "s.yaml"
    path.write_text(
        "sources:\n  - {name: A, site: 'https://a.az', feeds: ['https://a.az/rss']}\n"
        "  - {name: B, site: 'https://b.az', feeds: ['https://b.az/rss']}\n",
        encoding="utf-8",
    )
    collector.sync_sources(conn, path)
    assert conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 2
    path.write_text(
        "sources:\n  - {name: A, site: 'https://a.az', feeds: ['https://a.az/rss']}\n"
        "  - {name: B, site: 'https://b.az', enabled: false, feeds: ['https://b.az/rss']}\n",
        encoding="utf-8",
    )
    collector.sync_sources(conn, path)
    assert [r[0] for r in conn.execute("SELECT url FROM feeds")] == ["https://a.az/rss"]
    assert [r[0] for r in conn.execute("SELECT name FROM sources WHERE active = 1")] == ["A"]
