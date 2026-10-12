"""Web UI for the launcher (FastAPI).

Runs locally (`tri-launch web`) and in the hosted container behind Caddy.
The Instantly key never reaches the browser - all API calls happen server-side.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Any, Optional

import yaml
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .autofix import copy_to_markdown, fix_copy
from .clients import client_key, client_summaries
from .config import LaunchConfigError, load_launch, validate_launch
from .workspaces import (
    SEQUENCERS,
    list_workspaces,
    normalize_sequencer,
    workspace_key,
)

MAX_UPLOAD_BYTES = int(os.environ.get("TRI_LAUNCH_MAX_UPLOAD_MB", "250")) * 1024 * 1024

app = FastAPI(title="The Revenue Inbox Launcher", version=__version__)
JOBS: dict[str, dict[str, Any]] = {}
SPINTAX_JOBS: dict[str, dict[str, Any]] = {}
LAUNCH_JOBS: dict[str, dict[str, Any]] = {}

# tri-design assets (tokens, fonts, launcher stylesheet) served next to the single page.
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def _static_version() -> str:
    """Short content hash so a stylesheet change always busts browser caches."""
    static_dir = Path(__file__).parent / "static"
    digest = hashlib.sha256()
    for rel in ("app.css", "tri/tokens.css", "tri/fonts.css"):
        path = static_dir / rel
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:8] or __version__


ASSET_VERSION = _static_version()


def web_root() -> Path:
    return Path(os.environ.get("TRI_LAUNCH_HOME", ".")).resolve()


def sessions_dir() -> Path:
    return web_root() / "state" / "web"


_ENV_LOADED: set[str] = set()


def _load_env_lenient() -> None:
    """Load .env when present; hosted containers inject the same vars via env_file.

    Loaded lazily per ``TRI_LAUNCH_HOME`` so tests and multiple roots stay isolated.
    """
    from .envload import load_env

    path = (web_root() / ".env").resolve()
    key = str(path)
    if key in _ENV_LOADED:
        return
    _ENV_LOADED.add(key)
    load_env(path, optional=True)


# --------------------------------------------------------------------------- auth
COOKIE = "launcher_session"
SESSION_DAYS = 30
OPEN_PATHS = {"/healthz", "/login", "/logout", "/favicon.ico"}

# Shared sign-on: the same signed parent-domain cookie every Revenue Inbox app verifies.
SSO_COOKIE = "tri_sso"
SSO_TTL_SECONDS = 60 * 60 * 24 * 30


def _sso_secret() -> str:
    return os.environ.get("TRI_SSO_SECRET", "")


def _sso_email(request: Request) -> str:
    secret = _sso_secret()
    if not secret:
        return ""
    raw = request.cookies.get(SSO_COOKIE, "")
    payload, _, signature = raw.rpartition(".")
    if not payload or not signature:
        return ""
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    try:
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except Exception:
        return ""
    if not hmac.compare_digest(expected, supplied):
        return ""
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return ""
    email = str(data.get("email") or "")
    if not email or int(data.get("exp") or 0) < int(datetime.now(timezone.utc).timestamp()):
        return ""
    return email


def _sso_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "exp": int(datetime.now(timezone.utc).timestamp()) + SSO_TTL_SECONDS}).encode()
    ).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(
        hmac.new(_sso_secret().encode(), payload.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    return f"{payload}.{signature}"


def _sso_credentials_match(email: str, password: str) -> bool:
    want_email = os.environ.get("TRI_SSO_EMAIL", "admin@example.com").strip().lower()
    want_password = os.environ.get("TRI_SSO_PASSWORD", "")
    return bool(want_password) and email.strip().lower() == want_email and hmac.compare_digest(password, want_password)


def _auth_configured() -> bool:
    return bool(
        os.environ.get("TRI_LAUNCH_PASSWORD") or os.environ.get("TRI_LAUNCH_PASSWORD_SHA256")
    )


def _check_credentials(user: str, password: str) -> bool:
    want_user = os.environ.get("TRI_LAUNCH_USER", "operator")
    if not hmac.compare_digest(user.strip(), want_user):
        return False
    hashed = os.environ.get("TRI_LAUNCH_PASSWORD_SHA256", "")
    if hashed:
        return hmac.compare_digest(sha256(password.encode()).hexdigest(), hashed.strip().lower())
    plain = os.environ.get("TRI_LAUNCH_PASSWORD", "")
    return bool(plain) and hmac.compare_digest(password, plain)


def _secret() -> str:
    return os.environ.get("TRI_LAUNCH_SECRET") or (
        os.environ.get("TRI_LAUNCH_PASSWORD_SHA256", "")
        + os.environ.get("TRI_LAUNCH_PASSWORD", "")
        + "|tri-launcher"
    )


def _make_session(user: str) -> str:
    expires = int(datetime.now(timezone.utc).timestamp()) + SESSION_DAYS * 86400
    payload = f"{user}.{expires}"
    sig = hmac.new(_secret().encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}.{sig}"


def _valid_session(token: Optional[str]) -> bool:
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    user, expires, sig = parts
    if not expires.isdigit() or int(expires) < int(datetime.now(timezone.utc).timestamp()):
        return False
    want = hmac.new(_secret().encode(), f"{user}.{expires}".encode(), "sha256").hexdigest()
    return hmac.compare_digest(sig, want) and user == os.environ.get("TRI_LAUNCH_USER", "operator")


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in - Campaign Launcher</title>
<link rel="icon" href="/static/tri/favicon.svg?v=__ASSET__" type="image/svg+xml">
<link rel="stylesheet" href="/static/tri/tokens.css?v=__ASSET__">
<link rel="stylesheet" href="/static/tri/fonts.css?v=__ASSET__">
<link rel="stylesheet" href="/static/app.css?v=__ASSET__">
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
 .card{width:360px;padding:28px}
 h1{font-size:var(--tri-text-xl);margin:0 0 4px;font-family:var(--tri-font-display)}
 p{color:var(--tri-ink-2);font-size:var(--tri-text-sm);margin:0 0 18px}
 label{display:block;font-size:var(--tri-text-sm);color:var(--tri-ink-2);margin:12px 0 4px}
 input{width:100%;padding:10px;font:inherit;box-sizing:border-box;background:var(--tri-surface);
       color:var(--tri-ink);border:1px solid var(--tri-control-border);border-radius:var(--tri-radius-control)}
 button{width:100%;margin-top:18px;padding:11px;font:inherit;font-weight:var(--tri-weight-medium);
        background:var(--tri-violet-action);color:var(--tri-on-violet);border:1px solid var(--tri-violet-action);
        border-radius:var(--tri-radius-control);cursor:pointer}
 .err{background:var(--tri-danger-bg);color:var(--tri-danger-text);border-radius:var(--tri-radius-chip);
      padding:8px 10px;font-size:var(--tri-text-sm);margin-bottom:6px}
 .card{background:var(--tri-surface);border:1px solid var(--tri-line-strong);border-radius:var(--tri-radius-panel)}
 .mark{display:grid;place-items:center;width:26px;height:26px;border-radius:var(--tri-radius-mark);
       background:var(--tri-violet-action);color:var(--tri-on-violet);margin-bottom:12px;
       font:var(--tri-weight-semibold) 11px/1 var(--tri-font-display)}
</style></head><body>
<form class="card" method="post" action="/login">
  <span class="mark">RI</span>
  <h1>Campaign Launcher</h1><p>The Revenue Inbox</p>
  __ERROR__
  <label>Email</label><input name="username" value="__USER__" autocomplete="username" autofocus>
  <label>Password</label><input name="password" type="password" autocomplete="current-password">
  <input type="hidden" name="next" value="__NEXT__">
  <button type="submit">Sign in</button>
</form></body></html>"""


def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


def _render_login(error: str = "", user: str = "", next_path: str = "/") -> str:
    from html import escape

    return (
        LOGIN_PAGE.replace("__ERROR__", error)
        .replace(
            "__USER__",
            escape(user or os.environ.get("TRI_SSO_EMAIL") or os.environ.get("TRI_LAUNCH_USER", "operator")),
        )
        .replace("__NEXT__", escape(next_path))
        .replace("__ASSET__", ASSET_VERSION)
    )


@app.middleware("http")
async def session_auth(request: Request, call_next):
    # Local runs (`tri-launch web`) keep secrets in .env; hosted runs inject them.
    _load_env_lenient()
    if not _auth_configured() or request.url.path in OPEN_PATHS:
        response = await call_next(request)
    # tri-design assets must load on the login page, before a session exists.
    elif request.url.path.startswith("/static/"):
        response = await call_next(request)
    elif _valid_session(request.cookies.get(COOKIE)):
        response = await call_next(request)
    # Shared sign-on cookie from any other Revenue Inbox app.
    elif _sso_email(request):
        response = await call_next(request)
    elif request.url.path.startswith("/api/"):
        response = JSONResponse(
            {"detail": "not authenticated - sign in at /login"}, status_code=401
        )
    else:
        response = RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=302)
    if request.url.path.startswith("/static/"):
        # Revalidate every time (304s are cheap); prevents stale stylesheets after deploys.
        response.headers["Cache-Control"] = "no-cache"
    elif response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> HTMLResponse:
    return HTMLResponse(_render_login(next_path=_safe_next(next)))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    user = str(form.get("username") or "")
    password = str(form.get("password") or "")
    nxt = _safe_next(str(form.get("next") or "/"))
    if _check_credentials(user, password):
        response = RedirectResponse(nxt, status_code=303)
        response.set_cookie(
            COOKIE, _make_session(user.strip()),
            httponly=True, samesite="lax", secure=_is_https(request),
            max_age=SESSION_DAYS * 86400,
        )
        if _sso_credentials_match(user, password) and _sso_secret():
            response.set_cookie(
                SSO_COOKIE, _sso_token(user.strip().lower()),
                httponly=True, samesite="lax", secure=_is_https(request),
                max_age=SSO_TTL_SECONDS,
                domain=os.environ.get("TRI_SSO_DOMAIN") or None,
            )
        return response
    if _sso_credentials_match(user, password):
        response = RedirectResponse(nxt, status_code=303)
        if _sso_secret():
            response.set_cookie(
                SSO_COOKIE, _sso_token(user.strip().lower()),
                httponly=True, samesite="lax", secure=_is_https(request),
                max_age=SSO_TTL_SECONDS,
                domain=os.environ.get("TRI_SSO_DOMAIN") or None,
            )
        return response
    return HTMLResponse(
        _render_login(
            error='<div class="err">Wrong username or password.</div>',
            user=user,
            next_path=nxt,
        ),
        status_code=401,
    )


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    response.delete_cookie(SSO_COOKIE, domain=os.environ.get("TRI_SSO_DOMAIN") or None)
    return response


# ---------------------------------------------------------------------- helpers
def _session_dir(session_id: str) -> Path:
    if not session_id or any(c not in "0123456789abcdef" for c in session_id):
        raise HTTPException(400, "bad session id")
    path = sessions_dir() / session_id
    if not path.is_dir():
        raise HTTPException(404, "session not found - upload a CSV first")
    return path


def _read_csv_preview(path: Path, max_rows: int = 5000) -> dict[str, Any]:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
        counts: dict[str, dict[str, int]] = {h: {} for h in headers}
        rows = 0
        for row in reader:
            rows += 1
            if rows > max_rows:
                continue
            for h in headers:
                v = (row.get(h) or "").strip()
                if v:
                    counts[h][v] = counts[h].get(v, 0) + 1
    candidates = []
    for h in headers:
        n = len(counts[h])
        if 2 <= n <= 500:
            top = sorted(counts[h].items(), key=lambda kv: (-kv[1], kv[0]))[:8]
            candidates.append({"column": h, "unique": n, "top": top})
    candidates.sort(key=lambda c: -c["unique"])
    from .leads import variable_for_header

    mapped = [
        {"column": h, "variable": variable_for_header(h)}
        for h in headers
        if variable_for_header(h) != h
    ]
    variables: list[str] = []
    for h in headers:
        name = variable_for_header(h)
        if name and name not in variables:
            variables.append(name)
    return {
        "rows": rows,
        "columns": headers,
        "candidates": candidates[:10],
        "mapped": mapped,
        "variables": variables[:60],
    }


def _load_copy_map(body: "PlanRequest") -> dict[str, str]:
    return {k: v for k, v in (body.copy_map or {}).items() if v.strip()}


def _copy_has_content(copy: CampaignCopy) -> bool:
    if (copy.subject or "").strip():
        return True
    for step in copy.steps or []:
        for variant in step.variants or []:
            if (variant or "").strip():
                return True
    return False


def _campaign_markdown(copy: CampaignCopy, *, ban_em_dash: bool = True) -> tuple[str, list[str]]:
    fixed, fixes = fix_copy(
        subject=copy.subject,
        steps=[step.model_dump() for step in copy.steps],
        spintax_greetings=True,
        ban_em_dash=ban_em_dash,
    )
    return copy_to_markdown(fixed), fixes


def _campaign_status_name(status: Any) -> str:
    return {
        0: "Draft",
        1: "Active",
        2: "Paused",
        3: "Completed",
        4: "Running Subsequences",
    }.get(status, str(status))


def _spintax_summary(qa) -> dict[str, dict[str, int]]:
    """Per-segment count of variants whose BODY already contains spintax."""
    from .spintax import count_combinations

    out: dict[str, dict[str, int]] = {}
    for segment in qa.segments:
        with_spin = sum(
            1 for v in segment.variants if count_combinations(v.body or "") > 1
        )
        out[segment.slug] = {"with_spin": with_spin, "variants": len(segment.variants)}
    return out


def _segment_copy_path(cfg, seg) -> Path:
    """Resolve a segment's copy file (mirrors create._copy_path)."""
    path = cfg.path / (seg.copy or f"copy/{seg.slug}.md")
    if path.is_file():
        return path
    return cfg.path / "copy" / (seg.copy or f"{seg.slug}.md")


def _segment_markdown(cfg, seg) -> str:
    if seg is None:
        return ""
    path = _segment_copy_path(cfg, seg)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _plan_payload(
    cfg,
    qa,
    run,
    *,
    session_id: str,
    client: str,
    client_env: str,
    autofixes: dict[str, list[str]],
    skipped_no_copy: list[str],
) -> dict[str, Any]:
    """The JSON the review UI consumes (used by /api/plan and /api/refresh)."""
    unsubscribe_appends = sum(
        1 for segment in qa.segments for v in segment.variants if not v.unsubscribe_present
    )
    spin = _spintax_summary(qa)
    segments_by_slug = {s.slug: s for s in cfg.segments}
    samples = []
    for seg in qa.segments[:3]:
        for v in seg.variants[:2]:
            for s in v.samples[:2]:
                samples.append(
                    {"segment": seg.name, "step": v.step, "variant": v.label,
                     "label": s.label, "text": s.text[:900]}
                )
    return {
        "session_id": session_id,
        "client": client,
        "client_env": client_env,
        "sequencer": cfg.provider,
        "variable_map": cfg.variable_map,
        "overall": qa.status,
        "unassigned": qa.unassigned,
        "autofixes": autofixes,
        "unsubscribe_appends": unsubscribe_appends,
        "skipped_no_copy": skipped_no_copy,
        "segmentation": cfg.segmentation_report,
        "segments": [
            {
                "slug": s.slug,
                "name": s.name,
                "status": s.status,
                "leads": s.leads_count,
                "variants": len(s.variants),
            }
            for s in qa.segments
        ],
        "campaigns": [
            {
                "slug": p.slug,
                "name": p.campaign_name,
                "leads": p.leads_count,
                "qa": p.qa_status,
                "blocked": p.blocked,
                "blocked_reasons": p.blocked_reasons,
                "spintax": spin.get(p.slug, {"with_spin": 0, "variants": 0}),
                "copy_markdown": _segment_markdown(cfg, segments_by_slug.get(p.slug)),
            }
            for p in run.plans
        ],
        "samples": samples,
    }


def _store_plan_meta(session: Path, meta: dict[str, Any]) -> None:
    (session / "plan.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------- models
class CampaignStep(BaseModel):
    delay_days: float = 0
    variants: list[str] = Field(default_factory=list)


class CampaignCopy(BaseModel):
    value: str
    name: str = ""
    include: bool = True
    use_shared: bool = False
    subject: str = ""
    steps: list[CampaignStep] = Field(default_factory=list)


class PlanRequest(BaseModel):
    session_id: str
    client: str
    sequencer: str = "instantly"
    api_key_env: str = ""
    variable_map: dict[str, str] = Field(default_factory=dict)
    settings_campaign: str
    list_column: str
    naming: str = "{client} | {segment} | {date}"
    exclude: list[str] = Field(default_factory=list)
    min_leads: int = 1
    shared: Optional[CampaignCopy] = None
    campaigns: list[CampaignCopy] = Field(default_factory=list)
    # Legacy markdown path (kept for the CLI-style form and older clients).
    shared_copy: str = ""
    copy_map: dict[str, str] = Field(default_factory=dict)
    unsubscribe_mode: str = "append"


class DraftRequest(BaseModel):
    session_id: str
    instruction: str
    client: str = ""
    mode: str = "all"  # all | one
    list_value: str = ""
    column: str = ""
    lists: list[dict[str, Any]] = Field(default_factory=list)
    follow_ups: bool = False
    follow_up_steps: int = 1
    variants: int = 1
    existing: dict[str, dict[str, Any]] = Field(default_factory=dict)
    history: list[dict[str, str]] = Field(default_factory=list)


class CreateRequest(BaseModel):
    session_id: str
    confirm: str = ""


class RefreshRequest(BaseModel):
    session_id: str


class SpintaxRequest(BaseModel):
    session_id: str
    slug: str = ""
    nudge: str = ""


class CopySaveRequest(BaseModel):
    session_id: str
    slug: str
    markdown: str


class LaunchCheckRequest(BaseModel):
    session_id: str


class LaunchRequest(BaseModel):
    session_id: str
    confirm: str = ""


class PauseAllRequest(BaseModel):
    session_id: str


class ChatRequest(BaseModel):
    session_id: str
    message: str
    history: list[dict[str, str]] = Field(default_factory=list)
    client: str = ""
    naming: str = "{client} | {segment} | {date}"


# ---------------------------------------------------------------------- routes
@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "version": __version__, "time": datetime.now(timezone.utc).isoformat()}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    html = (Path(__file__).parent / "web_ui.html").read_text(encoding="utf-8")
    email = _sso_email(request) or os.environ.get("TRI_SSO_EMAIL", "admin@example.com")
    return HTMLResponse(
        html.replace("__EMAIL__", email).replace("__ASSET__", ASSET_VERSION)
    )


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> JSONResponse:
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "please upload a .csv file")
    session_id = uuid.uuid4().hex[:16]
    directory = sessions_dir() / session_id
    (directory / "leads").mkdir(parents=True, exist_ok=True)
    target = directory / "leads" / "all_leads.csv"
    size = 0
    with target.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, f"file larger than {MAX_UPLOAD_BYTES // (1024*1024)} MB")
            out.write(chunk)
    preview = _read_csv_preview(target)
    if not preview["columns"]:
        raise HTTPException(400, "no header row found in the CSV")
    (directory / "preview.json").write_text(json.dumps(preview), encoding="utf-8")
    # sensible default: a column named like a list, else the best candidate
    headers_lower = {c.strip().lower(): c for c in preview["columns"]}
    default_col = headers_lower.get("list name") or headers_lower.get("list_name")
    if not default_col:
        default_col = next(
            (c for c in preview["columns"] if "list" in c.strip().lower()),
            preview["candidates"][0]["column"] if preview["candidates"] else preview["columns"][0],
        )
    return JSONResponse(
        {"session_id": session_id, **preview, "default_column": default_col,
         "size_bytes": size}
    )


@app.get("/api/clients")
def clients() -> JSONResponse:
    """Configured Instantly workspaces. Names and env-var names only - never keys."""
    _load_env_lenient()
    return JSONResponse(client_summaries())


@app.get("/api/sequencers")
def sequencers() -> JSONResponse:
    _load_env_lenient()
    return JSONResponse(list(SEQUENCERS))


@app.get("/api/workspaces")
def workspaces(sequencer: str = "instantly") -> JSONResponse:
    """Workspaces for one sequencer (Instantly or OutFalcon). Names only, never keys."""
    _load_env_lenient()
    try:
        seq = normalize_sequencer(sequencer)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return JSONResponse({"sequencer": seq, "workspaces": list_workspaces(seq)})


@app.get("/api/variables")
def variables(session_id: str) -> JSONResponse:
    """CSV column statistics for the variable-mapping step (empty %, unique, samples)."""
    import csv
    from .leads import variable_for_header

    session = _session_dir(session_id)
    path = session / "leads" / "all_leads.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
        counts = {h: {"empty": 0, "total": 0, "values": {}} for h in headers}
        for row in reader:
            for header in headers:
                value = (row.get(header) or "").strip()
                item = counts[header]
                item["total"] += 1
                if not value:
                    item["empty"] += 1
                    continue
                if len(item["values"]) < 200:
                    item["values"][value] = item["values"].get(value, 0) + 1
    columns = []
    for header in headers:
        item = counts[header]
        total = item["total"] or 1
        top = sorted(item["values"].items(), key=lambda kv: -kv[1])[:5]
        columns.append(
            {
                "column": header,
                "variable": variable_for_header(header),
                "empty": item["empty"],
                "empty_pct": round(100.0 * item["empty"] / total, 1),
                "unique": len(item["values"]),
                "samples": [value for value, _ in top[:3]],
            }
        )
    total_rows = counts[headers[0]]["total"] if headers else 0
    return JSONResponse({"rows": total_rows, "columns": columns})


@app.get("/api/lists")
def lists(session_id: str, column: str = "") -> JSONResponse:
    """Every unique value of the chosen column, with lead counts (full CSV)."""
    import csv

    session = _session_dir(session_id)
    path = session / "leads" / "all_leads.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
        lookup = {h.lower(): h for h in headers}
        want = lookup.get((column or "").strip().lower())
        if not want:
            raise HTTPException(400, f"column {column!r} not found in the CSV")
        counts: dict[str, int] = {}
        blank = 0
        total = 0
        for row in reader:
            total += 1
            value = (row.get(want) or "").strip()
            if not value:
                blank += 1
                continue
            counts[value] = counts.get(value, 0) + 1
    values = [
        {"value": value, "leads": n}
        for value, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return JSONResponse(
        {
            "column": want,
            "columns": headers,
            "values": values[:1000],
            "unique": len(values),
            "rows": total,
            "blank": blank,
        }
    )


@app.get("/api/campaigns")
def campaigns(
    client_env: str = "",
    search: str = "",
    sequencer: str = "instantly",
    workspace_env: str = "",
) -> JSONResponse:
    from .envload import ConfigError

    env_ref = workspace_env or client_env
    try:
        _load_env_lenient()
        seq = normalize_sequencer(sequencer)
        name, _env_var, key = workspace_key(seq, env_ref or None)
    except ConfigError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")

    if seq == "outfalcon":
        from .outfalcon import OutFalconClient

        client = OutFalconClient(key)
        try:
            items = client.list_campaigns(search=search, limit=100)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"OutFalcon API error: {type(exc).__name__}: {exc}")
        finally:
            client.close()
        campaigns_list = [
            {
                "id": c["id"],
                "name": c.get("name", ""),
                "status": c.get("status"),
                "status_name": str(c.get("status") or "").capitalize(),
                "updated": c.get("updated_at", ""),
                "leads": c.get("lead_count"),
            }
            for c in items
        ]
    else:
        from .instantly import InstantlyClient

        client = InstantlyClient(key)
        try:
            items = client.list_campaigns(search=search or None, limit=100)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Instantly API error: {type(exc).__name__}: {exc}")
        finally:
            client.close()
        campaigns_list = [
            {
                "id": c["id"],
                "name": c["name"],
                "status": c.get("status"),
                "status_name": _campaign_status_name(c.get("status")),
                "updated": c.get("timestamp_updated", ""),
            }
            for c in items
        ]
    return JSONResponse(
        {"client": name, "sequencer": seq, "campaigns": campaigns_list}
    )


@app.post("/api/plan")
def plan(body: PlanRequest) -> JSONResponse:
    import shutil

    from .create import build_plan
    from .config import slugify
    from .qa import analyze_launch

    session = _session_dir(body.session_id)
    launch_dir = session / "launch"
    if launch_dir.exists():
        shutil.rmtree(launch_dir)
    (launch_dir / "leads").mkdir(parents=True)
    (launch_dir / "copy").mkdir(parents=True)
    shutil.copyfile(session / "leads" / "all_leads.csv", launch_dir / "leads" / "all_leads.csv")

    exclude = [x for x in body.exclude if x and x.strip()]
    autofixes: dict[str, list[str]] = {}
    skipped_no_copy: list[str] = []

    if body.campaigns:
        shared_md = ""
        if body.shared and _copy_has_content(body.shared):
            shared_md, shared_fixes = _campaign_markdown(body.shared)
            if shared_fixes:
                autofixes["__shared__"] = shared_fixes
        (launch_dir / "copy" / "default.md").write_text(shared_md, encoding="utf-8")
        custom: dict[str, str] = {}
        for campaign in body.campaigns:
            value = (campaign.value or "").strip()
            if not value:
                continue
            if not campaign.include:
                if value not in exclude:
                    exclude.append(value)
                continue
            use_custom = not campaign.use_shared and _copy_has_content(campaign)
            if use_custom:
                markdown, fixes = _campaign_markdown(campaign)
                (launch_dir / "copy" / f"{slugify(value)}.md").write_text(
                    markdown, encoding="utf-8"
                )
                custom[value] = f"copy/{slugify(value)}.md"
                if fixes:
                    autofixes[value] = fixes
            elif not shared_md.strip():
                # No shared copy and nothing written for this list: skip it cleanly
                # instead of blocking the whole batch.
                if value not in exclude:
                    exclude.append(value)
                skipped_no_copy.append(value)
        copy_map = custom
    else:
        # Legacy path: markdown supplied directly.
        (launch_dir / "copy" / "default.md").write_text(body.shared_copy or "", encoding="utf-8")
        copy_map = {}
        for value, text in _load_copy_map(body).items():
            (launch_dir / "copy" / f"{slugify(value)}.md").write_text(text, encoding="utf-8")
            copy_map[value] = f"copy/{slugify(value)}.md"

    config = {
        "client": body.client.strip(),
        "provider": normalize_sequencer(body.sequencer),
        "settings_campaign": body.settings_campaign.strip(),
        "api_key_env": body.api_key_env.strip(),
        "variable_map": {
            str(k): str(v) for k, v in (body.variable_map or {}).items() if str(v).strip()
        },
        "naming": body.naming.strip() or "{client} | {segment} | {date}",
        "segmentation": {
            "mode": "list_name",
            "column": body.list_column.strip(),
            "source": "leads/all_leads.csv",
            "shared_copy": "copy/default.md",
            "copy_map": copy_map,
            "exclude": exclude,
            "min_leads": max(1, body.min_leads),
        },
        "lint": {"ban_em_dash": True, "max_body_words": 120, "max_links": 1},
        "unsubscribe": {"mode": body.unsubscribe_mode},
    }
    (launch_dir / "launch.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    try:
        cfg = load_launch(launch_dir)
    except LaunchConfigError as exc:
        raise HTTPException(400, str(exc))
    issues = validate_launch(cfg)
    if issues:
        raise HTTPException(400, {"issues": issues})
    qa = analyze_launch(cfg)
    run = build_plan(cfg, qa)
    payload = _plan_payload(
        cfg,
        qa,
        run,
        session_id=body.session_id,
        client=body.client,
        client_env=body.api_key_env,
        autofixes=autofixes,
        skipped_no_copy=skipped_no_copy,
    )
    _store_plan_meta(
        session,
            {
                "client": body.client,
                "sequencer": config["provider"],
                "client_env": body.api_key_env,
                "settings_campaign": body.settings_campaign,
                "api_key_env": config["api_key_env"],
                "variable_map": config["variable_map"],
            "autofixes": autofixes,
            "unsubscribe_appends": payload["unsubscribe_appends"],
            "skipped_no_copy": skipped_no_copy,
            "spintax": {},
            "campaigns": [
                {"slug": c["slug"], "name": c["name"], "leads": c["leads"],
                 "qa": c["qa"], "blocked": c["blocked"],
                 "blocked_reasons": c["blocked_reasons"]}
                for c in payload["campaigns"]
            ],
        },
    )
    return JSONResponse(payload)


@app.post("/api/refresh")
def refresh_plan(body: RefreshRequest) -> JSONResponse:
    """Re-run QA/plan on the launch folder without touching the copy files.

    Used after the spintax pass so the review shows the new rendered samples.
    """
    from .create import build_plan
    from .qa import analyze_launch

    session = _session_dir(body.session_id)
    plan_path = session / "plan.json"
    if not plan_path.is_file():
        raise HTTPException(400, "build a plan first")
    meta = json.loads(plan_path.read_text(encoding="utf-8"))
    launch_dir = session / "launch"
    try:
        cfg = load_launch(launch_dir)
    except LaunchConfigError as exc:
        raise HTTPException(400, str(exc))
    qa = analyze_launch(cfg)
    run = build_plan(cfg, qa)
    payload = _plan_payload(
        cfg,
        qa,
        run,
        session_id=body.session_id,
        client=str(meta.get("client") or ""),
        client_env=str(meta.get("client_env") or ""),
        autofixes=meta.get("autofixes") or {},
        skipped_no_copy=meta.get("skipped_no_copy") or [],
    )
    meta["unsubscribe_appends"] = payload["unsubscribe_appends"]
    meta["campaigns"] = [
        {"slug": c["slug"], "name": c["name"], "leads": c["leads"], "qa": c["qa"],
         "blocked": c["blocked"], "blocked_reasons": c["blocked_reasons"]}
        for c in payload["campaigns"]
    ]
    _store_plan_meta(session, meta)
    return JSONResponse(payload)


@app.post("/api/copy")
def save_copy(body: CopySaveRequest) -> JSONResponse:
    """Save hand-edited copy for one campaign and re-run QA/plan."""
    from .copy_parser import CopyParseError, parse_copy, serialize_copy
    from .create import build_plan
    from .qa import analyze_launch

    session = _session_dir(body.session_id)
    plan_path = session / "plan.json"
    if not plan_path.is_file():
        raise HTTPException(400, "build a plan first")
    launch_dir = session / "launch"
    try:
        cfg = load_launch(launch_dir)
    except LaunchConfigError as exc:
        raise HTTPException(400, str(exc))
    seg = next((s for s in cfg.segments if s.slug == body.slug), None)
    if seg is None:
        raise HTTPException(404, f"unknown campaign {body.slug!r}")
    try:
        parsed = parse_copy(body.markdown or "", source=f"{body.slug}.md")
    except CopyParseError as exc:
        raise HTTPException(400, str(exc))
    if not parsed.steps or not any(step.variants for step in parsed.steps):
        raise HTTPException(
            400,
            "the copy needs at least one '# Step N' with one '## Variant' and a Body",
        )
    _segment_copy_path(cfg, seg).write_text(serialize_copy(parsed), encoding="utf-8")

    meta = json.loads(plan_path.read_text(encoding="utf-8"))
    qa = analyze_launch(cfg)
    run = build_plan(cfg, qa)
    payload = _plan_payload(
        cfg,
        qa,
        run,
        session_id=body.session_id,
        client=str(meta.get("client") or ""),
        client_env=str(meta.get("client_env") or ""),
        autofixes=meta.get("autofixes") or {},
        skipped_no_copy=meta.get("skipped_no_copy") or [],
    )
    meta["unsubscribe_appends"] = payload["unsubscribe_appends"]
    meta["campaigns"] = [
        {"slug": c["slug"], "name": c["name"], "leads": c["leads"], "qa": c["qa"],
         "blocked": c["blocked"], "blocked_reasons": c["blocked_reasons"]}
        for c in payload["campaigns"]
    ]
    _store_plan_meta(session, meta)
    return JSONResponse(payload)


def _run_spintax_job(session_id: str, slug: str, nudge: str) -> None:
    job = SPINTAX_JOBS[session_id]
    session = sessions_dir() / session_id
    launch_dir = session / "launch"
    import copy as copy_module
    import hashlib

    from .copy_parser import parse_copy_file, serialize_copy
    from .create import _copy_path, build_plan
    from .qa import analyze_launch
    from .spintax_ai import spin_copy_file

    try:
        cfg = load_launch(launch_dir)
        targets = [s for s in cfg.segments if not slug or s.slug == slug]
        job["total"] = len(targets)
        cache: dict[str, tuple[Any, Any]] = {}
        processed_paths: dict[Any, Any] = {}
        for seg in targets:
            job["current"] = seg.name
            path = _copy_path(cfg, seg)
            try:
                if path in processed_paths:
                    # Two campaigns share one copy file (shared copy): already spun.
                    job["done"].append(
                        {"slug": seg.slug, "name": seg.name, "status": "ok",
                         "error": "", "notes": ["shared copy already spun in this run"],
                         "warnings": []}
                    )
                    job["finished"] = len(job["done"])
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                cached = cache.get(digest)
                if cached is not None:
                    spun, _outcome = cached
                    path.write_text(serialize_copy(copy_module.deepcopy(spun)), encoding="utf-8")
                    processed_paths[path] = True
                    job["done"].append(
                        {"slug": seg.slug, "name": seg.name, "status": "ok",
                         "error": "", "notes": ["reused spintax from an identical campaign"],
                         "warnings": []}
                    )
                    job["finished"] = len(job["done"])
                    continue
                copy_file = parse_copy_file(path)
                outcome = spin_copy_file(
                    copy_file,
                    campaign_name=seg.name,
                    max_words=cfg.lint.max_body_words,
                    nudge=nudge,
                )
            except Exception as exc:  # noqa: BLE001
                job["done"].append(
                    {"slug": seg.slug, "name": seg.name, "status": "error",
                     "error": f"{type(exc).__name__}: {exc}", "notes": []}
                )
                job["finished"] = len(job["done"])
                continue
            if outcome.status == "ok":
                path.write_text(serialize_copy(copy_file), encoding="utf-8")
                cache[digest] = (copy_module.deepcopy(copy_file), outcome)
                processed_paths[path] = True
            job["done"].append(
                {"slug": seg.slug, "name": seg.name, "status": outcome.status,
                 "error": outcome.error, "notes": outcome.notes,
                 "warnings": outcome.warnings}
            )
            job["finished"] = len(job["done"])

        qa = analyze_launch(cfg)
        run = build_plan(cfg, qa)
        meta = json.loads((session / "plan.json").read_text(encoding="utf-8"))
        spin = _spintax_summary(qa)
        meta["campaigns"] = [
            {"slug": p.slug, "name": p.campaign_name, "leads": p.leads_count,
             "qa": p.qa_status, "blocked": p.blocked,
             "blocked_reasons": p.blocked_reasons}
            for p in run.plans
        ]
        meta["unsubscribe_appends"] = sum(
            1 for segment in qa.segments for v in segment.variants
            if not v.unsubscribe_present
        )
        _store_plan_meta(session, meta)
        job["spin_summary"] = spin
        errors = [d for d in job["done"] if d["status"] != "ok"]
        job["status"] = "done" if not errors else (
            "failed" if len(errors) == len(job["done"]) else "partial"
        )
        job["overall"] = qa.status
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["current"] = ""


@app.post("/api/spintax")
def spintax_start(body: SpintaxRequest) -> JSONResponse:
    """Add (or regenerate) spintax for all campaigns or one campaign."""
    _load_env_lenient()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise HTTPException(400, "DEEPSEEK_API_KEY is not configured - spintax needs the AI")
    session = _session_dir(body.session_id)
    if not (session / "plan.json").is_file():
        raise HTTPException(400, "build a plan first")
    existing = SPINTAX_JOBS.get(body.session_id)
    if existing and existing.get("status") == "running":
        raise HTTPException(409, "spintax is already running for this session")
    SPINTAX_JOBS[body.session_id] = {
        "status": "running",
        "total": 0,
        "finished": 0,
        "done": [],
        "current": "",
        "slug": body.slug,
        "started": datetime.now(timezone.utc).isoformat(),
    }
    threading.Thread(
        target=_run_spintax_job,
        args=(body.session_id, body.slug.strip(), body.nudge.strip()),
        daemon=True,
    ).start()
    return JSONResponse({"status": "running"})


@app.get("/api/spintax/{session_id}")
def spintax_status(session_id: str) -> JSONResponse:
    job = SPINTAX_JOBS.get(session_id)
    if not job:
        raise HTTPException(404, "no spintax job for this session")
    return JSONResponse(job)


# ------------------------------------------------------------------- launch
# The only flow that starts sending. It is gated by a readiness readback and a typed
# confirmation, and it only ever touches campaigns this tool created (local state) for
# this session's launch folder. The settings campaign and every other campaign are
# untouchable by construction.
def _tracked_campaigns_for(cfg) -> list[dict[str, Any]]:
    from . import state
    from .create import same_launch

    out = []
    for cid, rec in state.load()["created"].items():
        if same_launch(rec.get("launch_path"), cfg) and (
            (rec.get("provider") or "instantly") == cfg.provider
        ):
            out.append(
                {
                    "id": cid,
                    "name": rec.get("name") or "",
                    "segment": rec.get("segment") or "",
                    "client": rec.get("client") or "",
                }
            )
    return out


def _launch_client(session: Path):
    meta = json.loads((session / "plan.json").read_text(encoding="utf-8"))
    provider = str(meta.get("sequencer") or meta.get("provider") or "instantly")
    env_name = (meta.get("api_key_env") or "").strip()
    name, _env_var, key = workspace_key(provider, env_name or None)
    if provider == "outfalcon":
        from .outfalcon import OutFalconClient

        return provider, name, OutFalconClient(key)
    from .instantly import InstantlyClient

    return provider, name, InstantlyClient(key)


def _campaign_snapshot(client, provider: str, campaign_id: str) -> dict[str, Any]:
    """Provider-neutral view of one campaign for the readiness check."""
    if provider == "outfalcon":
        campaign = client.get_campaign(campaign_id)
        status = str(campaign.get("status") or "")
        leads = campaign.get("lead_count")
        if leads is None:
            leads = client.count_campaign_leads(campaign_id)
        return {
            "name": campaign.get("name") or "",
            "status": status,
            "status_name": status.capitalize() or "?",
            "is_draft": status == "draft",
            "is_active": status == "active",
            "is_paused": status == "paused",
            "leads": int(leads),
            "daily_limit": campaign.get("daily_limit"),
        }
    campaign = client.get_campaign(campaign_id)
    status = campaign.get("status")
    return {
        "name": campaign.get("name") or "",
        "status": status,
        "status_name": _campaign_status_name(status),
        "is_draft": status == 0,
        "is_active": status == 1,
        "is_paused": status == 2,
        "leads": client.count_leads(campaign_id=campaign_id),
        "daily_limit": campaign.get("daily_limit"),
    }


def _launch_readiness(session: Path) -> dict[str, Any]:
    """Readback check for the confirm dialog: status, leads, accounts, daily limits."""
    plan_path = session / "plan.json"
    if not plan_path.is_file():
        raise HTTPException(400, "create the drafts first")
    try:
        cfg = load_launch(session / "launch")
    except LaunchConfigError as exc:
        raise HTTPException(400, str(exc))
    meta = json.loads(plan_path.read_text(encoding="utf-8"))
    blocked_slugs = {
        c.get("slug") for c in (meta.get("campaigns") or []) if c.get("blocked")
    }
    try:
        provider, client_name, client = _launch_client(session)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")
    try:
        tracked = _tracked_campaigns_for(cfg)
        if not tracked:
            raise HTTPException(400, "no campaigns were created for this session yet")
        rows = []
        for item in tracked:
            snapshot = _campaign_snapshot(client, provider, item["id"])
            reasons = []
            if not snapshot["is_draft"]:
                reasons.append(f"status is {snapshot['status_name']}")
            if snapshot["leads"] == 0:
                reasons.append("no leads")
            if item["segment"] in blocked_slugs:
                reasons.append("QA blocked")
            rows.append(
                {
                    "id": item["id"],
                    "name": snapshot["name"] or item["name"],
                    "slug": item["segment"],
                    "status": snapshot["status"],
                    "status_name": snapshot["status_name"],
                    "leads": snapshot["leads"],
                    "daily_limit": snapshot["daily_limit"],
                    "ready": not reasons,
                    "reasons": reasons,
                }
            )
        try:
            if provider == "outfalcon":
                account_count = client.count_accounts()
            else:
                account_count = len(client.list_accounts(limit=100))
        except Exception:  # noqa: BLE001 - not fatal, reported separately
            account_count = 0
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Instantly API error: {type(exc).__name__}: {exc}")
    finally:
        client.close()
    ready = [r for r in rows if r["ready"]]
    return {
        "client": client_name,
        "sequencer": provider,
        "campaigns": rows,
        "ready": len(ready),
        "total": len(rows),
        "leads": sum(r["leads"] for r in ready),
        "accounts": account_count,
        "daily_limit": sum(int(r["daily_limit"] or 0) for r in ready),
        "confirm_phrase": f"LAUNCH {len(ready)}",
    }


@app.post("/api/launch/check")
def launch_check(body: LaunchCheckRequest) -> JSONResponse:
    _load_env_lenient()
    return JSONResponse(_launch_readiness(_session_dir(body.session_id)))


def _run_launch_job(session_id: str) -> None:
    job = LAUNCH_JOBS[session_id]
    session = sessions_dir() / session_id
    try:
        meta = json.loads((session / "plan.json").read_text(encoding="utf-8"))
        cfg = load_launch(session / "launch")
        provider, client_name, client = _launch_client(session)
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
        return
    blocked_slugs = {
        c.get("slug") for c in (meta.get("campaigns") or []) if c.get("blocked")
    }
    try:
        targets = [
            item
            for item in _tracked_campaigns_for(cfg)
            if item["segment"] not in blocked_slugs
        ]
        job["total"] = len(targets)
        for item in targets:
            job["current"] = item["name"]
            entry = {"id": item["id"], "name": item["name"], "status": "error",
                     "error": "", "leads": 0}
            try:
                snapshot = _campaign_snapshot(client, provider, item["id"])
                if snapshot["is_active"]:
                    entry["status"] = "already-active"
                    entry["leads"] = snapshot["leads"]
                elif not snapshot["is_draft"]:
                    entry["error"] = (
                        f"not a draft ({snapshot['status_name']}) - skipped"
                    )
                else:
                    if provider == "outfalcon":
                        client.set_status(item["id"], "active")
                    else:
                        client.activate_campaign(item["id"])
                    final = {"is_active": False, "status_name": "?"}
                    for _ in range(6):
                        final = _campaign_snapshot(client, provider, item["id"])
                        if final["is_active"]:
                            break
                        time.sleep(1.5)
                    if final["is_active"]:
                        entry["status"] = "activated"
                        entry["leads"] = final["leads"]
                    else:
                        entry["error"] = (
                            f"readback status is {final.get('status_name')}, not active"
                        )
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"{type(exc).__name__}: {exc}"
            job["done"].append(entry)
            job["finished"] = len(job["done"])
        ok = [e for e in job["done"] if e["status"] in {"activated", "already-active"}]
        errors = [e for e in job["done"] if e["status"] == "error"]
        job["activated"] = len([e for e in job["done"] if e["status"] == "activated"])
        job["leads"] = sum(e["leads"] for e in ok)
        job["status"] = "done" if not errors else ("failed" if not ok else "partial")
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        client.close()
        job["current"] = ""


@app.post("/api/launch")
def launch_start(body: LaunchRequest) -> JSONResponse:
    """Activate every ready draft for this session. Requires the typed confirmation."""
    _load_env_lenient()
    session = _session_dir(body.session_id)
    readiness = _launch_readiness(session)
    phrase = str(readiness["confirm_phrase"])
    if body.confirm.strip().upper() != phrase.upper():
        raise HTTPException(
            400,
            f"confirmation required: type {phrase!r} to start sending from "
            f"{readiness['client']}",
        )
    if not readiness["ready"]:
        raise HTTPException(400, "no campaigns are ready to launch")
    if not readiness["accounts"]:
        raise HTTPException(400, "no sending accounts visible in this workspace")
    existing = LAUNCH_JOBS.get(body.session_id)
    if existing and existing.get("status") == "running":
        raise HTTPException(409, "a launch is already running for this session")
    LAUNCH_JOBS[body.session_id] = {
        "status": "running",
        "total": readiness["ready"],
        "finished": 0,
        "done": [],
        "current": "",
        "client": readiness["client"],
        "started": datetime.now(timezone.utc).isoformat(),
    }
    threading.Thread(target=_run_launch_job, args=(body.session_id,), daemon=True).start()
    return JSONResponse({"status": "running", "total": readiness["ready"]})


@app.get("/api/launch/jobs/{session_id}")
def launch_job(session_id: str) -> JSONResponse:
    job = LAUNCH_JOBS.get(session_id)
    if not job:
        raise HTTPException(404, "no launch job for this session")
    return JSONResponse(job)


@app.post("/api/launch/pause")
def launch_pause(body: PauseAllRequest) -> JSONResponse:
    """Pause every active campaign this session created (safety stop)."""
    _load_env_lenient()
    session = _session_dir(body.session_id)
    try:
        cfg = load_launch(session / "launch")
        provider, _name, client = _launch_client(session)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")
    results = []
    try:
        for item in _tracked_campaigns_for(cfg):
            snapshot = _campaign_snapshot(client, provider, item["id"])
            if not snapshot["is_active"]:
                results.append({"name": item["name"], "status": "skipped",
                                "detail": snapshot["status_name"]})
                continue
            if provider == "outfalcon":
                client.set_status(item["id"], "paused")
            else:
                client.pause_campaign_safety(item["id"], reason="safety:operator-pause-all")
            got = _campaign_snapshot(client, provider, item["id"])
            results.append(
                {
                    "name": item["name"],
                    "status": "paused" if got["is_paused"] else "failed",
                    "detail": got["status_name"],
                }
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Instantly API error: {type(exc).__name__}: {exc}")
    finally:
        client.close()
    return JSONResponse(
        {"paused": len([r for r in results if r["status"] == "paused"]), "results": results}
    )


def _run_create_job(session_id: str) -> None:
    job = JOBS[session_id]
    session = sessions_dir() / session_id
    launch_dir = session / "launch"
    from .create import create_launch
    from .report import write_launch_report

    try:
        _load_env_lenient()
        plan_data = json.loads((session / "plan.json").read_text(encoding="utf-8"))
        cfg = load_launch(launch_dir)
        provider = cfg.provider
        env_name = (plan_data.get("api_key_env") or cfg.raw.get("api_key_env") or "").strip()
        if provider == "outfalcon":
            from .outfalcon import OutFalconClient
            from .outfalcon_create import create_launch_outfalcon

            name, _env, key = workspace_key("outfalcon", env_name or None)
            client = OutFalconClient(key)
            runner = lambda: create_launch_outfalcon(cfg, client)  # noqa: E731
        else:
            from .envload import get_api_key
            from .instantly import InstantlyClient

            if env_name:
                name, _env, key = workspace_key("instantly", env_name)
            else:
                name, _env, key = workspace_key("instantly", None)
            client = InstantlyClient(key)
            runner = lambda: create_launch(cfg, client)  # noqa: E731
        job["log"].append(f"connected to {provider.capitalize()}: {name}")
        try:
            run = runner()
        finally:
            client.close()
        report_path = write_launch_report(run)
        from .create import launch_totals

        job["result"] = {
            "status": run.status,
            "totals": launch_totals(run),
            "segments": [
                {
                    "name": s.campaign_name,
                    "id": s.campaign_id,
                    "status": s.status,
                    "uploaded": s.leads_uploaded,
                    "skipped": s.leads_skipped,
                    "rejected": s.leads_rejected,
                    "issues": s.issues,
                }
                for s in run.segments
            ],
            "report": report_path.read_text(encoding="utf-8"),
        }
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["log"].append(job["error"])


@app.post("/api/create")
def create(body: CreateRequest) -> JSONResponse:
    if body.confirm.strip().lower() != "create":
        raise HTTPException(400, "confirmation required: send confirm='create'")
    _session_dir(body.session_id)
    if not (sessions_dir() / body.session_id / "plan.json").is_file():
        raise HTTPException(400, "build a plan first")
    existing = JOBS.get(body.session_id)
    if existing and existing["status"] == "running":
        raise HTTPException(409, "a create job is already running for this session")
    JOBS[body.session_id] = {
        "status": "running",
        "log": ["starting"],
        "started": datetime.now(timezone.utc).isoformat(),
    }
    threading.Thread(target=_run_create_job, args=(body.session_id,), daemon=True).start()
    return JSONResponse({"session_id": body.session_id, "status": "running"})


@app.post("/api/chat")
def chat_endpoint(body: ChatRequest) -> JSONResponse:
    """Free-text instruction -> copy/settings for the review form (no API writes)."""
    from .chat import chat

    session = _session_dir(body.session_id)
    preview_path = session / "preview.json"
    if not preview_path.is_file():
        raise HTTPException(400, "upload a CSV first")
    _load_env_lenient()
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    result = chat(
        body.message,
        preview,
        history=body.history,
        client_name=body.client,
        naming=body.naming,
    )
    if result.status == "skipped":
        raise HTTPException(400, result.error or "chat is not configured")
    if result.status == "error":
        raise HTTPException(502, result.error or "chat failed")
    return JSONResponse({"reply": result.reply, "fields": result.fields})


@app.post("/api/draft")
def draft_endpoint(body: DraftRequest) -> JSONResponse:
    """Per-campaign AI drafting. Fills the copy builder only - no Instantly writes."""
    from .draft import draft_all

    session = _session_dir(body.session_id)
    preview_path = session / "preview.json"
    preview = (
        json.loads(preview_path.read_text(encoding="utf-8"))
        if preview_path.is_file()
        else {}
    )
    _load_env_lenient()
    lists = [item for item in body.lists if (item.get("value") or "").strip()]
    if body.mode == "one" and body.list_value:
        match = next(
            (item for item in lists if item.get("value") == body.list_value),
            {"value": body.list_value, "leads": 0},
        )
        lists = [match]
    if not lists:
        for cand in preview.get("candidates") or []:
            if body.column and cand.get("column") != body.column:
                continue
            lists = [{"value": v, "leads": n} for v, n in cand.get("top") or []]
            break
    if not lists:
        raise HTTPException(400, "select the list-name column and pick at least one campaign first")
    result = draft_all(
        body.instruction,
        lists,
        columns=preview.get("columns") or [],
        client=body.client,
        follow_ups=body.follow_ups,
        follow_up_steps=body.follow_up_steps,
        variants=body.variants,
        existing=body.existing,
        history=body.history,
    )
    if result.status == "skipped":
        raise HTTPException(400, result.error or "AI drafting is not configured")
    if result.status == "error":
        raise HTTPException(502, result.error or "AI drafting failed")
    return JSONResponse({"reply": result.reply, "campaigns": result.campaigns})


@app.get("/api/jobs/{session_id}")
def job(session_id: str) -> JSONResponse:
    job_data = JOBS.get(session_id)
    if not job_data:
        raise HTTPException(404, "no job for this session")
    return JSONResponse(job_data)
