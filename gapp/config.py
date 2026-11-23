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

# Показывать ли на страницах новостей полный текст, фото и видео. Общий выключатель;
# отдельному источнику показ отключается в sources.yaml строкой `full_text: false`.
SHOW_FULL_TEXT = os.environ.get("GAPP_SHOW_FULL_TEXT", "1") not in ("0", "false", "no", "")

# Сколько страниц статей можно скачать с одного сайта за обход (для сайтов без RSS).
PAGE_FETCH_LIMIT = int(os.environ.get("GAPP_PAGE_FETCH_LIMIT", "10"))

# Сохранять ли полный текст статей в базе (для анализа, на сайте не показывается).
FETCH_BODIES = os.environ.get("GAPP_FETCH_BODIES", "1") not in ("0", "false", "no", "")
# Сколько страниц статей можно скачать ради текста за обход: с одного сайта и всего.
BODY_FETCH_PER_SITE = int(os.environ.get("GAPP_BODY_FETCH_PER_SITE", "4"))
BODY_FETCH_PER_PASS = int(os.environ.get("GAPP_BODY_FETCH_PER_PASS", "40"))
# Текст добирается только для статей не старше этого числа часов.
BODY_MAX_AGE_HOURS = int(os.environ.get("GAPP_BODY_MAX_AGE_HOURS", "48"))

# --- Языковые версии сайта: русская в корне, азербайджанская в /az ---
LANGS = ("ru", "az")

RUBRICS_AZ = [
    ("politika", "Siyasət"),
    ("obshestvo", "Cəmiyyət"),
    ("proisshestviya", "Hadisə"),
    ("ekonomika", "İqtisadiyyat"),
    ("kultura", "Mədəniyyət"),
    ("nauka", "Elm və texnologiya"),
    ("sport", "İdman"),
    ("internet", "İnternet"),
    ("avto", "Avto"),
    ("raznoe", "Digər"),
]

I18N = {
    "ru": {
        "html_lang": "ru", "locale": "ru_RU", "other": "az",
        "rubrics": RUBRICS, "rubric_names": RUBRIC_NAMES,
        # адреса разделов
        "home": "/", "rubric": "/rubrika/", "story": "/novost/", "source": "/istochnik/", "search": "/poisk",
        "about": "/o-proekte", "rss": "/rss.xml",
        # тексты
        "home_title": "Gapp.az: новости Азербайджана из всех изданий",
        "home_desc": "Свежие новости Азербайджана на русском языке из ведущих изданий в одной ленте.",
        "rubric_title": "{name}: новости Азербайджана | Gapp.az",
        "rubric_desc": "Новости рубрики «{name}» из азербайджанских изданий на русском языке.",
        "rss_desc": "Новости Азербайджана из всех изданий",
        "search_title": "Поиск по новостям | Gapp.az", "search_desc": "Поиск по новостям Gapp.az.",
        "about_title": "О проекте | Gapp.az",
        "about_desc": "Что такое Gapp.az, как собираются новости и как связаться с нами.",
        "logo_label": "Gapp.az, на главную", "nav_label": "Рубрики",
        "search_word": "Поиск", "search_placeholder": "по новостям", "find": "Найти",
        "top": "Главные новости", "top_note": "Сюжеты, о которых сейчас пишет больше всего изданий",
        "all": "Все новости", "sources": "Источники", "empty": "Пока здесь нет новостей.",
        "pages": "Страницы", "newer": "Новее", "more": "Показать ещё", "also": "ещё",
        "min_query": "Введите не меньше трёх букв.", "main": "Главная", "crumbs": "Навигация",
        "who": "Кто об этом пишет", "read_at": "Читать на сайте {name}", "photo": "Фото",
        "related_label": "Ещё по теме", "related": "{name}: ещё новости",
        "source_word": "Источник", "original": "оригинал статьи", "site_link": "Сайт издания",
        "source_title": "{name}: все новости | Gapp.az",
        "source_desc": "Все новости издания {name}, собранные на Gapp.az.",
        "foot": "Gapp.az собирает новости азербайджанских изданий и ведёт на сайт источника",
        "about_link": "О проекте", "rights": "Правообладателям", "bot": "О боте",
    },
    "az": {
        "html_lang": "az", "locale": "az_AZ", "other": "ru",
        "rubrics": RUBRICS_AZ, "rubric_names": dict(RUBRICS_AZ),
        "home": "/az", "rubric": "/az/rubrika/", "story": "/az/xeber/", "source": "/az/menbe/", "search": "/az/axtar",
        "about": "/az/haqqinda", "rss": "/az/rss.xml",
        "home_title": "Gapp.az: bütün nəşrlərdən Azərbaycan xəbərləri",
        "home_desc": "Aparıcı nəşrlərdən ən son Azərbaycan xəbərləri bir lentdə.",
        "rubric_title": "{name}: Azərbaycan xəbərləri | Gapp.az",
        "rubric_desc": "Azərbaycan nəşrlərindən «{name}» rubrikasının xəbərləri.",
        "rss_desc": "Bütün nəşrlərdən Azərbaycan xəbərləri",
        "search_title": "Xəbərlərdə axtarış | Gapp.az", "search_desc": "Gapp.az xəbərlərində axtarış.",
        "about_title": "Layihə haqqında | Gapp.az",
        "about_desc": "Gapp.az nədir, xəbərlər necə toplanır və bizimlə necə əlaqə saxlamaq olar.",
        "logo_label": "Gapp.az, ana səhifə", "nav_label": "Rubrikalar",
        "search_word": "Axtarış", "search_placeholder": "xəbərlərdə", "find": "Axtar",
        "top": "Əsas xəbərlər", "top_note": "Hazırda ən çox nəşrin yazdığı mövzular",
        "all": "Bütün xəbərlər", "sources": "Mənbələr", "empty": "Hələlik burada xəbər yoxdur.",
        "pages": "Səhifələr", "newer": "Daha yeni", "more": "Daha çox", "also": "daha",
        "min_query": "Ən azı üç hərf daxil edin.", "main": "Ana səhifə", "crumbs": "Naviqasiya",
        "who": "Bu barədə kim yazır", "read_at": "{name} saytında oxu", "photo": "Foto",
        "related_label": "Mövzu üzrə", "related": "{name}: digər xəbərlər",
        "source_word": "Mənbə", "original": "orijinal məqalə", "site_link": "Nəşrin saytı",
        "source_title": "{name}: bütün xəbərlər | Gapp.az",
        "source_desc": "Gapp.az-da toplanmış {name} nəşrinin bütün xəbərləri.",
        "foot": "Gapp.az Azərbaycan nəşrlərinin xəbərlərini toplayır və mənbənin saytına yönləndirir",
        "about_link": "Layihə haqqında", "rights": "Müəllif hüququ sahiblərinə", "bot": "Bot haqqında",
    },
}
