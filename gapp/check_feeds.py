"""Проверка лент из sources.yaml: `python -m gapp.check_feeds`."""
import feedparser
import httpx
import yaml

from . import config, scrape
from .collector import entry_to_item, now_utc


def main() -> None:
    with open(config.SOURCES_PATH, encoding="utf-8") as fh:
        sources = (yaml.safe_load(fh) or {}).get("sources", [])
    with httpx.Client(
        headers={"User-Agent": config.USER_AGENT}, timeout=20, follow_redirects=True
    ) as client:
        for src in sources:
            if src.get("enabled", True) is False:
                continue
            for feed in src.get("feeds", []):
                feed = {"url": feed} if isinstance(feed, str) else feed
                url, kind = feed["url"], feed.get("type", "rss")
                try:
                    resp = client.get(url)
                    if kind != "rss":
                        if resp.status_code != 200:
                            found = []
                        elif kind == "html":
                            found = scrape.links_matching(resp.text, url, feed.get("pattern") or r"\d{3,}")
                        else:
                            found = scrape.parse_sitemap(resp.content)[1]
                        print(f"{src['name']:<24} {resp.status_code}  адресов: {len(found):>4}  "
                              f"({kind})  {url}")
                        continue
                    entries = feedparser.parse(resp.content).entries if resp.status_code == 200 else []
                    good = sum(1 for e in entries if entry_to_item(e, now_utc()))
                    print(f"{src['name']:<24} {resp.status_code}  записей: {len(entries):>3}  "
                          f"подходящих: {good:>3}  {url}")
                except httpx.HTTPError as exc:
                    print(f"{src['name']:<24} ОШИБКА {type(exc).__name__}  {url}")


if __name__ == "__main__":
    main()
