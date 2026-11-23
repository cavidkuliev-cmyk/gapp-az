import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("GAPP_DB", BASE_DIR / "data" / "gapp.db"))
SOURCES_PATH = Path(os.environ.get("GAPP_SOURCES", BASE_DIR / "sources.yaml"))
SITE_URL = os.environ.get("GAPP_SITE_URL", "https://gapp.az").rstrip("/")
USER_AGENT = os.environ.get("GAPP_USER_AGENT", "GappBot/1.0 (+https://gapp.az/bot)")
# Пауза между полными обходами лент, секунды.
POLL_INTERVAL = int(os.environ.get("GAPP_POLL_INTERVAL", "180"))
# Окно, в котором новости склеиваются в один сюжет, часы.
STORY_WINDOW_HOURS = int(os.environ.get("GAPP_STORY_WINDOW_HOURS", "48"))
# Порог сходства заголовков (коэффициент Жаккара) для склейки в сюжет.
STORY_SIMILARITY = float(os.environ.get("GAPP_STORY_SIMILARITY", "0.5"))
# Сколько источников нужно сюжету, чтобы страница индексировалась.
INDEX_MIN_SOURCES = int(os.environ.get("GAPP_INDEX_MIN_SOURCES", "2"))

RUBRICS = [
    ("politika", "Политика"),
    ("obshestvo", "Общество"),
    ("proisshestviya", "Происшествия"),
    ("ekonomika", "Экономика"),
    ("kultura", "Культура"),
    ("nauka", "Наука и технологии"),
    ("sport", "Спорт"),
    ("internet", "Интернет"),
    ("avto", "Авто"),
    ("raznoe", "Разное"),
]
RUBRIC_NAMES = dict(RUBRICS)
