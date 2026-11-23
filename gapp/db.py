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
    have = {row[1] for row in conn.execute("PRAGMA table_info(stories)")}
    for name, ddl in (
        ("digest", "TEXT NOT NULL DEFAULT ''"),        # пересказ, написанный моделью
        ("digest_at", "TEXT NOT NULL DEFAULT ''"),     # когда написан или когда была попытка
        ("digest_sources", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in have:
            conn.execute(f"ALTER TABLE stories ADD COLUMN {name} {ddl}")
    have = {row[1] for row in conn.execute("PRAGMA table_info(feeds)")}
    for name, ddl in (
        ("kind", "TEXT NOT NULL DEFAULT 'rss'"),   # rss, sitemap или html
        ("pattern", "TEXT NOT NULL DEFAULT ''"),   # шаблон адреса статьи для html
    ):
        if name not in have:
            conn.execute(f"ALTER TABLE feeds ADD COLUMN {name} {ddl}")
    conn.commit()


@contextmanager
def session(path: Path | str | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
