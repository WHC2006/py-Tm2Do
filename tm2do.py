#!/usr/bin/env python3
"""
Tm2Do — single-file FastAPI + SQLite team progress tool (Web 1.5).

文件结构（在编辑器内搜索章节标题即可跳转）：
  Config / 常量 · `_CSS`
  SQLite：`migrate` · `get_db`
  业务小函数：`slugify` · `log_activity`
  Jinja：`build_jinja` · `render_page`
  会话与鉴权：`cleanup_sessions` … `verify_csrf`
  FastAPI：`hub` 路由（`/health` … `/admin/…`）
  项目域：`/_csrf_field` 起 — 快照与里程碑 HTML、`projects_page`、任务、流水回退、管理端

Everything lives in this module by design — no package sprawl.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from urllib.parse import quote as url_quote

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from markupsafe import Markup, escape
try:
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
except ImportError:  # pragma: no cover
    ProxyHeadersMiddleware = None  # type: ignore

import jinja2

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

_DB_PATH = os.environ.get("TM2DO_DB_PATH", os.path.join(os.getcwd(), "tm2do.sqlite3"))
_ROOT_PATH = (os.environ.get("TM2DO_ROOT_PATH") or "").rstrip("/")
_TRUST_PROXY = os.environ.get("TM2DO_TRUST_PROXY", "").strip() in ("1", "true", "yes", "on")
_ICP = os.environ.get("TM2DO_ICP", "").strip()
_ICP_URL = os.environ.get("TM2DO_ICP_URL", "https://beian.miit.gov.cn/").strip()

_SESSION_COOKIE = "tm2do_session"
_SESSION_DAYS = 14
_DEFAULT_PORT = 8766
_TASK_STATUSES = ("待办", "进行中", "阻塞", "已完成")
_MILESTONE_STATUSES = ("未开始", "进行中", "已完成")

_PH = PasswordHasher()
_REVERT_ACTIONS = frozenset({"task.update", "milestone.update"})

_CSS = """
:root { font-family: system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif; line-height: 1.45; color: #111; background: #fafafa; }
body { margin: 0; }
a { color: #0b57d0; text-decoration: none; }
a:hover { text-decoration: underline; }
header, footer { background: #fff; border-bottom: 1px solid #e6e6e6; padding: 0.75rem 1rem; }
footer { border-top: 1px solid #e6e6e6; border-bottom: none; margin-top: 2rem; font-size: 0.85rem; color: #444; }
nav a { margin-right: 1rem; }
main { max-width: 960px; margin: 1rem auto; padding: 0 1rem 2rem; }
main:has(.project-page) { max-width: 1120px; }
.card { background: #fff; border: 1px solid #e6e6e6; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }
table { width: 100%; border-collapse: collapse; background: #fff; }
th, td { border: 1px solid #e6e6e6; padding: 0.45rem 0.6rem; text-align: left; vertical-align: top; }
th { background: #f3f5f7; }
.flash { padding: 0.75rem 1rem; background: #ecf7ec; border: 1px solid #b9dfb9; border-radius: 6px; margin: 1rem auto; max-width: 960px; }
.error { background: #fdeaea; border-color: #f3bcbc; }
button, .btn { display: inline-block; padding: 0.35rem 0.75rem; border-radius: 6px; border: 1px solid #ccc; background: #f6f6f6; cursor: pointer; font: inherit; }
button.primary, .btn.primary { background: #0b57d0; border-color: #0b57d0; color: #fff; }
.form-row { margin-bottom: 0.65rem; }
label { display: inline-block; min-width: 8rem; }
input[type=text], input[type=password], textarea, select { width: min(100%, 420px); padding: 0.35rem 0.5rem; font: inherit; box-sizing: border-box; }
textarea { min-height: 90px; }
.small { font-size: 0.85rem; color: #555; }
.icp-footer { margin-top: 0.35rem; }
.icp-footer a { color: #555; }
.project-page { max-width: 1120px; margin: 0 auto; }
.project-layout { display: block; }
@media (min-width: 900px) {
  .project-layout { display: grid; grid-template-columns: 11rem minmax(0, 1fr); gap: 1.25rem; align-items: start; }
}
.project-toc { background: #fff; border: 1px solid #e6e6e6; border-radius: 8px; padding: 0.65rem 0.75rem; font-size: 0.88rem; }
.project-toc summary { cursor: pointer; font-weight: 600; }
.project-toc ul { list-style: none; padding: 0; margin: 0.35rem 0 0 0; }
.project-toc li { margin: 0.2rem 0; }
.project-toc .toc-sub { margin: 0.25rem 0 0.35rem 0.65rem; padding: 0; font-size: 0.82rem; max-height: 12rem; overflow-y: auto; }
.project-toc .toc-sub li { margin: 0.15rem 0; word-break: break-word; }
@media (min-width: 900px) {
  .project-toc.project-toc-desktop { position: sticky; top: 0.75rem; }
  .project-toc-mobile-only { display: none !important; }
}
.project-toc-desktop-wrap { display: none; }
@media (min-width: 900px) {
  .project-toc-desktop-wrap { display: block; }
}
.sec-anchor { scroll-margin-top: 0.75rem; }
.ms-dash-list { list-style: none; padding: 0; margin: 0.35rem 0 0 0; }
.ms-dash-list li { margin: 0.45rem 0; line-height: 1.35; }
.ms-dash-proj { font-weight: 600; }
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    d = dt or utcnow()
    return d.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def path(url_path: str) -> str:
    if not url_path.startswith("/"):
        url_path = "/" + url_path
    return (_ROOT_PATH or "") + url_path


def login_redirect_next(request: Request) -> str:
    return path("/login?next=" + url_quote(request.url.path))


def resolve_next(next_val: str) -> str:
    if not next_val:
        return path("/projects")
    nv = next_val.strip()
    if "://" in nv or nv.startswith("//"):
        return path("/projects")
    if not nv.startswith("/"):
        return path("/projects")
    rp = _ROOT_PATH or ""
    if rp and not nv.startswith(rp):
        return rp + nv
    return nv


def effective_scheme(request: Request) -> str:
    if _TRUST_PROXY:
        xh = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if xh in ("https", "http"):
            return xh
    return request.url.scheme


def is_https(request: Request) -> bool:
    return effective_scheme(request) == "https"


@contextmanager
def get_db():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> None:
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v < 1:
        conn.executescript(
            """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('owner','collaborator')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            flash TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL
        );

        CREATE TABLE project_members (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (project_id, user_id)
        );

        CREATE TABLE milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT '未开始',
            created_at TEXT NOT NULL
        );

        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '待办',
            assignee_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            due_at TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            author_id INTEGER NOT NULL REFERENCES users(id),
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            actor_id INTEGER NOT NULL REFERENCES users(id),
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX idx_tasks_project ON tasks(project_id);
        CREATE INDEX idx_milestones_project ON milestones(project_id);
        CREATE INDEX idx_activity_project ON activity_events(project_id);
        CREATE INDEX idx_sessions_expires ON sessions(expires_at);
        PRAGMA user_version = 1;
        """
        )
        v = 1
    if v < 2:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(milestones)").fetchall()}
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE milestones ADD COLUMN updated_at TEXT")
            conn.execute("UPDATE milestones SET updated_at = created_at WHERE updated_at IS NULL")
        conn.execute("PRAGMA user_version = 2")
        v = 2
    if v < 3:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(milestones)").fetchall()}
        if "assigned_user_id" not in cols:
            conn.execute(
                "ALTER TABLE milestones ADD COLUMN assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            )
        conn.execute("PRAGMA user_version = 3")
        v = 3
    if v < 4:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS milestone_assignees (
                milestone_id INTEGER NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY (milestone_id, user_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_milestone_assignees_user ON milestone_assignees(user_id)"
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(milestones)").fetchall()}
        if "assigned_user_id" in cols:
            conn.execute(
                """
                INSERT OR IGNORE INTO milestone_assignees (milestone_id, user_id)
                SELECT id, assigned_user_id FROM milestones WHERE assigned_user_id IS NOT NULL
                """
            )
        conn.execute("PRAGMA user_version = 4")


def slugify(name: str, conn: sqlite3.Connection) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-") or "project"
    slug = base
    n = 2
    while True:
        row = conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone()
        if not row:
            return slug
        slug = f"{base}-{n}"
        n += 1


def log_activity(
    conn: sqlite3.Connection,
    *,
    project_id: Optional[int],
    actor_id: int,
    entity_type: str,
    entity_id: int,
    action: str,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    meta = json.dumps(metadata or {}, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO activity_events (project_id, actor_id, entity_type, entity_id, action, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, actor_id, entity_type, entity_id, action, meta, iso()),
    )
    return int(cur.lastrowid)


def row_user(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "username": r["username"], "role": r["role"], "is_active": bool(r["is_active"])}


# -----------------------------------------------------------------------------
# Jinja
# -----------------------------------------------------------------------------

def build_jinja() -> jinja2.Environment:
    templates = {
        "layout.j2": """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} · Tm2Do</title>
  <style>{{ css }}</style>
</head>
<body>
<header>
  <strong>Tm2Do</strong>
  <nav>
    {% if user %}
      <a href="{{ path('/projects') }}">项目</a>
      {% if user.role == 'owner' %}
        <a href="{{ path('/admin/users') }}">用户管理</a>
      {% endif %}
      <span class="small">{{ user.username }}（{% if user.role == 'owner' %}站长{% else %}协作者{% endif %}）</span>
      <form method="post" action="{{ path('/logout') }}" style="display:inline;">
        <input type="hidden" name="csrf_token" value="{{ csrf }}">
        <button type="submit">退出</button>
      </form>
    {% endif %}
  </nav>
</header>
{% if flash %}
<div class="flash">{{ flash }}</div>
{% endif %}
<main>{{ body }}</main>
<footer>
  <div class="small">Tm2Do · 团队协作进度</div>
  {% if icp %}
  <div class="icp-footer"><a href="{{ icp_url }}" rel="noopener noreferrer" target="_blank">{{ icp }}</a></div>
  {% endif %}
</footer>
</body>
</html>
""",
        "plain_body.j2": "<div class='card'>{{ content }}</div>",
    }
    env = jinja2.Environment(
        loader=jinja2.DictLoader(templates),
        autoescape=jinja2.select_autoescape(("html", "xml")),
        undefined=jinja2.StrictUndefined,
    )

    def _path(url_path: str) -> str:
        return path(url_path)

    env.globals["path"] = _path
    return env


JENV = build_jinja()


def render_page(
    request: Request,
    *,
    title: str,
    body_html: str,
    user: Optional[dict[str, Any]],
    csrf: str = "",
    flash: str = "",
) -> HTMLResponse:
    icp_esc = escape(_ICP) if _ICP else ""
    icp_url_esc = escape(_ICP_URL or "https://beian.miit.gov.cn/")
    html_out = JENV.get_template("layout.j2").render(
        title=title,
        body=Markup(body_html),
        user=user,
        csrf=csrf,
        flash=flash or "",
        css=_CSS,
        icp=icp_esc,
        icp_url=icp_url_esc,
        path=path,
    )
    return HTMLResponse(html_out)


# -----------------------------------------------------------------------------
# Sessions & auth
# -----------------------------------------------------------------------------

def cleanup_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(),))


def get_session_row(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    cleanup_sessions(conn)
    return conn.execute(
        "SELECT * FROM sessions WHERE token = ? AND expires_at >= ?",
        (token, iso()),
    ).fetchone()


def create_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(16)
    exp = utcnow() + timedelta(days=_SESSION_DAYS)
    conn.execute(
        """
        INSERT INTO sessions (token, user_id, csrf_token, expires_at, flash, created_at)
        VALUES (?, ?, ?, ?, NULL, ?)
        """,
        (token, user_id, csrf, iso(exp), iso()),
    )
    return token, csrf


def set_session_flash(conn: sqlite3.Connection, token: str, message: str) -> None:
    conn.execute("UPDATE sessions SET flash = ? WHERE token = ?", (message, token))


def take_session_flash(conn: sqlite3.Connection, token: str) -> str:
    row = conn.execute("SELECT flash FROM sessions WHERE token = ?", (token,)).fetchone()
    if not row or not row["flash"]:
        return ""
    conn.execute("UPDATE sessions SET flash = NULL WHERE token = ?", (token,))
    return row["flash"]


def session_cookie_response(resp: RedirectResponse, token: str, request: Request) -> RedirectResponse:
    resp.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=is_https(request),
        samesite="lax",
        max_age=_SESSION_DAYS * 86400,
        path="/" if not _ROOT_PATH else _ROOT_PATH,
    )
    return resp


def clear_session_cookie(resp: RedirectResponse, request: Request) -> RedirectResponse:
    resp.delete_cookie(_SESSION_COOKIE, path="/" if not _ROOT_PATH else _ROOT_PATH)
    return resp


def count_owners(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'owner'").fetchone()
    return int(r["c"])


def get_current_user(conn: sqlite3.Connection, request: Request) -> tuple[Optional[dict[str, Any]], Optional[sqlite3.Row]]:
    token = request.cookies.get(_SESSION_COOKIE or "")
    if not token:
        return None, None
    srow = get_session_row(conn, token)
    if not srow:
        return None, None
    urow = conn.execute("SELECT * FROM users WHERE id = ?", (srow["user_id"],)).fetchone()
    if not urow or not urow["is_active"]:
        return None, None
    return row_user(urow), srow


def require_owner(user: dict[str, Any]) -> None:
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="需要站长权限")


def project_accessible(conn: sqlite3.Connection, user: dict[str, Any], project_id: int) -> bool:
    if user["role"] == "owner":
        return True
    r = conn.execute(
        "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
        (project_id, user["id"]),
    ).fetchone()
    return r is not None


def require_project(conn: sqlite3.Connection, user: dict[str, Any], project_id: int) -> sqlite3.Row:
    prow = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not prow:
        raise HTTPException(status_code=404, detail="项目不存在")
    if not project_accessible(conn, user, project_id):
        raise HTTPException(status_code=403, detail="无权访问该项目")
    return prow


def verify_csrf(srow: sqlite3.Row, token: Optional[str]) -> None:
    if not token or token != srow["csrf_token"]:
        raise HTTPException(status_code=400, detail="CSRF 校验失败")


# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------

hub = APIRouter()

app = FastAPI(title="Tm2Do", docs_url=None, redoc_url=None)

if _TRUST_PROXY and ProxyHeadersMiddleware is not None:
    app.add_middleware(ProxyHeadersMiddleware)


@app.middleware("http")
async def db_bootstrap(request: Request, call_next):
    with get_db() as conn:
        migrate(conn)
    return await call_next(request)


@hub.get("/health")
def health():
    return PlainTextResponse("ok")


@hub.get("/")
def root(request: Request):
    with get_db() as conn:
        migrate(conn)
        owners = count_owners(conn)
        if owners == 0:
            return RedirectResponse(path("/setup"), status_code=302)
        user, _ = get_current_user(conn, request)
        if not user:
            return RedirectResponse(path("/login"), status_code=302)
        return RedirectResponse(path("/projects"), status_code=302)


@hub.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    with get_db() as conn:
        migrate(conn)
        if count_owners(conn) > 0:
            return RedirectResponse(path("/login"), status_code=302)
        body = """
        <div class="card">
          <h1>初始化 Tm2Do</h1>
          <p class="small">创建首位<strong>站长</strong>账号（全站仅允许一名站长）。</p>
          <form method="post" action="%s">
            <div class="form-row"><label>用户名</label><input name="username" required minlength="2"></div>
            <div class="form-row"><label>密码</label><input name="password" type="password" required minlength="8"></div>
            <div class="form-row"><label>确认密码</label><input name="password2" type="password" required></div>
            <button class="primary" type="submit">创建并继续</button>
          </form>
        </div>
        """ % escape(
            path("/setup")
        )
        return render_page(request, title="初始化", body_html=body, user=None)


@hub.post("/setup")
def setup_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    username = username.strip()
    if password != password2:
        raise HTTPException(status_code=400, detail="两次密码不一致")
    with get_db() as conn:
        migrate(conn)
        if count_owners(conn) > 0:
            return RedirectResponse(path("/login"), status_code=302)
        try:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, is_active, created_at)
                VALUES (?, ?, 'owner', 1, ?)
                """,
                (username, _PH.hash(password), iso()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="用户名已存在")
    return RedirectResponse(path("/login?flash=" + url_quote("站长账号已创建，请登录")), status_code=302)


@hub.get("/login", response_class=HTMLResponse)
def login_get(request: Request, flash: str = Query(""), next: str = Query("")):
    with get_db() as conn:
        migrate(conn)
        if count_owners(conn) == 0:
            return RedirectResponse(path("/setup"), status_code=302)
        body = f"""
        <div class="card">
          <h1>登录</h1>
          <form method="post" action="{escape(path('/login'))}">
            <input type="hidden" name="next" value="{escape(next)}">
            <div class="form-row"><label>用户名</label><input name="username" required autofocus></div>
            <div class="form-row"><label>密码</label><input name="password" type="password" required></div>
            <button class="primary" type="submit">登录</button>
          </form>
        </div>
        """
        return render_page(request, title="登录", body_html=body, user=None, flash=flash)


@hub.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    with get_db() as conn:
        migrate(conn)
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()
        ok = False
        if row and row["is_active"]:
            try:
                ok = _PH.verify(row["password_hash"], password)
                if ok and _PH.check_needs_rehash(row["password_hash"]):
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (_PH.hash(password), row["id"]),
                    )
            except VerifyMismatchError:
                ok = False
        if not ok:
            return RedirectResponse(path("/login?flash=" + url_quote("用户名或密码错误")), status_code=302)
        token, _csrf = create_session(conn, row["id"])
    dest = resolve_next(next)
    resp = RedirectResponse(dest, status_code=302)
    return session_cookie_response(resp, token, request)


@hub.post("/logout")
def logout_post(request: Request, csrf_token: str = Form("")):
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        if token:
            srow = get_session_row(conn, token)
            if srow:
                verify_csrf(srow, csrf_token)
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    resp = RedirectResponse(path("/login"), status_code=302)
    return clear_session_cookie(resp, request)


# -----------------------------------------------------------------------------
# Projects / milestones / tasks / comments / activity / admin
# -----------------------------------------------------------------------------
# 下文顺序：表单与快照辅助函数 → 里程碑/任务 HTML 片段 → 仪表盘与列表查询 → 路由处理器。


def _csrf_field(csrf: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{escape(csrf)}">'


def _flash_pop(conn: sqlite3.Connection, request: Request) -> str:
    tok = request.cookies.get(_SESSION_COOKIE, "")
    if not tok:
        return ""
    return take_session_flash(conn, tok)


def _task_update_snapshot(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not t:
        return {}
    return {
        "title": t["title"],
        "body": t["body"],
        "status": t["status"],
        "milestone_id": t["milestone_id"],
        "assignee_user_id": t["assignee_user_id"],
        "due_at": t["due_at"],
    }


def _milestone_update_snapshot(conn: sqlite3.Connection, ms_id: int) -> dict[str, Any]:
    m = conn.execute("SELECT * FROM milestones WHERE id = ?", (ms_id,)).fetchone()
    if not m:
        return {}
    return {
        "name": m["name"],
        "sort_order": m["sort_order"],
        "due_date": m["due_date"],
        "status": m["status"],
        "assigned_user_ids": milestone_assignee_ids(conn, ms_id),
    }


def _milestone_assignee_checkboxes(users: list, selected: set[int]) -> str:
    if not users:
        return '<span class="small">暂无活跃用户</span>'
    parts = []
    for u in users:
        uid = int(u["id"])
        chk = " checked" if uid in selected else ""
        parts.append(
            "<label style=\"display:inline-block;margin:0.25rem 1rem 0.25rem 0;\">"
            f'<input type="checkbox" name="assigned_user_ids" value="{uid}"{chk}> '
            f"{escape(u['username'])}</label>"
        )
    return "".join(parts)


def _milestone_remaining_label(due_date: Optional[str]) -> str:
    """Human-readable days until milestone due date (local calendar)."""
    if due_date is None or not str(due_date).strip():
        return "未设截止日"
    raw = str(due_date).strip()[:10]
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return f"截止：{due_date}"
    today = datetime.now().astimezone().date()
    delta = (d - today).days
    if delta > 0:
        return f"剩余 {delta} 天"
    if delta == 0:
        return "今日截止"
    return f"已逾期 {-delta} 天"


def _milestone_card_html(
    project_id: int,
    m: sqlite3.Row,
    related_tasks: list,
    uid_to_name: dict[int, str],
    users_rows: list,
    csrf: str,
    *,
    anchor_id: str,
    assignee_ids: list[int],
) -> str:
    ms_id = int(m["id"])
    assign_label = (
        "、".join(uid_to_name.get(i, str(i)) for i in assignee_ids) if assignee_ids else "（未指派）"
    )
    rows_html = []
    for t in related_tasks:
        assign = uid_to_name.get(int(t["assignee_user_id"])) if t["assignee_user_id"] else ""
        tid = int(t["id"])
        thref = path(f"/projects/{project_id}/tasks/{tid}")
        rows_html.append(
            "<tr>"
            f"<td><a href=\"{escape(thref)}\">{escape(t['title'])}</a></td>"
            f"<td>{escape(t['status'])}</td>"
            f"<td>{escape(assign or '—')}</td>"
            f"<td>{escape(t['due_at'] or '—')}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>任务</th><th>状态</th><th>负责人</th><th>截止</th></tr></thead><tbody>"
        + ("".join(rows_html) if rows_html else "<tr><td colspan=\"4\" class=\"small\">暂无任务</td></tr>")
        + "</tbody></table>"
    )
    sel_ms = "".join(
        f'<option value="{escape(s)}"{" selected" if m["status"] == s else ""}>{escape(s)}</option>'
        for s in _MILESTONE_STATUSES
    )
    assign_checks = _milestone_assignee_checkboxes(users_rows, set(assignee_ids))
    ms_edit = (
        f'<form method="post" action="{escape(path(f"/projects/{project_id}/milestones/{ms_id}/update"))}">'
        f"{_csrf_field(csrf)}"
        f'<div class="form-row"><label>名称</label><input name="name" value="{escape(m["name"])}" required maxlength="200"></div>'
        f'<div class="form-row"><label>排序</label><input name="sort_order" type="number" value="{int(m["sort_order"])}"></div>'
        f'<div class="form-row"><label>截止日</label><input name="due_date" value="{escape(m["due_date"] or "")}" placeholder="YYYY-MM-DD"></div>'
        f'<div class="form-row"><label>状态</label><select name="status">{sel_ms}</select></div>'
        '<fieldset class="form-row" style="border:1px solid #e6e6e6;border-radius:6px;padding:0.5rem 0.75rem;">'
        "<legend>里程碑负责人（可多选）</legend>"
        f"{assign_checks}"
        "</fieldset>"
        '<button type="submit">保存里程碑</button></form>'
    )
    return (
        f'<div class="card sec-anchor" id="{escape(anchor_id)}">'
        f"<h3>{escape(m['name'])}</h3>"
        f'<p class="small">里程碑 #{ms_id} · 负责人：{escape(assign_label)}</p>'
        f"{ms_edit}"
        f"<p class=\"small\">状态：{escape(m['status'])} · 截止：{escape(m['due_date'] or '—')}</p>"
        f"{table}</div>"
    )


def _project_toc_nav(ms_rows: list, project_id: int, my_ms_ids: list[int]) -> str:
    sub_all = []
    for m in ms_rows:
        ms_id = int(m["id"])
        sub_all.append(f'<li><a href="#ms-{ms_id}">{escape(m["name"])}</a></li>')
    sub_ul_all = "<ul class=\"toc-sub\">" + "".join(sub_all) + "</ul>" if sub_all else ""
    id_to_name = {int(m["id"]): m["name"] for m in ms_rows}
    sub_my = []
    for mid in my_ms_ids:
        nm = id_to_name.get(mid, str(mid))
        sub_my.append(f'<li><a href="#my-ms-{mid}">{escape(nm)}</a></li>')
    sub_ul_my = "<ul class=\"toc-sub\">" + "".join(sub_my) + "</ul>" if sub_my else ""
    p = path(f"/projects/{project_id}")
    return (
        "<ul>"
        f'<li><a href="{escape(p)}#project-timeline">时间线</a></li>'
        f'<li><a href="{escape(p)}#my-milestones">指派给我</a>{sub_ul_my}</li>'
        f'<li><a href="{escape(p)}#all-milestones">全部里程碑</a>{sub_ul_all}</li>'
        f'<li><a href="{escape(p)}#tasks-unassigned">未归类任务</a></li>'
        f'<li><a href="{escape(p)}#project-forms">新建</a></li>'
        "</ul>"
    )


def _resolve_assignable_user_id(conn: sqlite3.Connection, raw: Optional[str]) -> Optional[int]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        uid = int(raw)
    except (TypeError, ValueError):
        return None
    row = conn.execute("SELECT id FROM users WHERE id = ? AND is_active = 1", (uid,)).fetchone()
    return uid if row else None


def milestone_assignee_ids(conn: sqlite3.Connection, milestone_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT user_id FROM milestone_assignees WHERE milestone_id = ? ORDER BY user_id",
        (milestone_id,),
    ).fetchall()
    return [int(r["user_id"]) for r in rows]


def set_milestone_assignees(conn: sqlite3.Connection, milestone_id: int, user_ids: list[int]) -> None:
    conn.execute("DELETE FROM milestone_assignees WHERE milestone_id = ?", (milestone_id,))
    for uid in sorted(set(user_ids)):
        conn.execute(
            "INSERT INTO milestone_assignees (milestone_id, user_id) VALUES (?, ?)",
            (milestone_id, uid),
        )


def parse_milestone_assignee_form(conn: sqlite3.Connection, raw_list: list[Any]) -> list[int]:
    out: list[int] = []
    for x in raw_list:
        if not isinstance(x, str):
            continue
        uid = _resolve_assignable_user_id(conn, x)
        if uid is not None:
            out.append(uid)
    return sorted(set(out))


def _my_milestone_summary_html(
    project_id: int,
    m: sqlite3.Row,
    related_tasks: list,
    uid_to_name: dict[int, str],
    ms_id: int,
    assignee_label: str,
) -> str:
    rows_html = []
    for t in related_tasks:
        assign = uid_to_name.get(int(t["assignee_user_id"])) if t["assignee_user_id"] else ""
        tid = int(t["id"])
        thref = path(f"/projects/{project_id}/tasks/{tid}")
        rows_html.append(
            "<tr>"
            f"<td><a href=\"{escape(thref)}\">{escape(t['title'])}</a></td>"
            f"<td>{escape(t['status'])}</td>"
            f"<td>{escape(assign or '—')}</td>"
            f"<td>{escape(t['due_at'] or '—')}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>任务</th><th>状态</th><th>负责人</th><th>截止</th></tr></thead><tbody>"
        + ("".join(rows_html) if rows_html else "<tr><td colspan=\"4\" class=\"small\">暂无任务</td></tr>")
        + "</tbody></table>"
    )
    jump = f'<p class="small"><a href="#ms-{ms_id}">下方编辑里程碑属性与完整视图 ↓</a></p>'
    return (
        f'<div class="card sec-anchor" id="my-ms-{ms_id}">'
        f"<h3>{escape(m['name'])}</h3>"
        f"<p class=\"small\">负责人：{escape(assignee_label)}</p>"
        f"<p class=\"small\">状态：{escape(m['status'])} · 截止：{escape(m['due_date'] or '—')}</p>"
        f"{table}{jump}</div>"
    )


def _can_show_revert(user: dict[str, Any], ev: sqlite3.Row) -> bool:
    if ev["action"] not in _REVERT_ACTIONS:
        return False
    meta = json.loads(ev["metadata_json"] or "{}")
    if "before" not in meta:
        return False
    if user["role"] == "owner":
        return True
    return int(ev["actor_id"]) == int(user["id"])


def _projects_visible_to_user(conn: sqlite3.Connection, user: dict[str, Any]) -> list[sqlite3.Row]:
    if user["role"] == "owner":
        return conn.execute(
            "SELECT id, name, slug, created_at FROM projects ORDER BY id DESC"
        ).fetchall()
    return conn.execute(
        """
        SELECT p.id, p.name, p.slug, p.created_at
        FROM projects p
        INNER JOIN project_members m ON m.project_id = p.id AND m.user_id = ?
        ORDER BY p.id DESC
        """,
        (user["id"],),
    ).fetchall()


def _active_milestones_for_dashboard(conn: sqlite3.Connection, user: dict[str, Any]) -> list[sqlite3.Row]:
    """状态为「进行中」的里程碑：站长看全部，协作者仅成员项目。"""
    st = _MILESTONE_STATUSES[1]
    sel = """
        SELECT m.id AS milestone_id, m.name AS milestone_name, m.due_date,
               p.id AS project_id, p.name AS project_name
        FROM milestones m
        INNER JOIN projects p ON p.id = m.project_id
    """
    # 按真实日历先后排序：due_date 存 TEXT，纯字符串 ASC 会在「2026-6-6」类未补零时排在「2026-06-29」之后（字典序 6 > 0）。
    ord_by = """
        ORDER BY (m.due_date IS NULL OR trim(m.due_date) = ''),
                 COALESCE(julianday(date(trim(replace(m.due_date, '/', '-')))), 999999999),
                 p.id ASC,
                 m.sort_order ASC,
                 m.id ASC
    """
    if user["role"] == "owner":
        return conn.execute(sel + " WHERE m.status = ? " + ord_by, (st,)).fetchall()
    return conn.execute(
        sel
        + " INNER JOIN project_members pm ON pm.project_id = p.id AND pm.user_id = ? WHERE m.status = ? "
        + ord_by,
        (user["id"], st),
    ).fetchall()


def _active_milestones_dash_card_html(active_ms: list[sqlite3.Row]) -> str:
    dash_items = []
    for r in active_ms:
        pid = int(r["project_id"])
        mid = int(r["milestone_id"])
        href = path(f"/projects/{pid}#ms-{mid}")
        rem = _milestone_remaining_label(r["due_date"])
        dash_items.append(
            "<li>"
            f'<a href="{escape(href)}"><span class="ms-dash-proj">{escape(r["project_name"])}</span>'
            f" · {escape(r['milestone_name'])}</a>"
            f' <span class="small">（{escape(rem)}）</span>'
            "</li>"
        )
    dash_body = (
        '<ul class="ms-dash-list">' + "".join(dash_items) + "</ul>"
        if dash_items
        else '<p class="small">暂无进行中的里程碑。</p>'
    )
    return f"""
        <div class="card">
          <h2>进行中的里程碑</h2>
          <p class="small">各项目中状态为「进行中」的里程碑，按截止日先后排序（未填截止日的排在后面）。点击条目跳转至项目页对应里程碑位置。</p>
          {dash_body}
        </div>
        """


@hub.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        flash = _flash_pop(conn, request)
        csrf = srow["csrf_token"]
        rows = _projects_visible_to_user(conn, user)
        dash_card = _active_milestones_dash_card_html(_active_milestones_for_dashboard(conn, user))

        lis = []
        for r in rows:
            lis.append(
                f"<li><a href=\"{escape(path('/projects/' + str(r['id'])))}\">{escape(r['name'])}</a>"
                f" <span class=\"small\">#{r['id']}</span></li>"
            )
        ul = "<ul>" + "".join(lis) + "</ul>" if lis else "<p class=\"small\">暂无项目。</p>"
        create_form = f"""
        <div class="card">
          <h2>新建项目</h2>
          <form method="post" action="{escape(path('/projects'))}">
            {_csrf_field(csrf)}
            <div class="form-row"><label>名称</label><input name="name" required maxlength="200"></div>
            <div class="form-row"><label>描述</label><textarea name="description" maxlength="4000"></textarea></div>
            <button class="primary" type="submit">创建</button>
          </form>
        </div>
        """
        owner_nav = ""
        if user["role"] == "owner":
            owner_nav = f'<p><a href="{escape(path("/admin/users"))}">用户管理</a></p>'
        body = f"""
        {dash_card}
        <div class="card">
          <h1>项目</h1>
          {owner_nav}
          {ul}
        </div>
        {create_form}
        """
        return render_page(request, title="项目", body_html=body, user=user, csrf=csrf, flash=flash)


@hub.post("/projects")
def projects_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    csrf_token: str = Form(""),
):
    name = name.strip()
    description = (description or "").strip()
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        verify_csrf(srow, csrf_token)
        slug = slugify(name, conn)
        cur = conn.execute(
            """
            INSERT INTO projects (name, slug, description, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, slug, description, user["id"], iso()),
        )
        pid = int(cur.lastrowid)
        conn.execute(
            "INSERT OR IGNORE INTO project_members (project_id, user_id, joined_at) VALUES (?, ?, ?)",
            (pid, user["id"], iso()),
        )
        log_activity(
            conn,
            project_id=pid,
            actor_id=user["id"],
            entity_type="project",
            entity_id=pid,
            action="project.create",
            metadata={"name": name},
        )
        set_session_flash(conn, token, "项目已创建")
    return RedirectResponse(path(f"/projects/{pid}"), status_code=302)


@hub.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: int):
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        prow = require_project(conn, user, project_id)
        flash = _flash_pop(conn, request)
        csrf = srow["csrf_token"]
        ms_rows = conn.execute(
            "SELECT * FROM milestones WHERE project_id = ? ORDER BY sort_order ASC, id ASC",
            (project_id,),
        ).fetchall()
        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY id DESC",
            (project_id,),
        ).fetchall()
        users = conn.execute(
            "SELECT id, username FROM users WHERE is_active = 1 ORDER BY username ASC"
        ).fetchall()
        uid_to_name = {int(u["id"]): u["username"] for u in users}

        ms_opts = "".join(
            f'<option value="{m["id"]}">{escape(m["name"])}</option>' for m in ms_rows
        )
        assign_opts = '<option value="">（未指定）</option>' + "".join(
            f'<option value="{u["id"]}">{escape(u["username"])}</option>' for u in users
        )
        status_opts = "".join(
            f'<option value="{escape(s)}">{escape(s)}</option>' for s in _TASK_STATUSES
        )
        ms_status_opts = "".join(
            f'<option value="{escape(s)}">{escape(s)}</option>' for s in _MILESTONE_STATUSES
        )
        ms_assign_checks_new = _milestone_assignee_checkboxes(users, set())

        uid = int(user["id"])
        ms_assign_map = {int(m["id"]): milestone_assignee_ids(conn, int(m["id"])) for m in ms_rows}
        my_ms_list = [m for m in ms_rows if uid in ms_assign_map[int(m["id"])]]
        my_ms_ids = [int(m["id"]) for m in my_ms_list]
        my_summaries = []
        for m in my_ms_list:
            ms_id = int(m["id"])
            related = [t for t in task_rows if t["milestone_id"] == ms_id]
            aids = ms_assign_map[ms_id]
            alabel = "、".join(uid_to_name.get(i, str(i)) for i in aids) if aids else "（未指派）"
            my_summaries.append(
                _my_milestone_summary_html(project_id, m, related, uid_to_name, ms_id, alabel)
            )
        my_block = (
            "<h2 class=\"sec-anchor\" id=\"my-milestones\">指派给我的里程碑</h2>"
            + (
                "".join(my_summaries)
                if my_summaries
                else "<div class=\"card\"><p class=\"small\">当前没有将您列为里程碑负责人的记录（可多选负责人）。</p></div>"
            )
        )

        ms_sections = []
        for m in ms_rows:
            ms_id = int(m["id"])
            related = [t for t in task_rows if t["milestone_id"] == ms_id]
            ms_sections.append(
                _milestone_card_html(
                    project_id,
                    m,
                    related,
                    uid_to_name,
                    users,
                    csrf,
                    anchor_id=f"ms-{ms_id}",
                    assignee_ids=ms_assign_map[ms_id],
                )
            )

        unassigned = [t for t in task_rows if not t["milestone_id"]]
        un_rows = []
        for t in unassigned:
            assign = uid_to_name.get(int(t["assignee_user_id"])) if t["assignee_user_id"] else ""
            tid = int(t["id"])
            thref = path(f"/projects/{project_id}/tasks/{tid}")
            un_rows.append(
                "<tr>"
                f"<td><a href=\"{escape(thref)}\">{escape(t['title'])}</a></td>"
                f"<td>{escape(t['status'])}</td>"
                f"<td>{escape(assign or '—')}</td>"
                f"<td>{escape(t['due_at'] or '—')}</td>"
                "</tr>"
            )
        un_table = (
            "<table><thead><tr><th>任务</th><th>状态</th><th>负责人</th><th>截止</th></tr></thead><tbody>"
            + ("".join(un_rows) if un_rows else "<tr><td colspan=\"4\" class=\"small\">暂无</td></tr>")
            + "</tbody></table>"
        )

        admin_members = ""
        if user["role"] == "owner":
            admin_members = (
                f'<p><a href="{escape(path(f"/admin/projects/{project_id}/members"))}">管理成员</a></p>'
            )

        activity_rows = conn.execute(
            """
            SELECT e.*, u.username AS actor_name
            FROM activity_events e
            LEFT JOIN users u ON u.id = e.actor_id
            WHERE e.project_id = ?
            ORDER BY e.id DESC
            LIMIT 80
            """,
            (project_id,),
        ).fetchall()
        ev_lines = []
        for e in activity_rows:
            summary = f"{escape(e['actor_name'] or '?')} · {escape(e['action'])} · {escape(e['entity_type'])}#{e['entity_id']}"
            revert_btn = ""
            if _can_show_revert(user, e):
                revert_btn = (
                    f'<form method="post" action="{escape(path("/activity/" + str(e["id"]) + "/revert"))}" '
                    'style="display:inline;margin-left:0.5rem;">'
                    f'{_csrf_field(csrf)}'
                    '<button type="submit" onclick="return confirm(\'确认回退该变更？\');">回退</button></form>'
                )
            ev_lines.append(f"<li><span class=\"small\">{escape(e['created_at'])}</span> — {summary}{revert_btn}</li>")
        activity_html = "<ul>" + "".join(ev_lines) + "</ul>" if ev_lines else "<p class=\"small\">暂无记录。</p>"

        toc_inner = _project_toc_nav(ms_rows, project_id, my_ms_ids)
        all_ms_html = "".join(ms_sections)
        body = f"""
        <div class="project-page">
          <div class="project-layout">
            <div class="project-toc-desktop-wrap">
              <nav class="project-toc project-toc-desktop" aria-label="页面索引">{toc_inner}</nav>
            </div>
            <div class="project-main">
              <details class="project-toc project-toc-mobile-only card">
                <summary>页面索引</summary>
                {toc_inner}
              </details>

              <div class="card sec-anchor" id="project-header">
                <h1>{escape(prow['name'])}</h1>
                <p class="small">slug: {escape(prow['slug'])} · <a href="{escape(path('/projects'))}">返回列表</a></p>
                <p>{escape(prow['description'] or '（无描述）')}</p>
                {admin_members}
              </div>

              <div class="card sec-anchor" id="project-timeline">
                <h2>时间线</h2>
                {activity_html}
              </div>

              {my_block}

              <h2 class="sec-anchor" id="all-milestones">全部里程碑</h2>
              {all_ms_html}

              <div class="card sec-anchor" id="tasks-unassigned">
                <h2>未归类任务</h2>
                {un_table}
              </div>

              <div class="sec-anchor" id="project-forms">
                <div class="card">
                  <h2>新建里程碑</h2>
                  <form method="post" action="{escape(path(f'/projects/{project_id}/milestones'))}">
                    {_csrf_field(csrf)}
                    <div class="form-row"><label>名称</label><input name="name" required maxlength="200"></div>
                    <div class="form-row"><label>排序</label><input name="sort_order" type="number" value="0"></div>
                    <div class="form-row"><label>截止日</label><input name="due_date" placeholder="YYYY-MM-DD"></div>
                    <div class="form-row"><label>状态</label>
                      <select name="status">{ms_status_opts}</select>
                    </div>
                    <fieldset class="form-row" style="border:1px solid #e6e6e6;border-radius:6px;padding:0.5rem 0.75rem;">
                      <legend>里程碑负责人（可多选）</legend>
                      {ms_assign_checks_new}
                    </fieldset>
                    <button class="primary" type="submit">添加</button>
                  </form>
                </div>

                <div class="card">
                  <h2>新建任务</h2>
                  <form method="post" action="{escape(path(f'/projects/{project_id}/tasks'))}">
                    {_csrf_field(csrf)}
                    <div class="form-row"><label>标题</label><input name="title" required maxlength="200"></div>
                    <div class="form-row"><label>详情</label><textarea name="body" maxlength="8000"></textarea></div>
                    <div class="form-row"><label>状态</label><select name="status">{status_opts}</select></div>
                    <div class="form-row"><label>里程碑</label>
                      <select name="milestone_id"><option value="">（未指定）</option>{ms_opts}</select>
                    </div>
                    <div class="form-row"><label>负责人</label><select name="assignee_user_id">{assign_opts}</select></div>
                    <div class="form-row"><label>截止</label><input name="due_at" placeholder="YYYY-MM-DD HH:MM 或 ISO"></div>
                    <button class="primary" type="submit">创建任务</button>
                  </form>
                </div>
              </div>
            </div>
          </div>
        </div>
        """
        return render_page(request, title=prow["name"], body_html=body, user=user, csrf=csrf, flash=flash)


@hub.post("/projects/{project_id}/milestones")
async def milestone_create(request: Request, project_id: int):
    form = await request.form()
    csrf_token = form.get("csrf_token")
    name = (form.get("name") or "").strip()
    sort_order = int(form.get("sort_order") or 0)
    due_date_raw = form.get("due_date")
    due_date = due_date_raw.strip() if isinstance(due_date_raw, str) else ""
    due_date = due_date or None
    status = (form.get("status") or _MILESTONE_STATUSES[0]).strip()
    raw_assignees = form.getlist("assigned_user_ids")
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_project(conn, user, project_id)
        verify_csrf(srow, csrf_token if isinstance(csrf_token, str) else None)
        if not name:
            raise HTTPException(status_code=400, detail="里程碑名称不能为空")
        if status not in _MILESTONE_STATUSES:
            status = _MILESTONE_STATUSES[0]
        assignee_ids = parse_milestone_assignee_form(conn, list(raw_assignees))
        cur = conn.execute(
            """
            INSERT INTO milestones (project_id, name, sort_order, due_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, name, sort_order, due_date, status, iso(), iso()),
        )
        mid = int(cur.lastrowid)
        set_milestone_assignees(conn, mid, assignee_ids)
        log_activity(
            conn,
            project_id=project_id,
            actor_id=user["id"],
            entity_type="milestone",
            entity_id=mid,
            action="milestone.create",
            metadata={"name": name, "status": status, "assigned_user_ids": assignee_ids},
        )
        set_session_flash(conn, token, "里程碑已创建")
    return RedirectResponse(path(f"/projects/{project_id}"), status_code=302)


@hub.post("/projects/{project_id}/milestones/{milestone_id}/update")
async def milestone_update(request: Request, project_id: int, milestone_id: int):
    form = await request.form()
    csrf_token = form.get("csrf_token")
    name = (form.get("name") or "").strip()
    sort_order = int(form.get("sort_order") or 0)
    due_date_raw = form.get("due_date")
    due_date = due_date_raw.strip() if isinstance(due_date_raw, str) else ""
    due_date = due_date or None
    status = (form.get("status") or "").strip()
    raw_assignees = form.getlist("assigned_user_ids")
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_project(conn, user, project_id)
        verify_csrf(srow, csrf_token if isinstance(csrf_token, str) else None)
        if not name:
            raise HTTPException(status_code=400, detail="里程碑名称不能为空")
        m = conn.execute(
            "SELECT * FROM milestones WHERE id = ? AND project_id = ?", (milestone_id, project_id)
        ).fetchone()
        if not m:
            raise HTTPException(status_code=404)
        if status not in _MILESTONE_STATUSES:
            status = m["status"]
        assignee_ids = parse_milestone_assignee_form(conn, list(raw_assignees))
        before = _milestone_update_snapshot(conn, milestone_id)
        conn.execute(
            """
            UPDATE milestones SET name=?, sort_order=?, due_date=?, status=?, updated_at=?
            WHERE id = ? AND project_id = ?
            """,
            (name, sort_order, due_date, status, iso(), milestone_id, project_id),
        )
        set_milestone_assignees(conn, milestone_id, assignee_ids)
        after = _milestone_update_snapshot(conn, milestone_id)
        log_activity(
            conn,
            project_id=project_id,
            actor_id=user["id"],
            entity_type="milestone",
            entity_id=milestone_id,
            action="milestone.update",
            metadata={"before": before, "after": after},
        )
        set_session_flash(conn, token, "里程碑已更新")
    return RedirectResponse(path(f"/projects/{project_id}"), status_code=302)


@hub.post("/projects/{project_id}/tasks")
def task_create(
    request: Request,
    project_id: int,
    title: str = Form(...),
    body: str = Form(""),
    status: str = Form(_TASK_STATUSES[0]),
    milestone_id: Optional[str] = Form(None),
    assignee_user_id: Optional[str] = Form(None),
    due_at: str = Form(""),
    csrf_token: str = Form(""),
):
    title = title.strip()
    body = (body or "").strip()
    due_at = due_at.strip() or None
    ms_id = int(milestone_id) if milestone_id else None
    assignee = int(assignee_user_id) if assignee_user_id else None
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_project(conn, user, project_id)
        verify_csrf(srow, csrf_token)
        if status not in _TASK_STATUSES:
            status = _TASK_STATUSES[0]
        if ms_id:
            mrow = conn.execute(
                "SELECT id FROM milestones WHERE id = ? AND project_id = ?", (ms_id, project_id)
            ).fetchone()
            if not mrow:
                ms_id = None
        cur = conn.execute(
            """
            INSERT INTO tasks (project_id, milestone_id, title, body, status, assignee_user_id, due_at, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, ms_id, title, body, status, assignee, due_at, user["id"], iso(), iso()),
        )
        tid = int(cur.lastrowid)
        log_activity(
            conn,
            project_id=project_id,
            actor_id=user["id"],
            entity_type="task",
            entity_id=tid,
            action="task.create",
            metadata={"title": title, "status": status},
        )
        set_session_flash(conn, token, "任务已创建")
    return RedirectResponse(path(f"/projects/{project_id}/tasks/{tid}"), status_code=302)


@hub.get("/projects/{project_id}/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, project_id: int, task_id: int):
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_project(conn, user, project_id)
        t = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND project_id = ?", (task_id, project_id)
        ).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="任务不存在")
        flash = _flash_pop(conn, request)
        csrf = srow["csrf_token"]
        ms_rows = conn.execute(
            "SELECT id, name FROM milestones WHERE project_id = ? ORDER BY sort_order ASC, id ASC",
            (project_id,),
        ).fetchall()
        users = conn.execute(
            "SELECT id, username FROM users WHERE is_active = 1 ORDER BY username ASC"
        ).fetchall()
        ms_opts = "".join(
            f'<option value="{m["id"]}" {"selected" if t["milestone_id"] == m["id"] else ""}>{escape(m["name"])}</option>'
            for m in ms_rows
        )
        assign_opts = '<option value="">（未指定）</option>' + "".join(
            f'<option value="{u["id"]}" {"selected" if t["assignee_user_id"] == u["id"] else ""}>{escape(u["username"])}</option>'
            for u in users
        )
        status_opts = "".join(
            f'<option value="{escape(s)}" {"selected" if t["status"] == s else ""}>{escape(s)}</option>'
            for s in _TASK_STATUSES
        )

        comments = conn.execute(
            """
            SELECT c.*, u.username
            FROM task_comments c
            LEFT JOIN users u ON u.id = c.author_id
            WHERE c.task_id = ?
            ORDER BY c.id ASC
            """,
            (task_id,),
        ).fetchall()
        c_html = []
        for c in comments:
            c_html.append(
                f"<li><span class=\"small\">{escape(c['created_at'])} · {escape(c['username'])}</span>"
                f"<pre>{escape(c['body'])}</pre></li>"
            )
        comments_block = "<ul>" + "".join(c_html) + "</ul>" if c_html else "<p class=\"small\">暂无评论</p>"

        body = f"""
        <div class="card">
          <h1>{escape(t['title'])}</h1>
          <p class="small"><a href="{escape(path(f'/projects/{project_id}'))}">返回项目</a></p>
          <form method="post" action="{escape(path(f'/projects/{project_id}/tasks/{task_id}/update'))}">
            {_csrf_field(csrf)}
            <div class="form-row"><label>标题</label><input name="title" value="{escape(t['title'])}" required maxlength="200"></div>
            <div class="form-row"><label>详情</label><textarea name="body" maxlength="8000">{escape(t['body'])}</textarea></div>
            <div class="form-row"><label>状态</label><select name="status">{status_opts}</select></div>
            <div class="form-row"><label>里程碑</label>
              <select name="milestone_id"><option value="">（未指定）</option>{ms_opts}</select>
            </div>
            <div class="form-row"><label>负责人</label><select name="assignee_user_id">{assign_opts}</select></div>
            <div class="form-row"><label>截止</label><input name="due_at" value="{escape(t['due_at'] or '')}"></div>
            <button class="primary" type="submit">保存修改</button>
          </form>
          <form method="post" action="{escape(path(f'/projects/{project_id}/tasks/{task_id}/delete'))}" style="margin-top:1rem;"
                onsubmit="return confirm('确认删除任务？');">
            {_csrf_field(csrf)}
            <button type="submit">删除任务</button>
          </form>
        </div>

        <div class="card">
          <h2>评论</h2>
          {comments_block}
          <form method="post" action="{escape(path(f'/projects/{project_id}/tasks/{task_id}/comments'))}">
            {_csrf_field(csrf)}
            <div class="form-row"><textarea name="body" required maxlength="4000" placeholder="写下评论…"></textarea></div>
            <button class="primary" type="submit">发表评论</button>
          </form>
        </div>
        """
        return render_page(request, title=t["title"], body_html=body, user=user, csrf=csrf, flash=flash)


@hub.post("/projects/{project_id}/tasks/{task_id}/update")
def task_update(
    request: Request,
    project_id: int,
    task_id: int,
    title: str = Form(...),
    body: str = Form(""),
    status: str = Form(...),
    milestone_id: Optional[str] = Form(None),
    assignee_user_id: Optional[str] = Form(None),
    due_at: str = Form(""),
    csrf_token: str = Form(""),
):
    title = title.strip()
    body = (body or "").strip()
    due_at = due_at.strip() or None
    ms_id = int(milestone_id) if milestone_id else None
    assignee = int(assignee_user_id) if assignee_user_id else None
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_project(conn, user, project_id)
        verify_csrf(srow, csrf_token)
        t = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND project_id = ?", (task_id, project_id)
        ).fetchone()
        if not t:
            raise HTTPException(status_code=404)
        if status not in _TASK_STATUSES:
            status = t["status"]
        if ms_id:
            mrow = conn.execute(
                "SELECT id FROM milestones WHERE id = ? AND project_id = ?", (ms_id, project_id)
            ).fetchone()
            if not mrow:
                ms_id = None
        before = _task_update_snapshot(conn, task_id)
        conn.execute(
            """
            UPDATE tasks SET title=?, body=?, status=?, milestone_id=?, assignee_user_id=?, due_at=?, updated_at=?
            WHERE id = ? AND project_id = ?
            """,
            (title, body, status, ms_id, assignee, due_at, iso(), task_id, project_id),
        )
        after = _task_update_snapshot(conn, task_id)
        log_activity(
            conn,
            project_id=project_id,
            actor_id=user["id"],
            entity_type="task",
            entity_id=task_id,
            action="task.update",
            metadata={"before": before, "after": after},
        )
        set_session_flash(conn, token, "任务已更新")
    return RedirectResponse(path(f"/projects/{project_id}/tasks/{task_id}"), status_code=302)


@hub.post("/projects/{project_id}/tasks/{task_id}/delete")
def task_delete(
    request: Request,
    project_id: int,
    task_id: int,
    csrf_token: str = Form(""),
):
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_project(conn, user, project_id)
        verify_csrf(srow, csrf_token)
        conn.execute("DELETE FROM tasks WHERE id = ? AND project_id = ?", (task_id, project_id))
        log_activity(
            conn,
            project_id=project_id,
            actor_id=user["id"],
            entity_type="task",
            entity_id=task_id,
            action="task.delete",
            metadata={},
        )
        set_session_flash(conn, token, "任务已删除")
    return RedirectResponse(path(f"/projects/{project_id}"), status_code=302)


@hub.post("/projects/{project_id}/tasks/{task_id}/comments")
def task_comment_add(
    request: Request,
    project_id: int,
    task_id: int,
    body: str = Form(...),
    csrf_token: str = Form(""),
):
    body = body.strip()
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_project(conn, user, project_id)
        verify_csrf(srow, csrf_token)
        t = conn.execute(
            "SELECT id FROM tasks WHERE id = ? AND project_id = ?", (task_id, project_id)
        ).fetchone()
        if not t:
            raise HTTPException(status_code=404)
        cur = conn.execute(
            """
            INSERT INTO task_comments (task_id, author_id, body, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, user["id"], body, iso()),
        )
        cid = int(cur.lastrowid)
        log_activity(
            conn,
            project_id=project_id,
            actor_id=user["id"],
            entity_type="task_comment",
            entity_id=cid,
            action="task.comment",
            metadata={"task_id": task_id, "preview": body[:120]},
        )
        set_session_flash(conn, token, "评论已发表")
    return RedirectResponse(path(f"/projects/{project_id}/tasks/{task_id}"), status_code=302)


@hub.post("/activity/{event_id}/revert")
def activity_revert(request: Request, event_id: int, csrf_token: str = Form("")):
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        verify_csrf(srow, csrf_token)
        ev = conn.execute("SELECT * FROM activity_events WHERE id = ?", (event_id,)).fetchone()
        if not ev or not ev["project_id"]:
            raise HTTPException(status_code=404, detail="记录不存在")
        require_project(conn, user, int(ev["project_id"]))
        if ev["action"] not in _REVERT_ACTIONS:
            raise HTTPException(status_code=400, detail="该记录不可回退")
        if user["role"] != "owner" and int(ev["actor_id"]) != int(user["id"]):
            raise HTTPException(status_code=403, detail="只能回退自己的变更")
        meta = json.loads(ev["metadata_json"] or "{}")
        before = meta.get("before")
        if not isinstance(before, dict):
            raise HTTPException(status_code=400, detail="缺少快照")
        created_at = ev["created_at"]
        pid = int(ev["project_id"])

        if ev["action"] == "task.update" and ev["entity_type"] == "task":
            tid = int(ev["entity_id"])
            t = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND project_id = ?", (tid, pid)
            ).fetchone()
            if not t:
                raise HTTPException(status_code=404, detail="任务不存在")
            if t["updated_at"] > created_at:
                set_session_flash(conn, token, "任务已被他人更新，拒绝回退（请刷新后重试）")
                return RedirectResponse(path(f"/projects/{pid}/tasks/{tid}"), status_code=302)
            conn.execute(
                """
                UPDATE tasks SET title=?, body=?, status=?, milestone_id=?, assignee_user_id=?, due_at=?, updated_at=?
                WHERE id = ? AND project_id = ?
                """,
                (
                    before.get("title", t["title"]),
                    before.get("body", t["body"]),
                    before.get("status", t["status"]),
                    before.get("milestone_id", t["milestone_id"]),
                    before.get("assignee_user_id", t["assignee_user_id"]),
                    before.get("due_at", t["due_at"]),
                    iso(),
                    tid,
                    pid,
                ),
            )
            log_activity(
                conn,
                project_id=pid,
                actor_id=user["id"],
                entity_type="task",
                entity_id=tid,
                action="task.rollback",
                metadata={"reverted_event_id": event_id},
            )
            set_session_flash(conn, token, "已回退任务变更")
            return RedirectResponse(path(f"/projects/{pid}/tasks/{tid}"), status_code=302)

        if ev["action"] == "milestone.update" and ev["entity_type"] == "milestone":
            mid = int(ev["entity_id"])
            m = conn.execute(
                "SELECT * FROM milestones WHERE id = ? AND project_id = ?", (mid, pid)
            ).fetchone()
            if not m:
                raise HTTPException(status_code=404)
            updated_marker = m["updated_at"] or m["created_at"]
            if updated_marker > created_at:
                set_session_flash(conn, token, "里程碑已被更新，拒绝回退（请刷新后重试）")
                return RedirectResponse(path(f"/projects/{pid}"), status_code=302)
            conn.execute(
                """
                UPDATE milestones SET name=?, sort_order=?, due_date=?, status=?, updated_at=?
                WHERE id = ? AND project_id = ?
                """,
                (
                    before.get("name", m["name"]),
                    int(before.get("sort_order", m["sort_order"])),
                    before.get("due_date", m["due_date"]),
                    before.get("status", m["status"]),
                    iso(),
                    mid,
                    pid,
                ),
            )
            ids_restored = before.get("assigned_user_ids")
            if ids_restored is None:
                legacy = before.get("assigned_user_id")
                ids_restored = [int(legacy)] if legacy is not None else []
            else:
                ids_restored = [int(x) for x in ids_restored if x is not None]
            set_milestone_assignees(conn, mid, ids_restored)
            log_activity(
                conn,
                project_id=pid,
                actor_id=user["id"],
                entity_type="milestone",
                entity_id=mid,
                action="milestone.rollback",
                metadata={"reverted_event_id": event_id},
            )
            set_session_flash(conn, token, "已回退里程碑变更")
            return RedirectResponse(path(f"/projects/{pid}"), status_code=302)

    raise HTTPException(status_code=400, detail="无法处理该回退")


@hub.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_owner(user)
        flash = _flash_pop(conn, request)
        csrf = srow["csrf_token"]
        rows = conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
        parts = []
        for u in rows:
            role_label = "站长" if u["role"] == "owner" else "协作者"
            active = "启用" if u["is_active"] else "禁用"
            disable_btn = ""
            if u["role"] != "owner" and u["is_active"]:
                disable_btn = (
                    f'<form method="post" action="{escape(path("/admin/users/" + str(u["id"]) + "/disable"))}" style="display:inline;">'
                    f'{_csrf_field(csrf)}<button type="submit">禁用</button></form>'
                )
            elif u["role"] != "owner" and not u["is_active"]:
                disable_btn = (
                    f'<form method="post" action="{escape(path("/admin/users/" + str(u["id"]) + "/enable"))}" style="display:inline;">'
                    f'{_csrf_field(csrf)}<button type="submit">启用</button></form>'
                )
            reset_form = ""
            if u["role"] != "owner":
                reset_form = (
                    f'<form method="post" action="{escape(path("/admin/users/" + str(u["id"]) + "/reset_password"))}" style="display:inline-flex;gap:0.35rem;align-items:center;">'
                    f'{_csrf_field(csrf)}'
                    '<input type="password" name="new_password" placeholder="新密码" required minlength="8">'
                    '<button type="submit">重置密码</button></form>'
                )
            parts.append(
                "<tr>"
                f"<td>{escape(u['username'])}</td>"
                f"<td>{escape(role_label)}</td>"
                f"<td>{escape(active)}</td>"
                f"<td>{disable_btn} {reset_form}</td>"
                "</tr>"
            )
        table = (
            "<table><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>操作</th></tr></thead><tbody>"
            + "".join(parts)
            + "</tbody></table>"
        )
        create_form = f"""
        <div class="card">
          <h2>新建协作者</h2>
          <form method="post" action="{escape(path('/admin/users/create'))}">
            {_csrf_field(csrf)}
            <div class="form-row"><label>用户名</label><input name="username" required minlength="2"></div>
            <div class="form-row"><label>密码</label><input name="password" type="password" required minlength="8"></div>
            <button class="primary" type="submit">创建</button>
          </form>
          <p class="small">全站只能有一名站长；此处仅创建协作者。</p>
        </div>
        """
        body = f'<div class="card"><h1>用户管理</h1><p><a href="{escape(path("/projects"))}">返回项目</a></p>{table}</div>{create_form}'
        return render_page(request, title="用户管理", body_html=body, user=user, csrf=csrf, flash=flash)


@hub.post("/admin/users/create")
def admin_users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    username = username.strip()
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_owner(user)
        verify_csrf(srow, csrf_token)
        try:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, is_active, created_at)
                VALUES (?, ?, 'collaborator', 1, ?)
                """,
                (username, _PH.hash(password), iso()),
            )
            uid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            log_activity(conn, project_id=None, actor_id=user["id"], entity_type="user", entity_id=uid, action="user.create", metadata={"username": username})
            set_session_flash(conn, token, "用户已创建")
        except sqlite3.IntegrityError:
            set_session_flash(conn, token, "用户名已存在")
    return RedirectResponse(path("/admin/users"), status_code=302)


@hub.post("/admin/users/{user_id}/disable")
def admin_users_disable(request: Request, user_id: int, csrf_token: str = Form("")):
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        actor, srow = get_current_user(conn, request)
        if not actor:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_owner(actor)
        verify_csrf(srow, csrf_token)
        u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not u or u["role"] == "owner":
            set_session_flash(conn, token, "不可禁用站长")
        else:
            conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            log_activity(conn, project_id=None, actor_id=actor["id"], entity_type="user", entity_id=user_id, action="user.disable", metadata={})
            set_session_flash(conn, token, "用户已禁用")
    return RedirectResponse(path("/admin/users"), status_code=302)


@hub.post("/admin/users/{user_id}/enable")
def admin_users_enable(request: Request, user_id: int, csrf_token: str = Form("")):
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        actor, srow = get_current_user(conn, request)
        if not actor:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_owner(actor)
        verify_csrf(srow, csrf_token)
        conn.execute("UPDATE users SET is_active = 1 WHERE id = ? AND role != 'owner'", (user_id,))
        log_activity(conn, project_id=None, actor_id=actor["id"], entity_type="user", entity_id=user_id, action="user.enable", metadata={})
        set_session_flash(conn, token, "用户已启用")
    return RedirectResponse(path("/admin/users"), status_code=302)


@hub.post("/admin/users/{user_id}/reset_password")
def admin_users_reset_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
    csrf_token: str = Form(""),
):
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        actor, srow = get_current_user(conn, request)
        if not actor:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_owner(actor)
        verify_csrf(srow, csrf_token)
        u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not u or u["role"] == "owner":
            set_session_flash(conn, token, "不可重置站长密码（请站长自行登录修改）")
        else:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (_PH.hash(new_password), user_id),
            )
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            log_activity(conn, project_id=None, actor_id=actor["id"], entity_type="user", entity_id=user_id, action="user.reset_password", metadata={})
            set_session_flash(conn, token, "密码已重置")
    return RedirectResponse(path("/admin/users"), status_code=302)


@hub.get("/admin/projects/{project_id}/members", response_class=HTMLResponse)
def admin_members(request: Request, project_id: int):
    with get_db() as conn:
        user, srow = get_current_user(conn, request)
        if not user:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_owner(user)
        prow = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not prow:
            raise HTTPException(status_code=404)
        flash = _flash_pop(conn, request)
        csrf = srow["csrf_token"]
        members = conn.execute(
            """
            SELECT u.id, u.username
            FROM project_members m
            JOIN users u ON u.id = m.user_id
            WHERE m.project_id = ?
            ORDER BY u.username ASC
            """,
            (project_id,),
        ).fetchall()
        lis = []
        for mem in members:
            mid = int(mem["id"])
            rm_url = path(f"/admin/projects/{project_id}/members/{mid}/remove")
            lis.append(
                "<li>"
                f"{escape(mem['username'])}"
                f'<form method="post" action="{escape(rm_url)}" style="display:inline;margin-left:0.5rem;">'
                f'{_csrf_field(csrf)}'
                '<button type="submit">移除</button></form>'
                "</li>"
            )
        ul = "<ul>" + "".join(lis) + "</ul>" if lis else "<p class=\"small\">暂无成员（站长默认可见全部项目，可不列入）。</p>"
        body = f"""
        <div class="card">
          <h1>项目成员 · {escape(prow['name'])}</h1>
          <p><a href="{escape(path(f'/projects/{project_id}'))}">返回项目</a></p>
          {ul}
          <h2>添加成员</h2>
          <form method="post" action="{escape(path(f'/admin/projects/{project_id}/members/add'))}">
            {_csrf_field(csrf)}
            <div class="form-row"><label>用户名</label><input name="username" required></div>
            <button class="primary" type="submit">加入</button>
          </form>
        </div>
        """
        return render_page(request, title="成员管理", body_html=body, user=user, csrf=csrf, flash=flash)


@hub.post("/admin/projects/{project_id}/members/add")
def admin_members_add(
    request: Request,
    project_id: int,
    username: str = Form(...),
    csrf_token: str = Form(""),
):
    username = username.strip()
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        actor, srow = get_current_user(conn, request)
        if not actor:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_owner(actor)
        verify_csrf(srow, csrf_token)
        prow = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not prow:
            raise HTTPException(status_code=404)
        u = conn.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE AND is_active = 1",
            (username,),
        ).fetchone()
        if not u:
            set_session_flash(conn, token, "用户不存在或已禁用")
        else:
            conn.execute(
                "INSERT OR IGNORE INTO project_members (project_id, user_id, joined_at) VALUES (?, ?, ?)",
                (project_id, int(u["id"]), iso()),
            )
            log_activity(
                conn,
                project_id=project_id,
                actor_id=actor["id"],
                entity_type="project_member",
                entity_id=int(u["id"]),
                action="project.member_add",
                metadata={"username": username},
            )
            set_session_flash(conn, token, "成员已添加")
    return RedirectResponse(path(f"/admin/projects/{project_id}/members"), status_code=302)


@hub.post("/admin/projects/{project_id}/members/{member_id}/remove")
def admin_members_remove(
    request: Request,
    project_id: int,
    member_id: int,
    csrf_token: str = Form(""),
):
    token = request.cookies.get(_SESSION_COOKIE, "")
    with get_db() as conn:
        actor, srow = get_current_user(conn, request)
        if not actor:
            return RedirectResponse(login_redirect_next(request), status_code=302)
        require_owner(actor)
        verify_csrf(srow, csrf_token)
        conn.execute(
            "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, member_id),
        )
        log_activity(
            conn,
            project_id=project_id,
            actor_id=actor["id"],
            entity_type="project_member",
            entity_id=member_id,
            action="project.member_remove",
            metadata={},
        )
        set_session_flash(conn, token, "成员已移除")
    return RedirectResponse(path(f"/admin/projects/{project_id}/members"), status_code=302)


app.include_router(hub, prefix=_ROOT_PATH if _ROOT_PATH else "")


if __name__ == "__main__":
    import uvicorn

    _port = int(os.environ.get("TM2DO_PORT", str(_DEFAULT_PORT)))
    _host = os.environ.get("TM2DO_HOST", "127.0.0.1")
    uvicorn.run(app, host=_host, port=_port)
