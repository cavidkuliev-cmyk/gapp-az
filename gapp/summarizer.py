"""Короткие пересказы сюжетов через Claude API.

Модель видит только заголовки и анонсы из лент, полных текстов у нас нет.
Пересказ пишется один раз на сюжет, когда о нём сообщили минимум
SUMMARY_MIN_SOURCES изданий.
"""
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import anthropic

from . import config

log = logging.getLogger("gapp.summarizer")

SYSTEM = (
    "Ты редактор новостного агрегатора Gapp.az. Тебе дают заголовки и анонсы "
    "нескольких изданий об одном событии. Напиши пересказ на русском языке: "
    "два или три предложения, до 450 знаков, нейтрально, без оценок и без "
    "обращений к читателю. Используй только факты из присланных материалов, "
    "ничего не добавляй от себя. Если издания расходятся в цифрах или деталях, "
    "скажи об этом одной фразой. Не повторяй дословно ни один заголовок и не "
    "упоминай названия изданий. Ответ состоит только из текста пересказа."
)


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_prompt(articles) -> str:
    parts = []
    for n, a in enumerate(articles, 1):
        parts.append(f"Материал {n}\nЗаголовок: {a['title']}\nАнонс: {a['summary'] or 'нет'}")
    return "\n\n".join(parts)


def summarize_story(client, articles) -> str:
    """Возвращает текст пересказа или пустую строку, если модель его не дала."""
    response = client.messages.create(
        model=config.SUMMARY_MODEL,
        max_tokens=2000,
        system=SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": build_prompt(articles)}],
    )
    if response.stop_reason != "end_turn":
        log.warning("пересказ не получен: stop_reason=%s", response.stop_reason)
        return ""
    text = " ".join(b.text for b in response.content if b.type == "text").strip()
    return " ".join(text.split())[:700]


def summarize_pending(conn: sqlite3.Connection, client=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stats = {"written": 0, "failed": 0, "skipped_limit": 0}
    done_today = conn.execute(
        "SELECT COUNT(*) FROM stories WHERE digest_at >= ?", (_iso(now - timedelta(hours=24)),)
    ).fetchone()[0]
    budget = min(config.SUMMARY_PER_PASS, config.SUMMARY_DAILY_LIMIT - done_today)
    pending = conn.execute(
        "SELECT id FROM stories WHERE digest_at = '' AND source_count >= ? AND last_at >= ? "
        "ORDER BY source_count DESC, last_at DESC",
        (config.SUMMARY_MIN_SOURCES, _iso(now - timedelta(hours=config.STORY_WINDOW_HOURS))),
    ).fetchall()
    if budget <= 0:
        stats["skipped_limit"] = len(pending)
        return stats
    client = client or anthropic.Anthropic()
    for row in pending[:budget]:
        articles = conn.execute(
            "SELECT title, summary FROM articles WHERE story_id = ? ORDER BY published_at LIMIT 8",
            (row["id"],),
        ).fetchall()
        try:
            text = summarize_story(client, articles)
        except anthropic.RateLimitError:
            log.warning("пересказы: превышен лимит запросов, продолжим в следующий обход")
            break
        except anthropic.APIStatusError as exc:
            log.warning("пересказ сюжета %s: ошибка API %s", row["id"], exc.status_code)
            if exc.status_code in (401, 403):
                break  # ключ не принят, повторять бессмысленно
            text = ""
        except anthropic.APIConnectionError:
            log.warning("пересказы: нет связи с API")
            break
        # digest_at ставим и при неудаче, чтобы не платить за один сюжет бесконечно
        conn.execute(
            "UPDATE stories SET digest = ?, digest_at = ?, digest_sources = ? WHERE id = ?",
            (text, _iso(now), len(articles), row["id"]),
        )
        conn.commit()
        stats["written" if text else "failed"] += 1
    stats["skipped_limit"] = max(0, len(pending) - budget)
    return stats
