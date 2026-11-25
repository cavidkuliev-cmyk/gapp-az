"""Админка /admin: источники, статистика, скрытие новостей. Только по паролю, без JavaScript.

Пароль задаётся переменной GAPP_ADMIN_PASSWORD (не короче 12 символов). Пока он не задан,
все адреса /admin отвечают 404. На публичные страницы и сборщик админка не влияет.
"""
import hashlib
import hmac
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MIN_PASSWORD = 12
SESSION_SECONDS = 7 * 24 * 3600
COOKIE = "gapp_admin"
_failures: dict[str, list[float]] = {}
MAX_FAILURES, FAILURE_WINDOW = 5, 600


def _enabled() -> bool:
    return len(config.ADMIN_PASSWORD) >= MIN_PASSWORD


def _key() -> bytes:
    return hashlib.sha256(("gapp-admin|" + config.ADMIN_PASSWORD).encode()).digest()


def _sign(text: str) -> str:
    return hmac.new(_key(), text.encode(), hashlib.sha256).hexdigest()


def _new_session() -> str:
    expires = str(int(time.time()) + SESSION_SECONDS)
    return f"{expires}.{_sign('session|' + expires)}"


def _session_ok(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    expires, _, sig = value.partition(".")
    return (expires.isdigit() and int(expires) > time.time()
            and hmac.compare_digest(sig, _sign("session|" + expires)))


def _csrf(session: str) -> str:
    return _sign("csrf|" + session)[:32]


def _ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",")[0].strip() or (request.client.host if request.client else "?"))


def _page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    response = templates.TemplateResponse(request, name, ctx, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _redirect(to: str = "/admin") -> RedirectResponse:
    response = RedirectResponse(to, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


async def _form(request: Request) -> dict[str, str]:
    data = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[0] for key, values in data.items()}


def _authorized(request: Request, form: dict[str, str] | None = None) -> str:
    """Возвращает сессию. Без входа 404 (админку не видно), с чужим токеном 403."""
    if not _enabled():
        raise HTTPException(404)
    session = request.cookies.get(COOKIE)
    if not _session_ok(session):
        raise HTTPException(401)
    if form is not None and not hmac.compare_digest(form.get("csrf", ""), _csrf(session)):
        raise HTTPException(403)
    return session


def _stats(conn) -> dict:
    day = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    rows = conn.execute(
        "SELECT s.id, s.name, COALESCE(NULLIF(s.title, ''), s.name) AS shown, s.lang, s.site, "
        "s.active, s.show_text, s.admin_active, s.admin_show_text, "
        "(SELECT COUNT(*) FROM articles a WHERE a.source_id = s.id) AS total, "
        "(SELECT COUNT(*) FROM articles a WHERE a.source_id = s.id AND a.fetched_at >= ?) AS day, "
        "(SELECT COUNT(*) FROM articles a WHERE a.source_id = s.id AND a.body != '') AS with_text, "
        "(SELECT MAX(a.fetched_at) FROM articles a WHERE a.source_id = s.id) AS last_at "
        "FROM sources s ORDER BY s.lang, shown COLLATE NOCASE",
        (day,),
    ).fetchall()
    totals = conn.execute(
        "SELECT (SELECT COUNT(*) FROM stories), (SELECT COUNT(*) FROM stories WHERE hidden = 1), "
        "(SELECT COUNT(*) FROM articles), (SELECT MAX(fetched_at) FROM articles), "
        "(SELECT COALESCE(SUM(LENGTH(body)), 0) FROM articles)"
    ).fetchone()
    try:
        db_size = Path(config.DB_PATH).stat().st_size
        disk = shutil.disk_usage(Path(config.DB_PATH).parent)
        disk_info = {"free": disk.free, "total": disk.total}
    except OSError:
        db_size, disk_info = 0, {"free": 0, "total": 0}
    return {"sources": rows, "stories": totals[0], "hidden": totals[1], "articles": totals[2],
            "last_fetch": totals[3], "text_bytes": totals[4], "db_size": db_size, "disk": disk_info}


def _mb(value: int) -> str:
    return f"{value / 1048576:.1f} МБ" if value < 1073741824 else f"{value / 1073741824:.2f} ГБ"


templates.env.filters["mb"] = _mb


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, q: str = ""):
    if not _enabled():
        raise HTTPException(404)
    session = request.cookies.get(COOKIE)
    if not _session_ok(session):
        return _page(request, "admin_login.html", error="")
    q = q.strip()[:100]
    with db.session() as conn:
        stats = _stats(conn)
        if len(q) >= 3:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stories = conn.execute(
                "SELECT * FROM stories WHERE title LIKE ? ESCAPE '\\' ORDER BY last_at DESC LIMIT 40", (like,)
            ).fetchall()
        else:
            stories = conn.execute("SELECT * FROM stories ORDER BY last_at DESC LIMIT 40").fetchall()
        hidden = conn.execute(
            "SELECT * FROM stories WHERE hidden = 1 ORDER BY last_at DESC LIMIT 50"
        ).fetchall()
        video_channels = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.id) AS videos "
            "FROM video_channels c ORDER BY c.origin, c.title COLLATE NOCASE LIMIT 300").fetchall()
    return _page(request, "admin.html", stats=stats, stories=stories, hidden=hidden, q=q, video_channels=video_channels,
                 csrf=_csrf(session), switch_off=not config.SHOW_FULL_TEXT)


@router.post("/admin/login")
async def admin_login(request: Request):
    if not _enabled():
        raise HTTPException(404)
    ip, now = _ip(request), time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW]
    if len(recent) >= MAX_FAILURES:
        return _page(request, "admin_login.html", 429, error="Слишком много попыток. Подождите 10 минут.")
    form = await _form(request)
    if hmac.compare_digest(form.get("password", "").encode(), config.ADMIN_PASSWORD.encode()):
        _failures.pop(ip, None)
        response = _redirect()
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE, _new_session(), max_age=SESSION_SECONDS, httponly=True,
                            samesite="strict", secure=secure, path="/admin")
        return response
    _failures[ip] = recent + [now]
    return _page(request, "admin_login.html", 401, error="Неверный пароль.")


@router.post("/admin/logout")
async def admin_logout(request: Request):
    _authorized(request, await _form(request))
    response = _redirect()
    response.delete_cookie(COOKIE, path="/admin")
    return response


def _flag(form: dict[str, str]) -> int:
    return 1 if form.get("value") == "1" else 0


@router.post("/admin/source/{source_id}/active")
async def source_active(request: Request, source_id: int):
    form = await _form(request)
    _authorized(request, form)
    value = _flag(form)
    with db.session() as conn:
        conn.execute("UPDATE sources SET active = ?, admin_active = ? WHERE id = ?", (value, value, source_id))
        conn.commit()
    return _redirect("/admin#sources")


@router.post("/admin/source/{source_id}/text")
async def source_text(request: Request, source_id: int):
    form = await _form(request)
    _authorized(request, form)
    value = _flag(form)
    with db.session() as conn:
        conn.execute("UPDATE sources SET show_text = ?, admin_show_text = ? WHERE id = ?",
                     (value, value, source_id))
        conn.commit()
    return _redirect("/admin#sources")


@router.post("/admin/videochannel/{channel_id}/{action}")
async def video_channel_switch(request: Request, channel_id: str, action: str):
    form = await _form(request)
    _authorized(request, form)
    if action not in ("on", "off") or not channel_id.startswith("UC"):
        raise HTTPException(404)
    with db.session() as conn:
        conn.execute("UPDATE video_channels SET enabled = ? WHERE id = ?", (1 if action == "on" else 0, channel_id))
        conn.commit()
    return _redirect("/admin#channels")


@router.post("/admin/story/{story_id}/{action}")
async def story_hide(request: Request, story_id: int, action: str):
    form = await _form(request)
    _authorized(request, form)
    if action not in ("hide", "unhide"):
        raise HTTPException(404)
    with db.session() as conn:
        conn.execute("UPDATE stories SET hidden = ? WHERE id = ?", (1 if action == "hide" else 0, story_id))
        conn.commit()
    back = form.get("q", "")
    return _redirect("/admin" + (f"?q={quote(back)}" if back else "") + "#stories")
