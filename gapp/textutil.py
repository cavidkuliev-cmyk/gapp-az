"""Нормализация ссылок и заголовков, определение языка, рубрики, slug."""
import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "ref", "from", "source", "amp"}

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

# Буквы азербайджанской латиницы и турецкого, которых нет в русском тексте.
_AZ_LETTERS = set("əğıöşüçƏĞİÖŞÜÇ")

STOP_WORDS = {
    "этот", "этого", "также", "после", "будет", "будут", "были", "было", "может",
    "более", "менее", "новый", "новая", "новые", "года", "году", "видео", "фото",
    "обновлено", "заявил", "заявила", "сообщил", "сообщили", "стало", "стали",
}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}

# Ключевые слова в рубриках источника и в адресах -> наша рубрика.
_RUBRIC_KEYWORDS = [
    ("sport", ("спорт", "футбол", "sport", "football")),
    ("proisshestviya", ("происшеств", "крими", "чп", "incident", "accident", "hadis")),
    ("ekonomika", ("эконом", "бизнес", "финанс", "econom", "business", "finance", "энерг")),
    ("politika", ("полит", "politic", "власть", "official", "официал")),
    ("kultura", ("культур", "cultur", "шоу", "искусств", "show")),
    ("nauka", ("наук", "технолог", "science", "tech", "hi-tech")),
    ("internet", ("интернет", "internet", "соцсет", "it")),
    ("avto", ("авто", "auto", "avto")),
    ("obshestvo", ("общест", "society", "social", "социум", "здоров", "образован")),
]


def clean_text(value: str | None) -> str:
    """Убирает HTML-разметку и лишние пробелы. Результат безопасен как простой текст."""
    if not value:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", value))
    return _SPACE_RE.sub(" ", text).strip()


def make_summary(value: str | None, limit: int = 300) -> str:
    """Короткий анонс не длиннее limit знаков, обрезанный по границе слова."""
    text = clean_text(value)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:-– ")
    return cut + "…"


def canonical_url(url: str) -> str:
    """Приводит ссылку к каноническому виду: без трекинговых параметров и якорей."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    host = parts.netloc.lower()
    if host.startswith("m."):
        host = host[2:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_PARAMS
    ]
    path = re.sub(r"/amp/?$", "/", parts.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(query), ""))


def title_tokens(title: str) -> set[str]:
    """Значимые слова заголовка в грубо усечённой форме (замена стеммингу)."""
    tokens = set()
    for word in _WORD_RE.findall(title.lower().replace("ё", "е")):
        if word.isdigit():
            tokens.add(word)
        elif len(word) >= 4 and word not in STOP_WORDS:
            tokens.add(word[:6])
    return tokens


def title_hash(title: str) -> str:
    """Хеш нормализованного заголовка: ловит ту же новость по другой ссылке."""
    norm = " ".join(_WORD_RE.findall(title.lower().replace("ё", "е")))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def similarity(a: set[str], b: set[str]) -> float:
    """Коэффициент Жаккара двух наборов слов."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_russian(text: str) -> bool:
    """True, если текст написан по-русски (кириллица, без азербайджанских букв)."""
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 5:
        return False
    if any(ch in _AZ_LETTERS for ch in letters):
        return False
    cyrillic = sum(1 for ch in letters if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    return cyrillic / len(letters) >= 0.6


def detect_rubric(categories: list[str], url: str = "") -> str:
    """Подбирает рубрику по рубрикам источника и адресу статьи."""
    haystacks = [c.lower() for c in categories if c]
    path = urlsplit(url).path.lower()
    for slug, keywords in _RUBRIC_KEYWORDS:
        for hay in haystacks:
            if any(k in hay for k in keywords if len(k) > 2):
                return slug
    segments = [s for s in path.split("/") if s]
    for slug, keywords in _RUBRIC_KEYWORDS:
        if any(seg.startswith(k) for seg in segments for k in keywords if len(k) > 3):
            return slug
    return "raznoe"


def slugify(title: str, limit: int = 70) -> str:
    """Адрес из заголовка: транслитерация в латиницу, слова через дефис."""
    out = []
    for ch in title.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    if len(slug) > limit:
        slug = slug[:limit].rsplit("-", 1)[0]
    return slug or "novost"
