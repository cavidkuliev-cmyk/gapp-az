from datetime import datetime, timedelta, timezone

import httpx
import pytest

from gapp import collector, config, db, scrape

NOW = datetime.now(timezone.utc)
RECENT = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

NEWS_SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://n.az/ru/news/101</loc><news:news>
  <news:publication><news:name>N</news:name><news:language>ru</news:language></news:publication>
  <news:publication_date>{RECENT}</news:publication_date>
  <news:title>В Баку открыли новую станцию метро</news:title></news:news></url>
<url><loc>https://n.az/az/news/102</loc><news:news>
  <news:publication><news:name>N</news:name><news:language>az</news:language></news:publication>
  <news:publication_date>{RECENT}</news:publication_date>
  <news:title>Bakıda yeni metro stansiyası açıldı</news:title></news:news></url>
</urlset>""".encode()

LISTING = """<html><body>
<a href="/ru/politika/55501-zasedanie-parlamenta">Заседание</a>
<a href="/ru/sport/55502-match">Матч</a>
<a href="/ru/about">О нас</a>
<a href="https://other.az/ru/sport/55503-x">Чужой сайт</a>
</body></html>"""


def article_page(title, date=RECENT):
    return f"""<html lang="ru"><head><title>{title} | Сайт</title>
<meta property="og:type" content="article">
<meta property="og:title" content="{title}">
<meta property="og:description" content="Подробности события в нашем материале.">
<meta property="og:image" content="https://h.az/img/1.jpg">
<meta property="article:published_time" content="{date}">
</head><body></body></html>"""


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(collector.time, "sleep", lambda s: None)
    c = db.connect()
    yield c
    c.close()


def add_feed(conn, url, kind, pattern=""):
    conn.execute("INSERT INTO sources(id, name, site) VALUES(1, 'Сайт', 'https://x.az')")
    conn.execute("INSERT INTO feeds(source_id, url, kind, pattern) VALUES(1, ?, ?, ?)",
                 (url, kind, pattern))
    conn.commit()


def client_for(routes, hits):
    def handler(request):
        url = str(request.url)
        hits.append(url)
        if url in routes:
            return httpx.Response(200, content=routes[url])
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_parse_news_sitemap_and_index():
    kind, items = scrape.parse_sitemap(NEWS_SITEMAP)
    assert kind == "urlset" and [i["lang"] for i in items] == ["ru", "az"]
    assert items[0]["title"] == "В Баку открыли новую станцию метро" and items[0]["date"]
    index = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' \
            b"<sitemap><loc>https://n.az/s1.xml</loc><lastmod>2026-11-01</lastmod></sitemap></sitemapindex>"
    assert scrape.parse_sitemap(index)[0] == "index"
    assert scrape.parse_sitemap(b"<html>not xml")[0] == "invalid"


def test_article_meta_reads_opengraph_and_jsonld():
    meta = scrape.article_meta(article_page("Цены на нефть выросли"))
    assert meta["title"] == "Цены на нефть выросли" and meta["type"] == "article"
    assert meta["image"] == "https://h.az/img/1.jpg" and meta["date"] and meta["lang"] == "ru"
    ld = """<html><head><script type="application/ld+json">{"@graph":[{"@type":"NewsArticle",
    "headline":"Курс маната не изменился","datePublished":"2026-11-23T10:00:00+04:00"}]}</script></head></html>"""
    meta = scrape.article_meta(ld)
    assert meta["title"] == "Курс маната не изменился" and meta["date"].hour == 6


def test_guess_pattern_and_links():
    links, _ = scrape.page_links(LISTING, "https://h.az/ru/")
    assert len(links) == 3  # чужой сайт отброшен
    many = [f"https://h.az/ru/{sec}/{55000 + n}-zagolovok" for n, sec in
            enumerate(["politika", "sport", "ekonomika"] * 4)]
    pattern = scrape.guess_pattern(many)
    assert pattern and len(scrape.links_matching(LISTING, "https://h.az/ru/", pattern)) == 2


def test_news_sitemap_collected_without_page_fetches(conn):
    add_feed(conn, "https://n.az/news-sitemap.xml", "sitemap")
    hits = []
    client = client_for({"https://n.az/news-sitemap.xml": NEWS_SITEMAP}, hits)
    stats = collector.collect_once(conn, client)
    assert stats["new"] == 1  # азербайджанская запись пропущена по языку
    assert not any("/news/" in h for h in hits)  # заголовок и дата есть в карте
    assert collector.collect_once(conn, client)["new"] == 0


def test_html_listing_fetches_each_article_once(conn, monkeypatch):
    add_feed(conn, "https://h.az/ru/", "html", r"^/ru/[^/]+/\d+-")
    routes = {
        "https://h.az/ru/": LISTING.encode(),
        "https://h.az/ru/politika/55501-zasedanie-parlamenta":
            article_page("В парламенте обсудили проект бюджета").encode(),
        "https://h.az/ru/sport/55502-match": b"<html><head><title>x</title></head></html>",
    }
    hits = []
    client = client_for(routes, hits)
    stats = collector.collect_once(conn, client)
    assert stats["new"] == 1 and stats["skipped"] == 1
    row = conn.execute("SELECT * FROM articles").fetchone()
    assert row["title"] == "В парламенте обсудили проект бюджета" and row["image"]
    before = len(hits)
    collector.collect_once(conn, client)
    assert [h for h in hits[before:] if "5550" in h] == []  # статьи повторно не скачиваются


def test_page_fetch_limit_and_robots(conn, monkeypatch):
    add_feed(conn, "https://h.az/ru/", "html", r"^/ru/[^/]+/\d+-")
    monkeypatch.setattr(config, "PAGE_FETCH_LIMIT", 1)
    routes = {
        "https://h.az/ru/": LISTING.encode(),
        "https://h.az/ru/politika/55501-zasedanie-parlamenta": article_page("Первая новость дня").encode(),
        "https://h.az/ru/sport/55502-match": article_page("Вторая новость дня о спорте").encode(),
    }
    hits = []
    assert collector.collect_once(conn, client_for(routes, hits))["new"] == 1
    assert sum("5550" in h for h in hits) == 1

    routes["https://h.az/robots.txt"] = b"User-agent: *\nDisallow: /ru/sport/\n"
    hits.clear()
    monkeypatch.setattr(config, "PAGE_FETCH_LIMIT", 10)
    collector.collect_once(conn, client_for(routes, hits))
    assert not any("55502" in h for h in hits)  # запрещённый раздел не запрашиваем


def test_sitemap_backlog_continues_when_server_supports_etag(conn, monkeypatch):
    add_feed(conn, "https://h.az/ru/", "html", r"^/ru/[^/]+/\d+-")
    monkeypatch.setattr(config, "PAGE_FETCH_LIMIT", 1)
    pages = {
        "https://h.az/ru/politika/55501-zasedanie-parlamenta": article_page("Первая новость дня"),
        "https://h.az/ru/sport/55502-match": article_page("Вторая новость дня о спорте"),
    }

    def handler(request):
        url = str(request.url)
        if url == "https://h.az/ru/":
            if request.headers.get("If-None-Match") == "v1":
                return httpx.Response(304)
            return httpx.Response(200, text=LISTING, headers={"ETag": "v1"})
        if url in pages:
            return httpx.Response(200, text=pages[url])
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert collector.collect_once(conn, client)["new"] == 1
    assert collector.collect_once(conn, client)["new"] == 1  # остаток добран, 304 не помешал
