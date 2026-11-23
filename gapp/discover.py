"""Подбор способа сбора для сайта: `python -m gapp.discover https://site.az/ru/`.

Проверяет по порядку ленты RSS, карты сайта и саму страницу, печатает,
что из этого отвечает, и готовый фрагмент для sources.yaml.
"""
import sys
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from . import config, scrape
from .collector import entry_to_item, now_utc
from .textutil import is_russian

FEED_PATHS = ("rss", "rss.xml", "feed", "feed/", "rss/all.rss", "index.rss", "atom.xml")
SITEMAP_PATHS = ("news-sitemap.xml", "sitemap-news.xml", "sitemap_news.xml", "sitemap.xml",
                 "sitemap_index.xml")


def _get(client, url):
    try:
        return client.get(url)
    except httpx.HTTPError as exc:
        print(f"  ОШИБКА {type(exc).__name__:<16} {url}")
        return None


def discover(site: str, client: httpx.Client) -> dict | None:
    if not site.endswith("/"):
        site += "/"
    parts = urlsplit(site)
    origin = f"{parts.scheme}://{parts.netloc}/"
    robots = scrape.Robots(client, config.USER_AGENT)
    if not robots.allowed(site):
        print("robots.txt запрещает боту этот сайт. Нужна договорённость с изданием.")
        return None
    home = _get(client, site)
    feeds, links = [], []
    if home is not None and home.status_code == 200:
        links, feeds = scrape.page_links(home.text, site)
    elif home is not None:
        print(f"  {home.status_code}  главная страница {site}")

    print("Ленты RSS:")
    candidates = list(dict.fromkeys(feeds + [urljoin(b, p) for b in (site, origin) for p in FEED_PATHS]))
    for url in candidates:
        resp = _get(client, url)
        if resp is None or resp.status_code != 200:
            if resp is not None and resp.status_code in (401, 403, 429):
                print(f"  {resp.status_code}  сайт отказывает боту  {url}")
            continue
        entries = feedparser.parse(resp.content).entries
        good = sum(1 for e in entries if entry_to_item(e, now_utc()))
        if entries:
            print(f"  200  записей {len(entries)}, подходящих {good}  {url}")
        if good >= 3:
            return {"url": url, "type": "rss"}

    print("Карты сайта:")
    candidates = list(dict.fromkeys(
        robots.sitemaps(site) + [urljoin(origin, p) for p in SITEMAP_PATHS]))
    best = None
    for url in candidates[:12]:
        resp = _get(client, url)
        if resp is None or resp.status_code != 200:
            continue
        kind, items = scrape.parse_sitemap(resp.content)
        if kind == "invalid" or not items:
            continue
        titled = [i for i in items if i["title"]]
        russian = sum(1 for i in titled if is_russian(i["title"]))
        dated = sum(1 for i in items if i["date"])
        label = "индекс карт" if kind == "index" else "карта"
        print(f"  200  {label}: адресов {len(items)}, с заголовком {len(titled)} "
              f"(русских {russian}), с датой {dated}  {url}")
        if russian >= 3:
            return {"url": url, "type": "sitemap"}  # карта для новостей: лучший вариант
        if best is None and dated >= 3:
            best = {"url": url, "type": "sitemap"}
    if best:
        return best

    print("Страница раздела:")
    pattern = scrape.guess_pattern(links)
    if pattern:
        count = len(scrape.links_matching(home.text, site, pattern))
        print(f"  ссылок на статьи {count}, шаблон адреса {pattern}")
        return {"url": site, "type": "html", "pattern": pattern}
    print("  не удалось распознать ссылки на статьи")
    return None


def main() -> None:
    if len(sys.argv) < 2:
        print("Укажите адрес сайта, например: python -m gapp.discover https://1news.az/")
        raise SystemExit(2)
    with httpx.Client(headers={"User-Agent": config.USER_AGENT}, timeout=20,
                      follow_redirects=True) as client:
        for site in sys.argv[1:]:
            print(f"\n=== {site}")
            found = discover(site, client)
            if not found:
                print("Итог: способ не найден.")
                continue
            print("Итог: добавьте в sources.yaml к этому источнику")
            if found["type"] == "rss":
                print(f"    feeds: [{found['url']}]")
            else:
                print("    feeds:")
                print(f"      - url: {found['url']}")
                print(f"        type: {found['type']}")
                if found.get("pattern"):
                    print(f"        pattern: '{found['pattern']}'")


if __name__ == "__main__":
    main()
