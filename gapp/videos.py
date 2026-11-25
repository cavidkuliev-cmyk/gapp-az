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
import yaml

from . import config
from .textutil import clean_text

log = logging.getLogger("gapp.videos")

API_BASE = "https://www.googleapis.com/youtube/v3/"
API_URL = API_BASE + "videos"
CHANNELS_PATH = config.BASE_DIR / "video_channels.yaml"
MAX_CHANNELS = 120          # сколько каналов опрашиваем за раз
SEED_RETRY_DAYS = 7         # как часто повторять поиск канала, которого не нашли
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
        language = str(snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or "").lower()
        result.append({
            "id": video_id,
            "title": title[:300],
            "channel": clean_text(snippet.get("channelTitle", ""))[:120],
            "channel_id": str(snippet.get("channelId", ""))[:40],
            "published_at": iso(published),
            "views": _int(stats.get("viewCount")),
            "likes": _int(stats.get("likeCount")),
            "duration": parse_duration((item.get("contentDetails") or {}).get("duration")),
            # Азербайджанский: язык ролика az или в названии есть буква «ə», которой нет в турецком.
            "is_az": int(language.startswith("az") or "ə" in title.lower()),
        })
    return result


def save_videos(conn: sqlite3.Connection, videos: list[dict], now: datetime,
                popular: int = 0, from_channel: int = 0) -> int:
    for video in videos:
        conn.execute(
            "INSERT INTO videos(id, title, channel, channel_id, published_at, views, likes, duration, seen_at, "
            "popular, from_channel, is_az) "
            "VALUES(:id, :title, :channel, :channel_id, :published_at, :views, :likes, :duration, :seen, "
            ":popular, :from_channel, :is_az) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, channel=excluded.channel, "
            "views=excluded.views, likes=excluded.likes, duration=excluded.duration, seen_at=excluded.seen_at, "
            "popular=MAX(popular, excluded.popular), from_channel=MAX(from_channel, excluded.from_channel), "
            "is_az=excluded.is_az",
            {**video, "seen": iso(now), "popular": popular, "from_channel": from_channel},
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
                      "regionCode": config.VIDEO_REGION, "maxResults": 50}
            if category:
                params["videoCategoryId"] = category
            if token:
                params["pageToken"] = token
            try:
                # Ключ только в заголовке: адрес запроса попадает в журнал, а заголовки нет.
                resp = client.get(API_URL, params=params, headers={"X-Goog-Api-Key": config.YOUTUBE_API_KEY})
            except httpx.HTTPError as exc:
                # Пишем только тип ошибки, без текста запроса.
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
            found = parse_videos(payload)
            stats["videos"] += save_videos(conn, found, now, popular=1)
            discover_channels(conn, found, now)
            token = payload.get("nextPageToken") or ""
            if not token:
                break
        conn.commit()
    resolve_seeds(conn, client, now)
    stats.update(collect_channels(conn, client, now))
    conn.execute("DELETE FROM videos WHERE published_at < ?", (iso(now - timedelta(days=KEEP_DAYS)),))
    conn.commit()
    return stats


def discover_channels(conn: sqlite3.Connection, found: list[dict], now: datetime) -> None:
    """Каналы азербайджанских роликов из списка популярных запоминаем: их свежие загрузки покажем отдельно."""
    for video in found:
        if video["is_az"] and video["channel_id"]:
            conn.execute(
                "INSERT OR IGNORE INTO video_channels(id, title, origin, added_at) VALUES(?, ?, 'auto', ?)",
                (video["channel_id"], video["channel"], iso(now)),
            )


def _api(client: httpx.Client, path: str, params: dict) -> tuple[int, dict]:
    """GET к API. Ключ только в заголовке. Возвращает (код, json); код 0 при сбое сети."""
    try:
        resp = client.get(API_BASE + path, params=params, headers={"X-Goog-Api-Key": config.YOUTUBE_API_KEY})
    except httpx.HTTPError as exc:
        log.warning("YouTube API: %s", type(exc).__name__)
        return 0, {}
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def load_seed_names(path=None) -> list[str]:
    try:
        with open(path or CHANNELS_PATH, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return []
    return [str(name).strip() for name in data.get("channels", []) if str(name).strip()]


def resolve_seeds(conn: sqlite3.Connection, client: httpx.Client, now: datetime, path=None) -> int:
    """Ищет на YouTube каналы по именам из video_channels.yaml. Берётся канал с точно таким же названием.
    Поиск стоит 100 единиц квоты, поэтому ищем каждое имя один раз, ненайденные повторяем раз в неделю."""
    found = 0
    for name in load_seed_names(path):
        if conn.execute("SELECT 1 FROM video_channels WHERE seed_name = ?", (name,)).fetchone():
            continue
        key = "seed_try:" + name
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row and row[0] > iso(now - timedelta(days=SEED_RETRY_DAYS)):
            continue
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, iso(now)))
        status, payload = _api(client, "search", {"part": "snippet", "type": "channel", "q": name,
                                                   "regionCode": config.VIDEO_REGION, "maxResults": 5})
        if status == 403:
            log.warning("YouTube API: поиск каналов отклонён (проверьте квоту)")
            conn.commit()
            return found
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            channel_id = snippet.get("channelId") or (item.get("id") or {}).get("channelId") or ""
            if channel_id.startswith("UC") and _norm(snippet.get("channelTitle", "")) == _norm(name):
                conn.execute(
                    "INSERT INTO video_channels(id, title, origin, seed_name, added_at) VALUES(?, ?, 'seed', ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET seed_name = excluded.seed_name",
                    (channel_id, clean_text(snippet.get("channelTitle", ""))[:120], name, iso(now)),
                )
                found += 1
                break
        else:
            log.info("YouTube: канал «%s» не найден", name)
        conn.commit()
    return found


def collect_channels(conn: sqlite3.Connection, client: httpx.Client, now: datetime) -> dict:
    """Свежие загрузки (за окно показа) всех включённых каналов: список загрузок канала и данные роликов."""
    stats = {"channel_videos": 0}
    since = iso(now - timedelta(hours=config.VIDEO_WINDOW_HOURS))
    ids: list[str] = []
    channels = conn.execute("SELECT id FROM video_channels WHERE enabled = 1 ORDER BY added_at LIMIT ?",
                            (MAX_CHANNELS,)).fetchall()
    for (channel_id,) in channels:
        if not channel_id.startswith("UC"):
            continue
        status, payload = _api(client, "playlistItems", {
            "part": "contentDetails", "playlistId": "UU" + channel_id[2:], "maxResults": 10})
        if status == 403:
            log.warning("YouTube API: доступ отклонён при чтении каналов")
            break
        for item in payload.get("items") or []:
            details = item.get("contentDetails") or {}
            if (details.get("videoPublishedAt") or "") >= since and VIDEO_ID.match(details.get("videoId", "")):
                ids.append(details["videoId"])
    for start in range(0, len(ids), 50):
        status, payload = _api(client, "videos", {
            "part": "snippet,statistics,contentDetails", "id": ",".join(ids[start:start + 50])})
        if status == 200:
            stats["channel_videos"] += save_videos(conn, parse_videos(payload), now, from_channel=1)
    conn.commit()
    return stats
