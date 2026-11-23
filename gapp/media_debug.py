"""Диагностика фото и видео статьи: python -m gapp.media_debug <адрес статьи>

Показывает, что находит сборщик, и все теги картинок страницы с их окружением,
чтобы понять, почему фото не взялись или взялись лишние. Файлы не скачиваются.

Команда `python -m gapp.media_debug --reset` ставит свежим (до 48 часов) статьям с текстом
со страницы метку «собрать заново»: сборщик сам повторно возьмёт у них текст и фото.
"""
import re
import sys

import httpx

from . import config, db, scrape


def main(url: str) -> None:
    with httpx.Client(headers={"User-Agent": config.USER_AGENT}, timeout=20, follow_redirects=True) as client:
        resp = client.get(url)
    print(f"HTTP {resp.status_code}, {len(resp.text)} символов")
    text, media = scrape.article_content(resp.text, str(resp.url))
    print(f"Текст: {len(text)} символов")
    print(f"Найдено фото и видео: {len(media)}")
    for item in media:
        print("  ", item["type"], item["url"])
    tags = re.findall(r"<(?:img|video|iframe|source)\b[^>]*>", resp.text, flags=re.I)
    print(f"\nВсего тегов img/video/iframe/source на странице: {len(tags)}")
    for tag in tags[:25]:
        print("  ", tag[:230])
    ld = re.findall(r'"(?:image|contentUrl|embedUrl)"\s*:\s*"([^"]+)"', resp.text)
    print(f"\nАдреса в JSON-LD: {len(ld)}")
    for value in ld[:10]:
        print("  ", value)


def reset() -> None:
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(hours=config.BODY_MAX_AGE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.session() as conn:
        cur = conn.execute(
            "UPDATE articles SET body_status = '' WHERE body_status = 'page' AND published_at >= ?", (since,))
        print(f"Поставлено на повторный сбор: {cur.rowcount} статей")


if __name__ == "__main__":
    if sys.argv[1:] == ["--reset"]:
        reset()
        sys.exit(0)
    if len(sys.argv) != 2:
        sys.exit("Использование: python -m gapp.media_debug <адрес статьи>")
    main(sys.argv[1])
