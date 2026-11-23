"""Полный текст статей: из ленты, со страницы, с уважением к запретам сайтов."""
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest

from gapp import collector, config, db, scrape

PARA = "Это абзац текста статьи, достаточно длинный, чтобы считаться настоящим содержанием новости. "
NOW = format_datetime(datetime.now(timezone.utc))


def page(title: str) -> str:
    return f"""<html lang="ru"><head><title>{title}</title></head><body>
<header><p>Меню сайта</p></header><div class="side"><p>Реклама</p></div>
<article><h1>{title}</h1><div><p>{PARA}<b>Важное</b> слово.</p><figure><p>Подпись к фото</p></figure>
<p>{PARA}</p><p>{PARA}</p></div></article>
<footer><p>{PARA * 6}</p></footer><script>var x = "<p>код</p>";</script></body></html>"""


def rss(items: str) -> bytes:
    return (f"<?xml version='1.0' encoding='UTF-8'?><rss version='2.0' "
            f"xmlns:content='http://purl.org/rss/1.0/modules/content/'><channel><title>T</title>"
            f"{items}</channel></rss>").encode()


def item(n: int, title: str, extra: str = "") -> str:
    return (f"<item><title>{title}</title><link>https://s.az/news/{n}</link>"
            f"<description>Короткий анонс новости номер {n}.</description>"
            f"<pubDate>{NOW}</pubDate>{extra}</item>")


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(collector.time, "sleep", lambda s: None)
    c = db.connect()
    c.execute("INSERT INTO sources(id, name, site) VALUES(1, 'Сайт', 'https://s.az')")
    c.execute("INSERT INTO feeds(source_id, url) VALUES(1, 'https://s.az/rss')")
    c.commit()
    yield c
    c.close()


def client_for(routes, hits):
    def handler(request):
        url = str(request.url)
        hits.append(url)
        if url not in routes:
            return httpx.Response(404)
        value = routes[url]
        if isinstance(value, int):
            return httpx.Response(value)
        return httpx.Response(200, content=value if isinstance(value, bytes) else value.encode())
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_article_body_picks_the_article_block():
    body = scrape.article_body(page("Заголовок"))
    assert body.count("\n") == 2 and "Важное слово." in body
    for junk in ("Меню", "Реклама", "Подпись", "код"):
        assert junk not in body
    assert scrape.article_body("<html><body><p>Мало текста</p></body></html>") == ""
    ld = ('<script type="application/ld+json">{"@type": "NewsArticle", "articleBody": "'
          + PARA * 3 + '\\n\\nВторой абзац."}</script><p>мусор</p>')
    assert scrape.article_body(ld).split("\n")[-1] == "Второй абзац."


def test_body_from_feed_needs_no_page_fetch(conn):
    full = f"<content:encoded><![CDATA[<p>{PARA}</p><p>{PARA}{PARA}</p>]]></content:encoded>"
    hits = []
    client = client_for({"https://s.az/rss": rss(item(1, "В Баку открыли новую станцию метро", full))}, hits)
    stats = collector.collect_once(conn, client)
    assert stats["new"] == 1 and stats["body_saved"] == 0
    row = conn.execute("SELECT body, body_status FROM articles").fetchone()
    assert row["body_status"] == "feed" and row["body"].count("\n") == 1
    assert not any("/news/" in h for h in hits)


def test_body_fetched_from_page_once_with_limits(conn, monkeypatch):
    monkeypatch.setattr(config, "BODY_FETCH_PER_SITE", 2)
    titles = ["В Баку открыли новую станцию метро", "Центробанк объявил курс маната на завтра",
              "Сборная назвала состав на отборочный матч"]
    routes = {"https://s.az/rss": rss("".join(item(n, t) for n, t in enumerate(titles, 1)))}
    routes.update({f"https://s.az/news/{n}": page(t) for n, t in enumerate(titles, 1)})
    hits = []
    client = client_for(routes, hits)
    assert collector.collect_once(conn, client)["body_saved"] == 2      # лимит на сайт за обход
    assert collector.collect_once(conn, client)["body_saved"] == 1      # остаток добран
    assert collector.collect_once(conn, client)["body_saved"] == 0
    assert sum("/news/" in h for h in hits) == 3                         # каждая страница один раз
    rows = conn.execute("SELECT body, body_status FROM articles").fetchall()
    assert all(r["body_status"] == "page" and "Важное слово." in r["body"] for r in rows)


def test_blocked_sites_are_not_retried(conn):
    routes = {
        "https://s.az/rss": rss(item(1, "В Баку открыли новую станцию метро")
                                + item(2, "Центробанк объявил курс маната на завтра")),
        "https://s.az/news/1": 403, "https://s.az/news/2": 403,
    }
    hits = []
    client = client_for(routes, hits)
    collector.collect_once(conn, client)
    collector.collect_once(conn, client)
    assert sum("/news/" in h for h in hits) == 1  # после первого отказа сайт больше не трогаем
    assert {r[0] for r in conn.execute("SELECT body_status FROM articles")} == {"blocked"}


def test_robots_disallow_stops_body_fetch(conn):
    routes = {
        "https://s.az/rss": rss(item(1, "В Баку открыли новую станцию метро")),
        "https://s.az/robots.txt": "User-agent: *\nDisallow: /news/\n",
        "https://s.az/news/1": page("x"),
    }
    hits = []
    collector.collect_once(conn, client_for(routes, hits))
    assert not any("/news/" in h for h in hits)
    assert conn.execute("SELECT body_status FROM articles").fetchone()[0] == "blocked"


def test_full_text_is_not_shown_on_the_site(conn):
    from fastapi.testclient import TestClient

    from gapp.web import app

    full = f"<content:encoded><![CDATA[<p>{PARA}</p><p>Секретный хвост статьи. {PARA}</p>]]></content:encoded>"
    collector.collect_once(conn, client_for(
        {"https://s.az/rss": rss(item(1, "В Баку открыли новую станцию метро", full))}, []))
    story = conn.execute("SELECT id, slug FROM stories").fetchone()
    site = TestClient(app)
    html = site.get(f"/novost/{story['slug']}-{story['id']}").text + site.get("/").text + site.get("/rss.xml").text
    assert "Короткий анонс" in html and "Секретный хвост" not in html
