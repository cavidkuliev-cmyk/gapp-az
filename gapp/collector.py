"""Сборщик: опрашивает RSS-ленты, отбрасывает дубликаты, склеивает сюжеты."""
import calendar
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
import yaml

from . import config, db
from .textutil import (
    canonical_url,
    clean_text,
    detect_rubric,
    is_russian,
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
    for src in data.get("sources", []):
        if src.get("enabled", True) is False:
            continue
        conn.execute(
            "INSERT INTO sources(name, site) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET site=excluded.site",
            (src["name"], src["site"]),
        )
        source_id = conn.execute("SELECT id FROM sources WHERE name=?", (src["name"],)).fetchone()[0]
        for url in src.get("feeds", []):
            wanted.add(url)
            conn.execute(
                "INSERT OR IGNORE INTO feeds(source_id, url) VALUES(?, ?)", (source_id, url)
            )
    # Ленты, убранные из файла или выключенные, больше не опрашиваем.
    for (url,) in conn.execute("SELECT url FROM feeds").fetchall():
        if url not in wanted:
            conn.execute("DELETE FROM feeds WHERE url=?", (url,))
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


def entry_to_item(entry, now: datetime) -> dict | None:
    """Превращает запись ленты в словарь статьи или None, если она не подходит."""
    title = clean_text(entry.get("title"))
    url = canonical_url(entry.get("link") or "")
    if not title or not url:
        return None
    summary = make_summary(entry.get("summary") or entry.get("description"))
    if not is_russian(title + " " + summary):
        return None
    published = _entry_time(entry, now)
    if now - published > timedelta(days=MAX_AGE_DAYS):
        return None
    categories = [t.get("term", "") for t in entry.get("tags") or []]
    return {
        "title": title,
        "url": url,
        "summary": summary,
        "image": _entry_image(entry),
        "rubric": detect_rubric(categories, url),
        "published_at": published,
    }


def _find_story(conn, tokens: set[str], published: datetime) -> int | None:
    """Ищет сюжет с похожим заголовком среди статей за последние часы."""
    if len(tokens) < MIN_COMMON_TOKENS:
        return None
    since = iso(published - timedelta(hours=config.STORY_WINDOW_HOURS))
    best_id, best_score = None, 0.0
    rows = conn.execute(
        "SELECT story_id, tokens FROM articles WHERE published_at >= ?", (since,)
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
    source_name = conn.execute("SELECT name FROM sources WHERE id=?", (source_id,)).fetchone()[0]
    story_id = _find_story(conn, tokens, published)
    result = "joined"
    if story_id is None:
        result = "new"
        cur = conn.execute(
            "INSERT INTO stories(slug, title, summary, image, rubric, first_at, last_at, main_source) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                slugify(item["title"]),
                item["title"],
                item["summary"],
                item["image"],
                item["rubric"],
                iso(published),
                iso(published),
                source_name,
            ),
        )
        story_id = cur.lastrowid

    conn.execute(
        "INSERT INTO articles(source_id, story_id, url, title, title_hash, tokens, summary, image, "
        "rubric, published_at, fetched_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def fetch_feed(client: httpx.Client, feed) -> tuple[str, bytes | None, dict]:
    """Скачивает ленту с условным запросом. Возвращает (статус, тело, заголовки кеша)."""
    headers = {}
    if feed["etag"]:
        headers["If-None-Match"] = feed["etag"]
    if feed["last_modified"]:
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
    try:
        feeds = conn.execute(
            "SELECT f.*, s.name AS source_name FROM feeds f JOIN sources s ON s.id = f.source_id "
            "WHERE s.active = 1"
        ).fetchall()
        for feed in feeds:
            now = now_utc()
            try:
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
            if body:
                parsed = feedparser.parse(body)
                for entry in parsed.entries:
                    item = entry_to_item(entry, now)
                    if item is None:
                        stats["skipped"] += 1
                        continue
                    stats[ingest(conn, feed["source_id"], item, now)] += 1
            conn.commit()
            time.sleep(1)  # вежливая пауза между запросами
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
