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


MEDIA_PAGE = f"""<html><head><meta property="og:image" content="https://s.az/img/main.jpg">
<script type="application/ld+json">{{"@type": "NewsArticle", "headline": "h",
"video": {{"@type": "VideoObject", "embedUrl": "https://www.youtube.com/embed/abc123"}}}}</script></head><body>
<header><img src="/img/logo.png"><img src="/img/menu-photo.jpg"></header>
<main><article><h1>Заголовок</h1>
<figure><img src="/img/main.jpg" alt=""><figcaption>Подпись</figcaption></figure>
<div class="text"><p>{PARA}</p>
<div class="gallery"><img data-src="/img/second.jpg" src="data:image/gif;base64,R0lGOD"><img src="/img/icon-share.png"></div>
<p>{PARA}</p><iframe src="https://www.youtube.com/embed/abc123"></iframe>
<iframe src="https://ads.example.com/frame"></iframe>
<video poster="/img/poster.jpg"><source src="/video/clip.mp4" type="video/mp4"></video><p>{PARA}</p></div>
<aside><img src="/img/related.jpg"></aside></article></main>
<footer><img src="/img/footer-photo.jpg"></footer></body></html>"""


def test_article_media_links():
    body, media = scrape.article_content(MEDIA_PAGE, "https://s.az/news/1")
    assert body.count("\n") == 2
    assert media == [
        {"type": "image", "url": "https://s.az/img/main.jpg"},
        {"type": "video", "url": "https://www.youtube.com/embed/abc123"},
        {"type": "image", "url": "https://s.az/img/second.jpg"},
        {"type": "image", "url": "https://s.az/img/poster.jpg"},
        {"type": "video", "url": "https://s.az/video/clip.mp4"},
    ]


def test_media_saved_from_page_and_feed(conn):
    import json

    full = (f"<content:encoded><![CDATA[<p>{PARA}</p><img src='https://s.az/img/in-feed.jpg'>"
            f"<p>{PARA}{PARA}</p><iframe src='https://www.youtube.com/embed/zzz'></iframe>]]></content:encoded>"
            "<enclosure url='https://s.az/img/cover.jpg' length='0' type='image/jpeg'/>")
    routes = {
        "https://s.az/rss": rss(item(1, "В Баку открыли новую станцию метро", full)
                                + item(2, "Центробанк объявил курс маната на завтра")),
        "https://s.az/news/2": MEDIA_PAGE,
    }
    hits = []
    collector.collect_once(conn, client_for(routes, hits))
    rows = {r["url"]: r for r in conn.execute("SELECT url, body_status, media FROM articles")}
    feed_media = json.loads(rows["https://s.az/news/1"]["media"])
    assert rows["https://s.az/news/1"]["body_status"] == "feed"
    assert [m["url"] for m in feed_media] == [
        "https://s.az/img/cover.jpg", "https://s.az/img/in-feed.jpg", "https://www.youtube.com/embed/zzz"]
    page_media = json.loads(rows["https://s.az/news/2"]["media"])
    assert rows["https://s.az/news/2"]["body_status"] == "page" and len(page_media) == 5
    assert not any(h.endswith((".jpg", ".mp4")) for h in hits)  # сами файлы не скачиваются


def _story_page(conn, source_extra=""):
    from fastapi.testclient import TestClient

    from gapp.web import app

    full = (f"<content:encoded><![CDATA[<p>{PARA}</p><img src='https://s.az/img/a.jpg'>"
            f"<p>Хвост <b>статьи</b>. {PARA}</p><p>{PARA}</p><img src='https://s.az/img/b.jpg'>"
            "<iframe src='https://www.youtube.com/embed/zzz'></iframe>"
            "<iframe src='https://evil.example/x'></iframe>]]></content:encoded>")
    collector.collect_once(conn, client_for(
        {"https://s.az/rss": rss(item(1, "В Баку открыли <станцию> метро", full))}, []))
    story = conn.execute("SELECT id, slug FROM stories").fetchone()
    return TestClient(app).get(f"/novost/{story['slug']}-{story['id']}")


def test_story_page_shows_full_text_photo_and_video(conn):
    page = _story_page(conn).text
    assert "Хвост статьи." in page and "<p>Короткий анонс" not in page
    assert 'src="https://s.az/img/a.jpg"' in page and 'src="https://s.az/img/b.jpg"' in page
    assert 'src="https://www.youtube.com/embed/zzz"' in page
    assert "evil.example" not in page                       # посторонние вставки не показываются
    assert "Источник: <b>Сайт</b>" in page and 'href="https://s.az/news/1"' in page
    assert "<станцию>" not in page                         # разметка из заголовков не попадает на страницу


def test_full_text_can_be_switched_off(conn, monkeypatch):
    conn.execute("UPDATE sources SET show_text = 0")
    conn.commit()
    page = _story_page(conn).text                           # выключено для источника
    assert "Хвост статьи." not in page and "<p>Короткий анонс" in page


def test_full_text_global_switch(conn, monkeypatch):
    monkeypatch.setattr(config, "SHOW_FULL_TEXT", False)
    page = _story_page(conn).text
    assert "Хвост статьи." not in page and "<p>Короткий анонс" in page


def test_body_stats_prints_sizes(conn, capsys):
    from gapp import body_stats

    full = f"<content:encoded><![CDATA[<p>{PARA}</p><p>{PARA}{PARA}</p>]]></content:encoded>"
    collector.collect_once(conn, client_for({"https://s.az/rss": rss(item(1, "В Баку открыли метро", full))}, []))
    body_stats.main()
    out = capsys.readouterr().out
    assert "Место в базе по дням" in out and "Файл базы:" in out and "Сайт" in out


RELATED_PAGE = f"""<html><body><div class="wrap"><div class="content"><h1>Заголовок</h1>
<img src="/img/photo1.jpg"><p>{PARA}</p><p>{PARA}</p><p>{PARA}</p><p>{PARA}</p><img src="/img/photo2.jpg"></div>
<div class="related-news"><div><img src="/img/other1.jpg"></div></div>
<ul><li><a href="/news/77"><img src="/img/card1.jpg"></a></li></ul>
<a href="/img/big.jpg"><img src="/img/photo3.jpg"></a></div></body></html>"""


def test_related_cards_are_not_article_media():
    _, media = scrape.article_content(RELATED_PAGE, "https://s.az/news/1")
    assert [m["url"] for m in media] == [
        "https://s.az/img/photo1.jpg", "https://s.az/img/photo2.jpg", "https://s.az/img/photo3.jpg"]


def test_share_buttons_and_ads_are_not_media():
    page = f"""<html><body><div><img src="/media/qaz.png"><p>{PARA}</p><p>{PARA}</p><p>{PARA}</p><p>{PARA}</p>
<img src="/images/share_btn_wa.png"><img src="/images/share_btn_print.png"><img src="/adv/Azernews-Reklam.png"></div></body></html>"""
    _, media = scrape.article_content(page, "https://s.az/news/1")
    assert [m["url"] for m in media] == ["https://s.az/media/qaz.png"]
    from gapp import web
    old = [{"type": "image", "url": "https://s.az/media/qaz.png"},
           {"type": "image", "url": "https://s.az/images/share_btn_tg.png"}]
    assert [b["kind"] for b in web.full_blocks("a\nb", old)].count("image") == 1
