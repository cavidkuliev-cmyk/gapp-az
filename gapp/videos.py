"""Популярные видео YouTube в стране (раздел «Видео»).

Берём официальный список «самые популярные в регионе» (YouTube Data API v3, videos.list,
chart=mostPopular): название, канал, число просмотров. Сами видео и картинки не копируются:
на сайте плеер YouTube и превью по ссылке. Без ключа GAPP_YOUTUBE_API_KEY ничего не делаем.
"""
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from . import config
from .textutil import clean_text

log = logging.getLogger("gapp.videos")

API_URL = "https://www.googleapis.com/youtube/v3/videos"
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Общий список и самые ходовые категории (музыка, спорт, игры, люди, юмор, развлечения, новости).
CATEGORIES = ("", "10", "17", "20", "22", "23", "24", "25")
MAX_PAGES = 3
KEEP_DAYS = 7


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_duration(value: str | None) -> int:
    """ISO 8601 вида PT1H2M3S в секунды."""
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _int(value) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def parse_videos(payload: dict) -> list[dict]:
    """Ролики из ответа API. Всё лишнее и неправильное пропускается."""
    result = []
    for item in payload.get("items") or []:
        video_id = str(item.get("id", ""))
        snippet = item.get("snippet") or {}
        if not VIDEO_ID.match(video_id) or snippet.get("liveBroadcastContent") == "upcoming":
            continue
        try:
            published = datetime.fromisoformat(str(snippet.get("publishedAt", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        title = clean_text(snippet.get("title", ""))
        if not title:
            continue
        stats = item.get("statistics") or {}
        result.append({
            "id": video_id,
            "title": title[:300],
            "channel": clean_text(snippet.get("channelTitle", ""))[:120],
            "channel_id": str(snippet.get("channelId", ""))[:40],
            "published_at": iso(published),
            "views": _int(stats.get("viewCount")),
            "likes": _int(stats.get("likeCount")),
            "duration": parse_duration((item.get("contentDetails") or {}).get("duration")),
        })
    return result


def save_videos(conn: sqlite3.Connection, videos: list[dict], now: datetime) -> int:
    for video in videos:
        conn.execute(
            "INSERT INTO videos(id, title, channel, channel_id, published_at, views, likes, duration, seen_at) "
            "VALUES(:id, :title, :channel, :channel_id, :published_at, :views, :likes, :duration, :seen) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, channel=excluded.channel, "
            "views=excluded.views, likes=excluded.likes, duration=excluded.duration, seen_at=excluded.seen_at",
            {**video, "seen": iso(now)},
        )
    return len(videos)


def due(conn: sqlite3.Connection, now: datetime) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = 'videos_checked'").fetchone()
    return row is None or row[0] <= iso(now - timedelta(minutes=config.VIDEO_POLL_MINUTES))


def collect_videos(conn: sqlite3.Connection, client: httpx.Client, now: datetime) -> dict:
    """Обновляет список популярных роликов, не чаще раза в VIDEO_POLL_MINUTES. Без ключа ничего не делает."""
    stats = {"videos": 0, "video_errors": 0}
    if not config.YOUTUBE_API_KEY or not due(conn, now):
        return stats
    conn.execute("INSERT INTO meta(key, value) VALUES('videos_checked', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (iso(now),))
    conn.commit()
    for category in CATEGORIES:
        token = ""
        for _ in range(MAX_PAGES):
            params = {"part": "snippet,statistics,contentDetails", "chart": "mostPopular",
                      "regionCode": config.VIDEO_REGION, "maxResults": 50, "key": config.YOUTUBE_API_KEY}
            if category:
                params["videoCategoryId"] = category
            if token:
                params["pageToken"] = token
            try:
                resp = client.get(API_URL, params=params)
            except httpx.HTTPError as exc:
                # В тексте ошибки может быть адрес с ключом: пишем только тип.
                log.warning("YouTube API: %s", type(exc).__name__)
                stats["video_errors"] += 1
                break
            if resp.status_code in (400, 403, 404):
                reason = ""
                try:
                    reason = resp.json()["error"]["errors"][0]["reason"]
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
                if resp.status_code == 403 or reason == "keyInvalid":
                    log.warning("YouTube API: доступ отклонён (%s). Проверьте ключ и квоту.", reason or 403)
                    stats["video_errors"] += 1
                    return stats
                break  # у категории нет списка популярных: это нормально
            if resp.status_code != 200:
                log.warning("YouTube API: HTTP %s", resp.status_code)
                stats["video_errors"] += 1
                break
            try:
                payload = resp.json()
            except ValueError:
                stats["video_errors"] += 1
                break
            stats["videos"] += save_videos(conn, parse_videos(payload), now)
            token = payload.get("nextPageToken") or ""
            if not token:
                break
        conn.commit()
    conn.execute("DELETE FROM videos WHERE published_at < ?", (iso(now - timedelta(days=KEEP_DAYS)),))
    conn.commit()
    return stats
