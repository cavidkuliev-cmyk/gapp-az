"""Сайт: серверный HTML без клиентского JavaScript."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import config, db

BAKU = timezone(timedelta(hours=4))
PAGE_SIZE = 60

app = FastAPI(title="Gapp.az", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def short_time(value: str) -> str:
    """Время по Баку: часы и минуты для сегодняшних новостей, иначе дата."""
    dt = parse_iso(value).astimezone(BAKU)
    if dt.date() == datetime.now(BAKU).date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d.%m")


def full_time(value: str) -> str:
    return parse_iso(value).astimezone(BAKU).strftime("%d.%m.%Y, %H:%M")


def baku_iso(value: str) -> str:
    return parse_iso(value).astimezone(BAKU).isoformat()


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {few}"
    return f"{n} {many}"


def sources_label(n: int, lang: str = "ru") -> str:
    if lang == "az":
        return f"{n} mənbə"
    return plural(n, "источник", "источника", "источников")


def story_path(story) -> str:
    return f"{config.I18N[story['lang']]['story']}{story['slug']}-{story['id']}"


templates.env.filters["short_time"] = short_time
templates.env.filters["full_time"] = full_time
templates.env.filters["baku_iso"] = baku_iso
templates.env.filters["sources_label"] = sources_label
templates.env.globals.update(site_url=config.SITE_URL, story_path=story_path, i18n=config.I18N)


def render(request: Request, name: str, lang: str = "ru", **ctx) -> HTMLResponse:
    ctx.update(lang=lang, L=config.I18N[lang])
    response = templates.TemplateResponse(request, name, ctx)
    response.headers["Cache-Control"] = "public, max-age=60"
    return response


def iso_ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).strftime("%Y-%m-%dT%H:%M:%SZ")


def story_list(conn, lang: str, rubric: str | None, page: int):
    where, params = "WHERE lang = ?", [lang]
    if rubric:
        where, params = where + " AND rubric = ?", [lang, rubric]
    rows = conn.execute(
        f"SELECT * FROM stories {where} ORDER BY last_at DESC LIMIT ? OFFSET ?",
        (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def paged(path: str, page: int) -> str:
    return path if page == 1 else f"{path}?page={page}"


def home_page(request: Request, lang: str, page: int):
    L = config.I18N[lang]
    with db.session() as conn:
        top = conn.execute(
            "SELECT * FROM stories WHERE lang = ? AND last_at >= ? AND source_count >= 2 "
            "ORDER BY source_count DESC, last_at DESC LIMIT 8",
            (lang, iso_ago(hours=24)),
        ).fetchall()
        feed, has_next = story_list(conn, lang, None, page)
        sources = conn.execute(
            "SELECT COALESCE(NULLIF(title, ''), name) AS name, site FROM sources "
            "WHERE active = 1 AND lang = ? ORDER BY name",
            (lang,),
        ).fetchall()
    return render(
        request, "home.html", lang, top=top, feed=feed, sources=sources, page=page,
        has_next=has_next, base_path=L["home"], title=L["home_title"], description=L["home_desc"],
        canonical=paged(L["home"], page),
        alternates={code: config.I18N[code]["home"] for code in config.LANGS} if page == 1 else None,
    )


def rubric_page(request: Request, lang: str, slug: str, page: int):
    L = config.I18N[lang]
    if slug not in L["rubric_names"]:
        raise HTTPException(404)
    name = L["rubric_names"][slug]
    path = L["rubric"] + slug
    with db.session() as conn:
        feed, has_next = story_list(conn, lang, slug, page)
    return render(
        request, "list.html", lang, feed=feed, page=page, has_next=has_next, heading=name,
        base_path=path, active_rubric=slug,
        title=L["rubric_title"].format(name=name), description=L["rubric_desc"].format(name=name),
        canonical=paged(path, page),
        alternates={code: config.I18N[code]["rubric"] + slug for code in config.LANGS} if page == 1 else None,
    )


def story_page(request: Request, slug_id: str):
    """Страница сюжета. Язык берётся из самого сюжета, чужой адрес перенаправляется."""
    slug, _, raw_id = slug_id.rpartition("-")
    if not raw_id.isdigit():
        raise HTTPException(404)
    with db.session() as conn:
        item = conn.execute("SELECT * FROM stories WHERE id = ?", (int(raw_id),)).fetchone()
        if item is None:
            raise HTTPException(404)
        if request.url.path != story_path(item):
            return RedirectResponse(story_path(item), status_code=301)
        articles = conn.execute(
            "SELECT a.*, COALESCE(NULLIF(s.title, ''), s.name) AS source_name "
            "FROM articles a JOIN sources s ON s.id = a.source_id "
            "WHERE a.story_id = ? ORDER BY a.published_at",
            (item["id"],),
        ).fetchall()
        related = conn.execute(
            "SELECT * FROM stories WHERE lang = ? AND rubric = ? AND id != ? ORDER BY last_at DESC LIMIT 8",
            (item["lang"], item["rubric"], item["id"]),
        ).fetchall()
    indexable = item["source_count"] >= config.INDEX_MIN_SOURCES
    return render(
        request, "story.html", item["lang"], story=item, articles=articles, related=related,
        indexable=indexable, active_rubric=item["rubric"],
        title=f"{item['title']} | Gapp.az",
        description=item["summary"] or item["title"],
        canonical=story_path(item),
        robots="index, follow, max-image-preview:large" if indexable else "noindex, follow",
    )


def search_page(request: Request, lang: str, q: str):
    L = config.I18N[lang]
    q = q.strip()
    feed = []
    if len(q) >= 3:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with db.session() as conn:
            feed = conn.execute(
                "SELECT * FROM stories WHERE lang = ? AND title LIKE ? ESCAPE '\\' "
                "ORDER BY last_at DESC LIMIT 100",
                (lang, like),
            ).fetchall()
    return render(
        request, "list.html", lang, feed=feed, page=1, has_next=False, query=q,
        heading=f"{L['search_word']}: {q}" if q else L["search_word"], base_path=L["search"],
        title=L["search_title"], description=L["search_desc"],
        canonical=L["search"], robots="noindex, follow",
    )


def about_page(request: Request, lang: str):
    L = config.I18N[lang]
    return render(
        request, "about.html", lang, title=L["about_title"], description=L["about_desc"],
        canonical=L["about"],
        alternates={code: config.I18N[code]["about"] for code in config.LANGS},
    )


PAGE = Query(1, ge=1, le=500)
Q = Query("", max_length=100)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, page: int = PAGE):
    return home_page(request, "ru", page)


@app.get("/az", response_class=HTMLResponse)
def home_az(request: Request, page: int = PAGE):
    return home_page(request, "az", page)


@app.get("/rubrika/{slug}", response_class=HTMLResponse)
def rubric(request: Request, slug: str, page: int = PAGE):
    return rubric_page(request, "ru", slug, page)


@app.get("/az/rubrika/{slug}", response_class=HTMLResponse)
def rubric_az(request: Request, slug: str, page: int = PAGE):
    return rubric_page(request, "az", slug, page)


@app.get("/novost/{slug_id}", response_class=HTMLResponse)
@app.get("/az/xeber/{slug_id}", response_class=HTMLResponse)
def story(request: Request, slug_id: str):
    return story_page(request, slug_id)


@app.get("/poisk", response_class=HTMLResponse)
def search(request: Request, q: str = Q):
    return search_page(request, "ru", q)


@app.get("/az/axtar", response_class=HTMLResponse)
def search_az(request: Request, q: str = Q):
    return search_page(request, "az", q)


@app.get("/o-proekte", response_class=HTMLResponse)
def about(request: Request):
    return about_page(request, "ru")


@app.get("/az/haqqinda", response_class=HTMLResponse)
def about_az(request: Request):
    return about_page(request, "az")


@app.get("/bot", response_class=HTMLResponse)
def bot(request: Request):
    return render(
        request, "about.html", title="О боте GappBot | Gapp.az",
        description="GappBot читает открытые RSS-ленты изданий для агрегатора Gapp.az.",
        canonical="/bot",
    )


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return (
        "User-agent: *\nDisallow: /poisk\nDisallow: /az/axtar\n\n"
        f"Sitemap: {config.SITE_URL}/sitemap.xml\nSitemap: {config.SITE_URL}/news-sitemap.xml\n"
    )


def _xml(body: str) -> Response:
    return Response(body, media_type="application/xml", headers={"Cache-Control": "public, max-age=300"})


@app.get("/sitemap.xml")
def sitemap():
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id, slug, lang, last_at FROM stories WHERE source_count >= ? ORDER BY last_at DESC LIMIT 20000",
            (config.INDEX_MIN_SOURCES,),
        ).fetchall()
    urls = []
    for code in config.LANGS:
        L = config.I18N[code]
        urls.append(f"<url><loc>{config.SITE_URL}{L['home']}</loc></url>")
        urls += [f"<url><loc>{config.SITE_URL}{L['rubric']}{slug}</loc></url>" for slug, _ in L["rubrics"]]
    urls += [
        f"<url><loc>{config.SITE_URL}{escape(story_path(r))}</loc><lastmod>{r['last_at']}</lastmod></url>"
        for r in rows
    ]
    return _xml(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(urls) + "</urlset>"
    )


@app.get("/news-sitemap.xml")
def news_sitemap():
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM stories WHERE source_count >= ? AND first_at >= ? "
            "ORDER BY first_at DESC LIMIT 1000",
            (config.INDEX_MIN_SOURCES, iso_ago(hours=48)),
        ).fetchall()
    urls = [
        f"<url><loc>{config.SITE_URL}{escape(story_path(r))}</loc><news:news><news:publication>"
        f"<news:name>Gapp.az</news:name><news:language>{r['lang']}</news:language></news:publication>"
        f"<news:publication_date>{r['first_at']}</news:publication_date>"
        f"<news:title>{escape(r['title'])}</news:title></news:news></url>"
        for r in rows
    ]
    return _xml(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">' + "".join(urls) + "</urlset>"
    )


def rss_feed(lang: str) -> Response:
    L = config.I18N[lang]
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM stories WHERE lang = ? ORDER BY last_at DESC LIMIT 50", (lang,)
        ).fetchall()
    items = [
        f"<item><title>{escape(r['title'])}</title><link>{config.SITE_URL}{escape(story_path(r))}</link>"
        f"<guid>{config.SITE_URL}{escape(story_path(r))}</guid>"
        f"<pubDate>{format_datetime(parse_iso(r['first_at']))}</pubDate>"
        f"<description>{escape(r['summary'])}</description></item>"
        for r in rows
    ]
    return _xml(
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Gapp.az</title><link>{config.SITE_URL}{L['home']}</link>"
        f"<description>{L['rss_desc']}</description><language>{lang}</language>"
        + "".join(items) + "</channel></rss>"
    )


@app.get("/rss.xml")
def rss():
    return rss_feed("ru")


@app.get("/az/rss.xml")
def rss_az():
    return rss_feed("az")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    with db.session() as conn:
        conn.execute("SELECT 1").fetchone()
    return "ok"
