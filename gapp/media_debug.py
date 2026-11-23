"""Диагностика фото и видео статьи: python -m gapp.media_debug <адрес статьи>

Показывает, что находит сборщик, и все теги картинок страницы с их окружением,
чтобы понять, почему фото не взялись или взялись лишние. Файлы не скачиваются.
"""
import re
import sys

import httpx

from . import config, scrape


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


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Использование: python -m gapp.media_debug <адрес статьи>")
    main(sys.argv[1])
