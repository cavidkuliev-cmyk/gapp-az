"""Хранилище на SQLite: источники, статьи, сюжеты."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    site TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    url TEXT NOT NULL UNIQUE,
    etag TEXT,
    last_modified TEXT,
    last_checked TEXT,
    last_status TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    rubric TEXT NOT NULL DEFAULT 'raznoe',
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 1,
    article_count INTEGER NOT NULL DEFAULT 1,
    main_source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS stories_last_at ON stories(last_at DESC);
CREATE INDEX IF NOT EXISTS stories_rubric ON stories(rubric, last_at DESC);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    story_id INTEGER NOT NULL REFERENCES stories(id),
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    title_hash TEXT NOT NULL,
    tokens TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    rubric TEXT NOT NULL DEFAULT 'raznoe',
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS articles_source_title ON articles(source_id, title_hash);
CREATE INDEX IF NOT EXISTS articles_story ON articles(story_id);
CREATE INDEX IF NOT EXISTS articles_published ON articles(published_at DESC);
CREATE TABLE IF NOT EXISTS seen_urls (
    url TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Добавляет столбцы, появившиеся после первого запуска."""
    for table, name, ddl in (
        ("feeds", "kind", "TEXT NOT NULL DEFAULT 'rss'"),    # rss, sitemap или html
        ("feeds", "pattern", "TEXT NOT NULL DEFAULT ''"),    # шаблон адреса статьи для html
        ("sources", "lang", "TEXT NOT NULL DEFAULT 'ru'"),   # язык источника: ru или az
        ("sources", "title", "TEXT NOT NULL DEFAULT ''"),    # имя для показа, если отличается от name
        ("stories", "lang", "TEXT NOT NULL DEFAULT 'ru'"),
        # Полный текст статьи: хранится для анализа, на сайте не показывается.
        ("articles", "body", "TEXT NOT NULL DEFAULT ''"),
        # '' ещё не пробовали, feed из ленты, page со страницы, blocked сайт не пускает, failed не вышло
        ("articles", "body_status", "TEXT NOT NULL DEFAULT ''"),
        # Фото и видео статьи: JSON-список {"type": "image"|"video", "url": ...}. Только адреса.
        ("articles", "media", "TEXT NOT NULL DEFAULT '[]'"),
        # Показывать ли полный текст статей источника на сайте (sources.yaml: full_text)
        ("sources", "show_text", "INTEGER NOT NULL DEFAULT 1"),
        # Ручные настройки из админки: NULL значит «как в sources.yaml».
        ("sources", "admin_active", "INTEGER"),
        ("sources", "admin_show_text", "INTEGER"),
        # Сюжет скрыт с сайта вручную (админка).
        ("stories", "hidden", "INTEGER NOT NULL DEFAULT 0"),
    ):
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS stories_lang ON stories(lang, last_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS articles_body_todo ON articles(published_at DESC) "
                 "WHERE body_status = ''")
    conn.commit()


@contextmanager
def session(path: Path | str | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
