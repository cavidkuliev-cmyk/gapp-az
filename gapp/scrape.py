"""Сбор новостей с сайтов без RSS: карты сайта и страницы разделов.

Три источника кандидатов:
  * карта сайта для Google News: в ней уже есть заголовок, дата и язык;
  * обычная карта сайта: только адреса и даты, остальное берём со страницы;
  * страница раздела: ссылки, подходящие под шаблон адреса статьи.
Данные статьи читаются из разметки, которую сайты делают для соцсетей
и поисковиков: Open Graph, JSON-LD, обычные meta-теги.
"""
import json
import re
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .textutil import clean_text, make_summary

_NS_NEWS = "{http://www.google.com/schemas/sitemap-news/0.9}"
_NS_IMAGE = "{http://www.google.com/schemas/sitemap-image/1.1}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_date(value: str | None) -> datetime | None:
    """Разбирает дату ISO 8601. Без часового пояса считает её UTC."""
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_sitemap(body: bytes) -> tuple[str, list[dict]]:
    """Возвращает ("index", вложенные карты) или ("urlset", кандидаты)."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "invalid", []
    kind = "index" if _local(root.tag) == "sitemapindex" else "urlset"
    items = []
    for node in root:
        entry = {"url": "", "date": None, "title": "", "lang": "", "image": ""}
        for child in node:
            name = _local(child.tag)
            if name == "loc":
                entry["url"] = (child.text or "").strip()
            elif name == "lastmod":
                entry["date"] = entry["date"] or parse_date(child.text)
            elif child.tag == _NS_NEWS + "news":
                for sub in child.iter():
                    sub_name = _local(sub.tag)
                    if sub_name == "title":
                        entry["title"] = clean_text(sub.text)
                    elif sub_name == "publication_date":
                        entry["date"] = parse_date(sub.text) or entry["date"]
                    elif sub_name == "language":
                        entry["lang"] = (sub.text or "").strip().lower()
            elif child.tag == _NS_IMAGE + "image":
                for sub in child.iter():
                    if _local(sub.tag) == "loc" and not entry["image"]:
                        entry["image"] = (sub.text or "").strip()
        if entry["url"].startswith(("http://", "https://")):
            items.append(entry)
    return kind, items


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.feeds: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "link" and a.get("href") and "alternate" in (a.get("rel") or "").lower():
            if (a.get("type") or "").lower() in ("application/rss+xml", "application/atom+xml"):
                self.feeds.append(a["href"])


def page_links(html: str, base_url: str) -> tuple[list[str], list[str]]:
    """Все ссылки страницы на тот же сайт и объявленные на ней ленты."""
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:  # кривая вёрстка не должна ронять сборщик
        pass
    host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    links, seen = [], set()
    for href in parser.links:
        url = urljoin(base_url, href).split("#", 1)[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower().removeprefix("www.") != host or url in seen:
            continue
        seen.add(url)
        links.append(url)
    return links, [urljoin(base_url, f) for f in parser.feeds]


def links_matching(html: str, base_url: str, pattern: str) -> list[dict]:
    regex = re.compile(pattern)
    links, _ = page_links(html, base_url)
    return [
        {"url": u, "date": None, "title": "", "lang": "", "image": ""}
        for u in links if regex.search(urlsplit(u).path)
    ]


def guess_pattern(links: list[str], minimum: int = 8) -> str:
    """Подбирает шаблон адреса статьи: самая частая форма пути с числом внутри."""
    shapes: dict[tuple, int] = {}
    for url in links:
        segments = [s for s in urlsplit(url).path.split("/") if s]
        if not segments or not any(re.search(r"\d{3,}", s) for s in segments):
            continue
        shape = []
        for seg in segments:
            if re.fullmatch(r"\d+", seg):
                shape.append(r"\d+")
            elif re.search(r"\d{3,}", seg) or len(seg) > 25:
                shape.append(r"[^/]+")
            else:
                shape.append(re.escape(seg))
        shapes[tuple(shape)] = shapes.get(tuple(shape), 0) + 1
    if not shapes:
        return ""
    best, count = max(shapes.items(), key=lambda kv: kv[1])
    # Статьи разных разделов отличаются одним сегментом пути: пробуем его обобщить.
    merged: dict[tuple, int] = {}
    for shape, n in shapes.items():
        for pos, seg in enumerate(shape):
            if seg not in (r"\d+", r"[^/]+"):
                key = shape[:pos] + (r"[^/]+",) + shape[pos + 1:]
                merged[key] = merged.get(key, 0) + n
    if merged:
        g_best, g_count = max(merged.items(), key=lambda kv: kv[1])
        if g_count >= count * 2:
            best, count = g_best, g_count
    return "^/" + "/".join(best) + "/?$" if count >= minimum else ""


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.lang = ""
        self.title = ""
        self.jsonld: list[str] = []
        self._in_title = False
        self._in_jsonld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = (a.get("lang") or "").lower()
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "title" and not self.title:
            self._in_title, self._buf = True, []
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_jsonld, self._buf = True, []

    def handle_data(self, data):
        if self._in_title or self._in_jsonld:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self.title, self._in_title = "".join(self._buf), False
        elif tag == "script" and self._in_jsonld:
            self.jsonld.append("".join(self._buf))
            self._in_jsonld = False


def _jsonld_articles(blocks: list[str]):
    for raw in blocks:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                stack.extend(node.get("@graph") or [])
                kind = node.get("@type")
                kinds = kind if isinstance(kind, list) else [kind]
                if any(isinstance(k, str) and k.endswith("Article") for k in kinds):
                    yield node


def article_meta(html: str) -> dict:
    """Заголовок, анонс, картинка, дата и язык из разметки страницы статьи."""
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    meta = parser.meta
    result = {
        "title": clean_text(meta.get("og:title") or meta.get("twitter:title") or ""),
        "summary": make_summary(meta.get("og:description") or meta.get("description") or ""),
        "image": meta.get("og:image") or meta.get("twitter:image") or "",
        "date": parse_date(
            meta.get("article:published_time") or meta.get("datepublished")
            or meta.get("og:article:published_time") or meta.get("pubdate")
        ),
        "lang": parser.lang or (meta.get("og:locale") or "").lower(),
        "type": (meta.get("og:type") or "").lower(),
    }
    for node in _jsonld_articles(parser.jsonld):
        if not result["title"] and isinstance(node.get("headline"), str):
            result["title"] = clean_text(node["headline"])
        if not result["date"] and isinstance(node.get("datePublished"), str):
            result["date"] = parse_date(node["datePublished"])
        if not result["summary"] and isinstance(node.get("description"), str):
            result["summary"] = make_summary(node["description"])
        result["type"] = result["type"] or "article"
    if not result["title"]:
        result["title"] = clean_text(parser.title)
    if not result["image"].startswith(("http://", "https://")):
        result["image"] = ""
    return result


_SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form",
              "figure", "figcaption", "button", "select", "iframe", "svg", "template"}
_BLOCK_TAGS = {"div", "article", "section", "main", "td", "body"}
_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col",
              "embed", "param", "track"}


class _BodyParser(HTMLParser):
    """Собирает абзацы <p> по блокам-родителям, чтобы найти блок с текстом статьи."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []   # (тег, номер блока или 0)
        self.blocks: dict[int, list[str]] = {}
        self._next = 0
        self._skip = 0
        self._para: list[str] | None = None

    def _block(self) -> int:
        for tag, block in reversed(self.stack):
            if block:
                return block
        return 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            if tag == "br" and self._para is not None:
                self._para.append(" ")
            return
        block = 0
        if tag in _BLOCK_TAGS:
            self._next += 1
            block = self._next
        self.stack.append((tag, block))
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "p" and not self._skip:
            self._flush()
            self._para = []

    def _flush(self):
        if self._para is not None:
            text = clean_text("".join(self._para))
            if len(text) >= 2:
                self.blocks.setdefault(self._block(), []).append(text)
        self._para = None

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if tag == "p":
            self._flush()
        for pos in range(len(self.stack) - 1, -1, -1):
            if self.stack[pos][0] == tag:
                for closed, _ in self.stack[pos:]:
                    if closed in _SKIP_TAGS:
                        self._skip -= 1
                    if closed == "p":
                        self._flush()
                del self.stack[pos:]
                break

    def handle_data(self, data):
        if self._para is not None and not self._skip:
            self._para.append(data)


def article_body(html: str, limit: int = 30000) -> str:
    """Полный текст статьи со страницы: абзацы через перевод строки или пустая строка.

    Сначала берётся articleBody из JSON-LD, иначе блок страницы, в котором
    больше всего текста в абзацах <p>.
    """
    meta = _MetaParser()
    try:
        meta.feed(html)
    except Exception:
        pass
    for node in _jsonld_articles(meta.jsonld):
        body = node.get("articleBody")
        if isinstance(body, str) and len(clean_text(body)) >= 200:
            parts = [clean_text(part) for part in re.split(r"(?:\r?\n)+|</p>", body)]
            return "\n".join(part for part in parts if part)[:limit]
    parser = _BodyParser()
    try:
        parser.feed(html)
        parser._flush()
    except Exception:
        pass
    if not parser.blocks:
        return ""
    best = max(parser.blocks.values(), key=lambda paras: sum(len(p) for p in paras))
    text = "\n".join(best)
    return text[:limit] if len(text) >= 200 else ""


def feed_body(value: str | None, limit: int = 30000) -> str:
    """Полный текст из поля ленты (content:encoded, turbo:content, yandex:full-text)."""
    if not value:
        return ""
    value = re.sub(r"<(header|figure|script|style)\b.*?</\1\s*>", " ", value, flags=re.S | re.I)
    if "<p" in value.lower():
        paras = [clean_text(part) for part in re.split(r"</p\s*>|<br\s*/?>\s*<br\s*/?>", value, flags=re.I)]
    else:
        paras = [clean_text(part) for part in re.split(r"(?:\r?\n)+", value)]
    text = "\n".join(part for part in paras if part)
    return text[:limit] if len(text) >= 200 else ""


class Robots:
    """Правила robots.txt по сайтам. Запрещённые адреса сборщик не запрашивает."""

    def __init__(self, client, user_agent: str):
        self.client, self.agent = client, user_agent
        self.cache: dict[str, urllib.robotparser.RobotFileParser | bool] = {}

    def _load(self, origin: str):
        try:
            resp = self.client.get(origin + "/robots.txt")
        except Exception:
            return True  # robots.txt недоступен: ограничений не знаем, считаем разрешённым
        if resp.status_code in (401, 403):
            return False
        if resp.status_code != 200:
            return True
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.cache:
            self.cache[origin] = self._load(origin)
        rules = self.cache[origin]
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self.agent, url)

    def sitemaps(self, url: str) -> list[str]:
        parts = urlsplit(url)
        self.allowed(url)
        rules = self.cache.get(f"{parts.scheme}://{parts.netloc}")
        if isinstance(rules, bool) or rules is None:
            return []
        return list(rules.site_maps() or [])
