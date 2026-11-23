"""Сборщик: опрашивает RSS-ленты, отбрасывает дубликаты, склеивает сюжеты."""
import calendar
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import feedparser
import httpx
import yaml

from . import config, db, scrape
from .textutil import (
    LANGS,
    canonical_url,
    clean_text,
    detect_rubric,
    is_lang,
    make_summary,
    similarity,
    slugify,
    title_hash,
    title_tokens,
)

log = logging.getLogger("gapp.collector")

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_AGE_DAYS = 7
MIN_COMMON_TOKENS = 3


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sync_sources(conn: sqlite3.Connection, path=None) -> None:
    """Переносит список источников из sources.yaml в базу."""
    with open(path or config.SOURCES_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    wanted: set[str] = set()
    enabled_names: set[str] = set()
    for src in data.get("sources", []):
        if src.get("enabled", True) is False:
            continue
        enabled_names.add(src["name"])
        lang = src.get("lang", "ru")
        if lang not in LANGS:
            raise ValueError(f"{src['name']}: неизвестный язык {lang!r}")
        conn.execute(
            "INSERT INTO sources(name, site, lang, title, show_text) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET site=excluded.site, lang=excluded.lang, "
            "title=excluded.title, show_text=excluded.show_text, active=1",
            (src["name"], src["site"], lang, src.get("title", ""),
             0 if src.get("full_text", True) is False else 1),
        )
        source_id = conn.execute("SELECT id FROM sources WHERE name=?", (src["name"],)).fetchone()[0]
        for feed in src.get("feeds", []):
            if isinstance(feed, str):
                feed = {"url": feed}
            url, kind = feed["url"], feed.get("type", "rss")
            if kind not in ("rss", "sitemap", "html"):
                raise ValueError(f"{src['name']}: неизвестный тип ленты {kind!r}")
            wanted.add(url)
            conn.execute(
                "INSERT INTO feeds(source_id, url, kind, pattern) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET kind=excluded.kind, pattern=excluded.pattern",
                (source_id, url, kind, feed.get("pattern", "")),
            )
    # Ленты, убранные из файла или выключенные, больше не опрашиваем.
    for (url,) in conn.execute("SELECT url FROM feeds").fetchall():
        if url not in wanted:
            conn.execute("DELETE FROM feeds WHERE url=?", (url,))
    # Выключенные и убранные из файла источники скрываем со списка на сайте.
    for (name,) in conn.execute("SELECT name FROM sources WHERE active = 1").fetchall():
        if name not in enabled_names:
            conn.execute("UPDATE sources SET active = 0 WHERE name = ?", (name,))
    conn.commit()


def _entry_image(entry) -> str:
    candidates = []
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key) or []:
            candidates.append(item.get("url", ""))
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            candidates.append(link.get("href", ""))
    for url in candidates:
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _entry_time(entry, now: datetime) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return now
    dt = datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return min(dt, now)  # дата из будущего считается текущим моментом


_TURBO_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)


def _turbo_title(entry) -> str:
    """Заголовок из блока Яндекс.Турбо: в таких лентах у записи нет своего <title>."""
    match = _TURBO_H1.search(entry.get("turbo_content") or "")
    return clean_text(match.group(1)) if match else ""


def entry_to_item(entry, now: datetime, lang: str = "ru") -> dict | None:
    """Превращает запись ленты в словарь статьи или None, если она не подходит."""
    title = clean_text(entry.get("title")) or _turbo_title(entry)
    url = canonical_url(entry.get("link") or "")
    if not title or not url:
        return None
    summary = make_summary(entry.get("summary") or entry.get("description"))
    if not is_lang(title + " " + summary, lang):
        return None
    published = _entry_time(entry, now)
    if now - published > timedelta(days=MAX_AGE_DAYS):
        return None
    categories = [t.get("term", "") for t in entry.get("tags") or []]
    content = entry.get("content") or []
    raws = (content[0].get("value") if content else "", entry.get("turbo_content"),
            entry.get("yandex_full-text"))
    raw = max(raws, key=lambda value: len(scrape.feed_body(value)))
    body = scrape.feed_body(raw)
    image = _entry_image(entry)
    media = scrape.feed_media(raw, url) if body else []
    if body and image and all(m["url"] != image for m in media):
        media.insert(0, {"type": "image", "url": image})
    return {
        "body": body,
        "media": media,
        "title": title,
        "url": url,
        "summary": summary,
        "image": _entry_image(entry),
        "rubric": detect_rubric(categories, url),
        "published_at": published,
    }


def _find_story(conn, tokens: set[str], published: datetime, lang: str = "ru") -> int | None:
    """Ищет сюжет с похожим заголовком среди статей на том же языке за последние часы."""
    if len(tokens) < MIN_COMMON_TOKENS:
        return None
    since = iso(published - timedelta(hours=config.STORY_WINDOW_HOURS))
    best_id, best_score = None, 0.0
    rows = conn.execute(
        "SELECT a.story_id, a.tokens FROM articles a JOIN stories st ON st.id = a.story_id "
        "WHERE a.published_at >= ? AND st.lang = ?",
        (since, lang),
    )
    for row in rows:
        other = set(row["tokens"].split())
        if len(tokens & other) < MIN_COMMON_TOKENS:
            continue
        score = similarity(tokens, other)
        if score > best_score:
            best_id, best_score = row["story_id"], score
    return best_id if best_score >= config.STORY_SIMILARITY else None


def ingest(conn: sqlite3.Connection, source_id: int, item: dict, now: datetime | None = None) -> str:
    """Сохраняет статью. Возвращает 'duplicate', 'joined' или 'new'."""
    now = now or now_utc()
    t_hash = title_hash(item["title"])
    exists = conn.execute(
        "SELECT 1 FROM articles WHERE url=? OR (source_id=? AND title_hash=?)",
        (item["url"], source_id, t_hash),
    ).fetchone()
    if exists:
        return "duplicate"

    tokens = title_tokens(item["title"])
    published = item["published_at"]
    source_name, lang = conn.execute(
        "SELECT COALESCE(NULLIF(title, ''), name), lang FROM sources WHERE id=?", (source_id,)
    ).fetchone()
    story_id = _find_story(conn, tokens, published, lang)
    result = "joined"
    if story_id is None:
        result = "new"
        cur = conn.execute(
            "INSERT INTO stories(slug, title, summary, image, rubric, first_at, last_at, main_source, lang) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                slugify(item["title"]),
                item["title"],
                item["summary"],
                item["image"],
                item["rubric"],
                iso(published),
                iso(published),
                source_name,
                lang,
            ),
        )
        story_id = cur.lastrowid

    conn.execute(
        "INSERT INTO articles(source_id, story_id, url, title, title_hash, tokens, summary, image, "
        "rubric, published_at, fetched_at, body, body_status, media) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            story_id,
            item["url"],
            item["title"],
            t_hash,
            " ".join(sorted(tokens)),
            item["summary"],
            item["image"],
            item["rubric"],
            iso(published),
            iso(now),
            item.get("body", ""),
            item.get("body_status") or ("feed" if item.get("body") else ""),
            json.dumps(item.get("media") or [], ensure_ascii=False),
        ),
    )
    if result == "joined":
        conn.execute(
            "UPDATE stories SET "
            "last_at = MAX(last_at, ?), "
            "article_count = (SELECT COUNT(*) FROM articles WHERE story_id = stories.id), "
            "source_count = (SELECT COUNT(DISTINCT source_id) FROM articles WHERE story_id = stories.id), "
            "image = CASE WHEN image = '' THEN ? ELSE image END, "
            "summary = CASE WHEN summary = '' THEN ? ELSE summary END "
            "WHERE id = ?",
            (iso(published), item["image"], item["summary"], story_id),
        )
    return result


def _known(conn, url: str) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM articles WHERE url = ?", (url,)).fetchone()
        or conn.execute("SELECT 1 FROM seen_urls WHERE url = ?", (url,)).fetchone()
    )


def _candidates(client, robots, feed, body: bytes) -> list[dict]:
    if feed["kind"] == "html":
        html = body.decode("utf-8", "replace")
        return scrape.links_matching(html, feed["url"], feed["pattern"] or r"\d{3,}")
    kind, items = scrape.parse_sitemap(body)
    if kind != "index":
        return items
    # Индекс карт: берём две самые свежие вложенные карты.
    floor = datetime.min.replace(tzinfo=timezone.utc)
    nested = sorted(items, key=lambda i: i["date"] or floor, reverse=True)[:2]
    result = []
    for child in nested:
        if not robots.allowed(child["url"]):
            continue
        try:
            resp = client.get(child["url"])
        except httpx.HTTPError:
            continue
        if resp.status_code == 200 and len(resp.content) <= MAX_FEED_BYTES:
            result.extend(scrape.parse_sitemap(resp.content)[1])
        time.sleep(1)
    return result


def scrape_items(conn, client, robots, feed, body: bytes, now: datetime):
    """Статьи с сайта без RSS. Отдаёт словарь статьи или None для неподходящей."""
    candidates = _candidates(client, robots, feed, body)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda c: c["date"] or floor, reverse=True)
    fetched = 0
    for cand in candidates:
        url = canonical_url(cand["url"])
        if not url or _known(conn, url):
            continue
        if cand["date"] and now - cand["date"] > timedelta(days=MAX_AGE_DAYS):
            continue
        if cand["lang"] and not cand["lang"].startswith(feed["lang"]):
            continue
        meta = {"title": cand["title"], "summary": "", "image": cand["image"], "date": cand["date"]}
        body, body_status, media = "", "", []
        if not (cand["title"] and cand["date"]):
            # В карте нет заголовка: читаем страницу статьи, но не больше лимита за обход.
            if fetched >= config.PAGE_FETCH_LIMIT:
                break
            if not robots.allowed(url):
                continue
            fetched += 1
            conn.execute("INSERT OR REPLACE INTO seen_urls(url, seen_at) VALUES(?, ?)", (url, iso(now)))
            try:
                resp = client.get(url)
            except httpx.HTTPError:
                continue
            finally:
                time.sleep(1)
            if resp.status_code != 200 or len(resp.content) > MAX_FEED_BYTES:
                yield None
                continue
            page = scrape.article_meta(resp.text)
            if config.FETCH_BODIES:  # страница уже скачана: второй раз за текстом не ходим
                body, media = scrape.article_content(resp.text, url)
                body_status = "page" if body else "failed"
            if feed["kind"] == "html" and not (page["date"] or page["type"] == "article"):
                yield None  # это не статья, а раздел или служебная страница
                continue
            meta = {
                "title": cand["title"] or page["title"],
                "summary": page["summary"],
                "image": cand["image"] or page["image"],
                "date": cand["date"] or page["date"],
            }
        published = min(meta["date"] or now, now)
        if (
            not meta["title"]
            or not is_lang(meta["title"] + " " + meta["summary"], feed["lang"])
            or now - published > timedelta(days=MAX_AGE_DAYS)
        ):
            yield None
            continue
        yield {
            "title": meta["title"], "url": url, "summary": meta["summary"],
            "image": meta["image"] if meta["image"].startswith(("http://", "https://")) else "",
            "rubric": detect_rubric([], url), "published_at": published,
            "body": body, "body_status": body_status, "media": media,
        }


def fill_bodies(conn, client, robots, now: datetime) -> dict:
    """Добирает полный текст свежих статей со страниц изданий.

    Текст нужен для анализа и на сайте не показывается. Сайты, которые запрещают
    бота в robots.txt или отвечают 401/403, не запрашиваются повторно.
    """
    stats = {"body_saved": 0, "body_failed": 0}
    since = iso(now - timedelta(hours=config.BODY_MAX_AGE_HOURS))
    rows = conn.execute(
        "SELECT id, url FROM articles WHERE body_status = '' AND published_at >= ? "
        "ORDER BY published_at DESC LIMIT 2000",
        (since,),
    ).fetchall()
    per_host: dict[str, int] = {}
    blocked: set[str] = set()
    total = 0
    for row in rows:
        if total >= config.BODY_FETCH_PER_PASS:
            break
        host = urlsplit(row["url"]).netloc
        if host in blocked:
            conn.execute("UPDATE articles SET body_status = 'blocked' WHERE id = ?", (row["id"],))
            continue
        if per_host.get(host, 0) >= config.BODY_FETCH_PER_SITE:
            continue
        if not robots.allowed(row["url"]):
            blocked.add(host)
            conn.execute("UPDATE articles SET body_status = 'blocked' WHERE id = ?", (row["id"],))
            continue
        per_host[host] = per_host.get(host, 0) + 1
        total += 1
        body, media, status = "", [], "failed"
        try:
            resp = client.get(row["url"])
            if resp.status_code in (401, 403):
                status = "blocked"
                blocked.add(host)
            elif resp.status_code == 200 and len(resp.content) <= MAX_FEED_BYTES:
                body, media = scrape.article_content(resp.text, row["url"])
                status = "page" if body else "failed"
        except httpx.HTTPError:
            pass
        finally:
            time.sleep(1)
        conn.execute(
            "UPDATE articles SET body = ?, body_status = ?, media = ? WHERE id = ?",
            (body, status, json.dumps(media, ensure_ascii=False), row["id"]),
        )
        stats["body_saved" if body else "body_failed"] += 1
    conn.commit()
    return stats


def fetch_feed(client: httpx.Client, feed) -> tuple[str, bytes | None, dict]:
    """Скачивает ленту с условным запросом. Возвращает (статус, тело, заголовки кеша)."""
    headers = {}
    # Условный запрос только для RSS. Карту сайта и страницу раздела читаем каждый раз:
    # за обход берётся ограниченное число статей, остаток добирается следующими обходами.
    conditional = feed["kind"] == "rss"
    if conditional and feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if conditional and feed["last_modified"]:
        headers["If-Modified-Since"] = feed["last_modified"]
    resp = client.get(feed["url"], headers=headers)
    if resp.status_code == 304:
        return "304", None, {}
    if resp.status_code != 200:
        return str(resp.status_code), None, {}
    if len(resp.content) > MAX_FEED_BYTES:
        return "too-large", None, {}
    cache = {"etag": resp.headers.get("ETag"), "last_modified": resp.headers.get("Last-Modified")}
    return "200", resp.content, cache


def collect_once(conn: sqlite3.Connection, client: httpx.Client | None = None) -> dict:
    """Один обход всех лент. Ошибка одной ленты не останавливает остальные."""
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": config.USER_AGENT},
        timeout=20,
        follow_redirects=True,
    )
    stats = {"new": 0, "joined": 0, "duplicate": 0, "skipped": 0, "failed_feeds": 0}
    robots = scrape.Robots(client, config.USER_AGENT)
    conn.execute("DELETE FROM seen_urls WHERE seen_at < ?", (iso(now_utc() - timedelta(days=30)),))
    try:
        feeds = conn.execute(
            "SELECT f.*, s.name AS source_name, s.lang AS lang FROM feeds f JOIN sources s ON s.id = f.source_id "
            "WHERE s.active = 1"
        ).fetchall()
        for feed in feeds:
            now = now_utc()
            try:
                if feed["kind"] != "rss" and not robots.allowed(feed["url"]):
                    status, body, cache = "robots", None, {}
                else:
                    status, body, cache = fetch_feed(client, feed)
            except httpx.HTTPError as exc:
                status, body, cache = f"error: {type(exc).__name__}", None, {}
            ok = status in ("200", "304")
            conn.execute(
                "UPDATE feeds SET last_checked=?, last_status=?, "
                "fail_count = CASE WHEN ? THEN 0 ELSE fail_count + 1 END, "
                "etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified) WHERE id=?",
                (iso(now), status, ok, cache.get("etag"), cache.get("last_modified"), feed["id"]),
            )
            if not ok:
                stats["failed_feeds"] += 1
                log.warning("лента %s: %s", feed["url"], status)
            if body and feed["kind"] == "rss":
                # Без чистки разметки: иначе feedparser вырезает из текста встроенные видео.
                # Всё, что идёт на сайт, проходит через clean_text и экранируется шаблонами.
                parsed = feedparser.parse(body, sanitize_html=False)
                for entry in parsed.entries:
                    item = entry_to_item(entry, now, feed["lang"])
                    if item is None:
                        stats["skipped"] += 1
                        continue
                    stats[ingest(conn, feed["source_id"], item, now)] += 1
            elif body:
                for item in scrape_items(conn, client, robots, feed, body, now):
                    if item is None:
                        stats["skipped"] += 1
                        continue
                    stats[ingest(conn, feed["source_id"], item, now)] += 1
            conn.commit()
            time.sleep(1)  # вежливая пауза между запросами
        if config.FETCH_BODIES:
            stats.update(fill_bodies(conn, client, robots, now_utc()))
    finally:
        if own_client:
            client.close()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.connect()
    sync_sources(conn)
    if "--once" in sys.argv[1:]:
        log.info("обход завершён: %s", collect_once(conn))
        return
    while True:
        try:
            stats = collect_once(conn)
            log.info("обход завершён: %s", stats)
        except Exception:  # сборщик должен пережить любую ошибку обхода
            log.exception("обход упал")
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
