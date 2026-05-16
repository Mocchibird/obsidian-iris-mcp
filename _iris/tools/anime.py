"""MAL integration; Anime DB mirror

@mcp.tool() definitions live here. The shared FastMCP instance is imported
from the package __init__.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
import uuid
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Optional

from .. import mcp
from ..core import *  # noqa: F401, F403  — all helpers and VaultIndex accessor
from ..core import _notify_index_of_write, _notify_index_of_delete  # underscore-prefixed names are excluded by `import *` so import them explicitly
from .web import _get_httpx_client


# ─── from original L8898-9409: MAL integration ───
# =============================================================================
# MyAnimeList integration (official MAL API v2)
# =============================================================================

_MAL_BASE = "https://api.myanimelist.net/v2"


def _mal_auth_path() -> Path:
    """Path to MAL credentials, in the vault's .ai_memory_cache/ folder (alongside vault.db)."""
    return get_vault_root() / ".ai_memory_cache" / "mal_auth.json"


def _load_mal_auth() -> dict | None:
    """Load saved MAL credentials/tokens, if any."""
    p = _mal_auth_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _save_mal_auth(auth: dict) -> None:
    """Persist MAL credentials/tokens to disk."""
    p = _mal_auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(auth, indent=2))


def _mal_token_expires_soon(auth: dict, hours: int = 24) -> bool:
    """True if access_token is missing, has no expiry, or expires within the next `hours`."""
    if not auth.get("access_token"):
        return True
    expires_at = auth.get("expires_at", "")
    if not expires_at:
        return True
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    return (exp - datetime.now()) < timedelta(hours=hours)


def _mal_refresh_token(auth: dict) -> tuple[bool, str]:
    """Exchange refresh_token for a new access_token. Mutates and persists `auth` on success.
    Returns (success, message)."""
    client_id = auth.get("client_id")
    client_secret = auth.get("client_secret")
    refresh_token = auth.get("refresh_token")
    if not (client_id and client_secret and refresh_token):
        return (False, "err: missing client_id/secret/refresh_token — full re-auth required (mal_auth_start)")
    try:
        client = _get_httpx_client()
        resp = client.post(
            "https://myanimelist.net/v1/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        tok = resp.json()
    except Exception as e:
        return (False, f"err refresh: {str(e)[:200]}")
    expires_in = tok.get("expires_in", 2419200)
    auth["access_token"] = tok.get("access_token")
    # MAL also rotates the refresh_token on each refresh; honor that if returned
    if tok.get("refresh_token"):
        auth["refresh_token"] = tok["refresh_token"]
    auth["expires_at"] = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
    _save_mal_auth(auth)
    return (True, f"ok refreshed. expires in {expires_in // 86400}d")


def _mal_headers(prefer_oauth: bool = False) -> tuple[dict, str | None]:
    """Build MAL API auth headers. Returns (headers, error_message).
    Auto-refreshes the access_token when it's expired or expiring within 24h.
    Uses OAuth bearer if available; falls back to X-MAL-CLIENT-ID for public endpoints.
    If prefer_oauth=True and no token (and refresh fails), returns an error."""
    auth = _load_mal_auth() or {}
    token = auth.get("access_token")

    # Try a proactive refresh when token is missing/expiring and we have a refresh_token
    if auth.get("refresh_token") and _mal_token_expires_soon(auth):
        ok, _msg = _mal_refresh_token(auth)
        if ok:
            token = auth["access_token"]

    if prefer_oauth and not token:
        return ({}, "err: this endpoint requires OAuth. Run mal_auth_start after registering credentials.")
    if token:
        return ({"Authorization": f"Bearer {token}"}, None)
    client_id = auth.get("client_id")
    if not client_id:
        return ({}, (
            "err: missing MAL credentials. Register an app at https://myanimelist.net/apiconfig\n"
            "Save <vault>/.ai_memory_cache/mal_auth.json: "
            '{"client_id": "...", "client_secret": "..."}'
        ))
    return ({"X-MAL-CLIENT-ID": client_id}, None)


@mcp.tool()
def mal_auth_refresh() -> str:
    """Manually refresh the MAL access_token using the saved refresh_token. Normally happens automatically when the token is within 24h of expiry."""
    auth = _load_mal_auth() or {}
    ok, msg = _mal_refresh_token(auth)
    return msg


def _mal_get(path: str, params: dict | None = None, prefer_oauth: bool = False) -> tuple[dict | None, str | None]:
    """Call MAL API. Returns (data, error_message)."""
    headers, err = _mal_headers(prefer_oauth=prefer_oauth)
    if err:
        return (None, err)
    try:
        client = _get_httpx_client()
        resp = client.get(f"{_MAL_BASE}{path}", params=params or {}, headers=headers)
        resp.raise_for_status()
        return (resp.json(), None)
    except Exception as e:
        return (None, f"err: {str(e)[:300]}")


@mcp.tool()
def mal_search(query: str, limit: int = 10) -> str:
    """Search anime on MyAnimeList. Returns mal_id|title|type|episodes|mean|status|year per line."""
    limit = max(1, min(limit, 100))
    fields = "alternative_titles,media_type,num_episodes,mean,status,start_season"
    data, err = _mal_get("/anime", {"q": query, "limit": limit, "fields": fields})
    if err:
        return err
    results = (data or {}).get("data", [])
    if not results:
        return "none"
    out = []
    for r in results:
        n = r.get("node", {})
        mal_id = n.get("id", "")
        title = n.get("title", "")
        title_en = (n.get("alternative_titles") or {}).get("en", "")
        media_type = n.get("media_type", "")
        eps = n.get("num_episodes") or "?"
        mean = n.get("mean", "?")
        status = n.get("status", "")
        year = (n.get("start_season") or {}).get("year", "?")
        display = title if not title_en or title_en == title else f"{title} / {title_en}"
        out.append(f"{mal_id}|{display}|{media_type}|{eps}eps|mean:{mean}|{status}|{year}")
    return f"[results:{len(out)}]\n" + "\n".join(out)


@mcp.tool()
def mal_anime(mal_id: int) -> str:
    """Get full anime details from MAL by ID. Returns title|type|status|episodes|mean|aired|genres|studios|url|synopsis|my_status (if OAuth)."""
    fields = (
        "alternative_titles,media_type,num_episodes,mean,status,start_date,end_date,"
        "genres,studios,synopsis,rank,popularity,num_list_users,my_list_status"
    )
    data, err = _mal_get(f"/anime/{mal_id}", {"fields": fields})
    if err:
        return err
    a = data or {}
    if not a or not a.get("id"):
        return "none"
    title = a.get("title", "")
    title_en = (a.get("alternative_titles") or {}).get("en", "")
    media_type = a.get("media_type", "")
    status = a.get("status", "")
    eps = a.get("num_episodes") or "?"
    mean = a.get("mean") or "?"
    start = a.get("start_date", "")
    end = a.get("end_date", "")
    aired = f"{start} to {end}".strip(" to ") if (start or end) else ""
    genres = ",".join(g.get("name", "") for g in a.get("genres", []))
    studios = ",".join(s.get("name", "") for s in a.get("studios", []))
    synopsis = (a.get("synopsis") or "")[:400].replace("\n", " ")
    url = f"https://myanimelist.net/anime/{mal_id}"
    display = title if not title_en or title_en == title else f"{title} / {title_en}"
    lines = [
        f"title|{display}",
        f"type|{media_type}",
        f"status|{status}",
        f"episodes|{eps}",
        f"mean|{mean}",
        f"aired|{aired}",
        f"genres|{genres}",
        f"studios|{studios}",
        f"url|{url}",
        f"synopsis|{synopsis}",
    ]
    mls = a.get("my_list_status")
    if mls:
        lines.append(f"my_status|{mls.get('status', '')}|score:{mls.get('score', 0)}|eps_watched:{mls.get('num_episodes_watched', 0)}")
    return "\n".join(lines)


@mcp.tool()
def mal_user_profile() -> str:
    """Get authenticated user's MAL profile stats (requires OAuth). Returns name|joined|location|days_watched|mean_score|watching|completed|on_hold|dropped|plan_to_watch."""
    data, err = _mal_get("/users/@me", {"fields": "anime_statistics"}, prefer_oauth=True)
    if err:
        return err
    d = data or {}
    if not d:
        return "none"
    stats = d.get("anime_statistics") or {}
    return "\n".join([
        f"name|{d.get('name', '')}",
        f"joined|{(d.get('joined_at') or '')[:10]}",
        f"location|{d.get('location', '')}",
        f"url|https://myanimelist.net/profile/{d.get('name', '')}",
        f"days_watched|{stats.get('num_days', '?')}",
        f"mean_score|{stats.get('mean_score', '?')}",
        f"watching|{stats.get('num_items_watching', 0)}",
        f"completed|{stats.get('num_items_completed', 0)}",
        f"on_hold|{stats.get('num_items_on_hold', 0)}",
        f"dropped|{stats.get('num_items_dropped', 0)}",
        f"plan_to_watch|{stats.get('num_items_plan_to_watch', 0)}",
        f"total_entries|{stats.get('num_items', 0)}",
        f"episodes_watched|{stats.get('num_episodes', 0)}",
    ])


@mcp.tool()
def mal_user_list(status: str = "", limit: int = 50, sort: str = "list_score") -> str:
    """Get authenticated user's anime list (requires OAuth). status: watching|completed|on_hold|dropped|plan_to_watch (empty=all). sort: list_score|list_updated_at|anime_title|anime_start_date. Returns mal_id|title|status|score|episodes_watched/total per line."""
    valid_status = {"", "watching", "completed", "on_hold", "dropped", "plan_to_watch"}
    if status not in valid_status:
        return f"err: status must be one of {sorted(valid_status - {''})}"
    limit = max(1, min(limit, 300))

    headers, err = _mal_headers(prefer_oauth=True)
    if err:
        return err

    params: dict[str, Any] = {
        "fields": "list_status,num_episodes,mean,media_type",
        "limit": min(limit, 100),
        "sort": sort,
    }
    if status:
        params["status"] = status

    out: list[str] = []
    url = f"{_MAL_BASE}/users/@me/animelist"
    try:
        client = _get_httpx_client()
        while url and len(out) < limit:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("data", []):
                node = item.get("node", {})
                ls = item.get("list_status", {}) or {}
                mal_id = node.get("id", "")
                title = node.get("title", "")
                s = ls.get("status", "")
                score = ls.get("score") or "-"
                eps_watched = ls.get("num_episodes_watched") or 0
                eps_total = node.get("num_episodes") or "?"
                out.append(f"{mal_id}|{title}|{s}|score:{score}|{eps_watched}/{eps_total}")
                if len(out) >= limit:
                    break
            url = (data.get("paging") or {}).get("next")
            params = {}
    except Exception as e:
        return f"err: {str(e)[:300]}"

    if not out:
        return "none"
    return f"[results:{len(out)}]\n" + "\n".join(out)


@mcp.tool()
def mal_seasonal(year: int = 0, season: str = "", limit: int = 20, sort: str = "anime_score") -> str:
    """Get seasonal anime from MAL. season: winter|spring|summer|fall (empty=current). year 0=current. sort: anime_score|anime_num_list_users. Returns mal_id|title|type|episodes|mean per line."""
    limit = max(1, min(limit, 100))
    now = datetime.now()
    if not year:
        year = now.year
    if not season:
        m = now.month
        season = "winter" if m <= 3 else "spring" if m <= 6 else "summer" if m <= 9 else "fall"
    season = season.lower()
    if season not in ("winter", "spring", "summer", "fall"):
        return "err: season must be winter|spring|summer|fall"
    data, err = _mal_get(
        f"/anime/season/{year}/{season}",
        {"limit": limit, "fields": "media_type,num_episodes,mean,status", "sort": sort},
    )
    if err:
        return err
    results = (data or {}).get("data", [])
    if not results:
        return "none"
    out = []
    for r in results[:limit]:
        n = r.get("node", {})
        mal_id = n.get("id", "")
        title = n.get("title", "")
        media_type = n.get("media_type", "")
        eps = n.get("num_episodes") or "?"
        mean = n.get("mean", "?")
        out.append(f"{mal_id}|{title}|{media_type}|{eps}eps|mean:{mean}")
    return f"[results:{len(out)}|{year}-{season}]\n" + "\n".join(out)


@mcp.tool()
def mal_ranking(ranking_type: str = "all", limit: int = 20) -> str:
    """Get MAL ranking lists. ranking_type: all|airing|upcoming|tv|ova|movie|special|bypopularity|favorite. Returns rank|mal_id|title|type|episodes|mean per line."""
    limit = max(1, min(limit, 100))
    valid = {"all", "airing", "upcoming", "tv", "ova", "movie", "special", "bypopularity", "favorite"}
    if ranking_type not in valid:
        return f"err: ranking_type must be one of {sorted(valid)}"
    data, err = _mal_get(
        "/anime/ranking",
        {"ranking_type": ranking_type, "limit": limit, "fields": "media_type,num_episodes,mean"},
    )
    if err:
        return err
    results = (data or {}).get("data", [])
    if not results:
        return "none"
    out = []
    for r in results[:limit]:
        n = r.get("node", {})
        rnk = (r.get("ranking") or {}).get("rank", "?")
        mal_id = n.get("id", "")
        title = n.get("title", "")
        media_type = n.get("media_type", "")
        eps = n.get("num_episodes") or "?"
        mean = n.get("mean", "?")
        out.append(f"#{rnk}|{mal_id}|{title}|{media_type}|{eps}eps|mean:{mean}")
    return f"[results:{len(out)}|{ranking_type}]\n" + "\n".join(out)


@mcp.tool()
def mal_auth_status() -> str:
    """Check if MAL OAuth is configured. Returns status|client_id|token_status|expires_at."""
    auth = _load_mal_auth()
    if not auth:
        return "status|not_configured\nhint|create <vault>/.ai_memory_cache/mal_auth.json with client_id and client_secret, then call mal_auth_start"
    cid = (auth.get("client_id") or "")[:8] + "..." if auth.get("client_id") else "missing"
    has_token = bool(auth.get("access_token"))
    expires_at = auth.get("expires_at", "")
    return "\n".join([
        f"status|{'authenticated' if has_token else 'credentials_only'}",
        f"client_id|{cid}",
        f"has_token|{has_token}",
        f"expires_at|{expires_at}",
    ])


@mcp.tool()
def mal_auth_start(port: int = 8765) -> str:
    """Begin MAL OAuth2 PKCE flow. Requires client_id/client_secret in <vault>/.ai_memory_cache/mal_auth.json. Returns auth URL to open in browser. Starts a local callback server that captures the code automatically."""
    import secrets
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    auth = _load_mal_auth() or {}
    client_id = auth.get("client_id")
    client_secret = auth.get("client_secret")
    if not client_id or not client_secret:
        return (
            "err: missing credentials. Register an app at https://myanimelist.net/apiconfig\n"
            "Then save <vault>/.ai_memory_cache/mal_auth.json:\n"
            '  {"client_id": "...", "client_secret": "..."}\n'
            f"App settings: type=Other, redirect URI=http://localhost:{port}/callback"
        )

    # MAL only supports plain PKCE method (verifier == challenge)
    verifier = secrets.token_urlsafe(64)[:128]
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{port}/callback"

    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            code = (qs.get("code") or [None])[0]
            got_state = (qs.get("state") or [None])[0]
            if code and got_state == state:
                captured["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>MAL auth complete</h1><p>You can close this tab.</p>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Bad state or missing code")

    server = HTTPServer(("localhost", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth_url = (
        "https://myanimelist.net/v1/oauth2/authorize"
        f"?response_type=code&client_id={client_id}"
        f"&code_challenge={verifier}&code_challenge_method=plain"
        f"&state={state}&redirect_uri={redirect_uri}"
    )

    # Try to open the browser automatically; user still has to click Allow on MAL
    try:
        import webbrowser
        webbrowser.open(auth_url, new=2)
    except Exception:
        pass

    # Wait up to 180s for callback
    import time
    deadline = time.time() + 180
    while time.time() < deadline and "code" not in captured:
        time.sleep(0.5)
    server.shutdown()

    if "code" not in captured:
        return (
            f"err: timeout (180s). If the browser didn't open, paste this URL manually:\n{auth_url}\n"
            "Then re-run mal_auth_start."
        )

    # Exchange code for token
    try:
        client = _get_httpx_client()
        resp = client.post(
            "https://myanimelist.net/v1/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": captured["code"],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        tok = resp.json()
    except Exception as e:
        return f"err token exchange: {str(e)[:300]}"

    now = datetime.now()
    expires_in = tok.get("expires_in", 2419200)
    auth.update({
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "token_type": tok.get("token_type", "Bearer"),
        "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
    })
    _save_mal_auth(auth)
    return f"ok authenticated. tokens saved. expires in {expires_in // 86400}d"


@mcp.tool()
def mal_update_list_status(
    mal_id: int,
    status: str = "",
    score: int = -1,
    num_watched_episodes: int = -1,
    comments: str = "",
) -> str:
    """Update an anime's status on the user's MAL list (requires OAuth). status: watching|completed|on_hold|dropped|plan_to_watch. score 0-10 (-1=skip). num_watched_episodes -1=skip."""
    auth = _load_mal_auth()
    if not auth or not auth.get("access_token"):
        return (
            "err: MAL OAuth not configured. To enable list updates:\n"
            "1. Register an app at https://myanimelist.net/apiconfig (app type: 'Other', redirect: 'http://localhost:8765/callback')\n"
            "2. Save credentials to <vault>/.ai_memory_cache/mal_auth.json:\n"
            '   {"client_id": "...", "client_secret": "..."}\n'
            "3. Run the OAuth flow (separate setup script — ask user to wire this up)\n"
            "4. Tokens will be saved automatically for future calls."
        )

    valid_status = {"watching", "completed", "on_hold", "dropped", "plan_to_watch"}
    fields: dict = {}
    if status:
        if status not in valid_status:
            return f"err: status must be one of {sorted(valid_status)}"
        fields["status"] = status
    if score >= 0:
        if score > 10:
            return "err: score must be 0-10"
        fields["score"] = score
    if num_watched_episodes >= 0:
        fields["num_watched_episodes"] = num_watched_episodes
    if comments:
        fields["comments"] = comments
    if not fields:
        return "err: nothing to update"

    try:
        client = _get_httpx_client()
        resp = client.patch(
            f"{_MAL_BASE}/anime/{mal_id}/my_list_status",
            data=fields,
            headers={"Authorization": f"Bearer {auth['access_token']}"},
        )
        resp.raise_for_status()
        result = resp.json()
        return f"ok mal_id:{mal_id}|" + "|".join(f"{k}:{v}" for k, v in result.items() if k in fields)
    except Exception as e:
        return f"err: {str(e)[:300]}"



# ─── from original L9410-10152: Anime DB mirror ───
# =============================================================================
# Anime list DB mirror — synced with Anime.md hub and optionally MAL
# =============================================================================

# Possible hub locations — checked in order. First existing wins.
_ANIME_HUB_PATHS = (
    "30_Episodic/Anime/Anime.md",
    "30_Episodic/Gaming/Anime.md",
)


def _anime_hub_path() -> str:
    """Return the active anime hub path (first one that exists)."""
    root = get_vault_root()
    for p in _ANIME_HUB_PATHS:
        if (root / p).exists():
            return p
    return _ANIME_HUB_PATHS[0]  # default fallback

_ANIME_VALID_STATUS = {"watching", "completed", "on_hold", "dropped", "plan_to_watch"}


# @mcp.tool()  # removed — use sqlite_query instead
def anime_list(
    status: str = "",
    limit: int = 100,
    order_by: str = "title",
) -> str:
    """Query the SQLite anime_list table. status: watching|completed|on_hold|dropped|plan_to_watch (empty=all). order_by: title|score|start_date|end_date|updated_at. Returns mal_id|title|status|score|start|end|eps_watched/total|priority|note per line."""
    if status and status not in _ANIME_VALID_STATUS:
        return f"err: status must be one of {sorted(_ANIME_VALID_STATUS)}"
    valid_order = {"title", "score", "start_date", "end_date", "updated_at"}
    if order_by not in valid_order:
        return f"err: order_by must be one of {sorted(valid_order)}"
    limit = max(1, min(limit, 500))

    idx = get_vault_index()
    c = idx.conn
    sql = "SELECT * FROM anime_list"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    if order_by == "score":
        sql += " ORDER BY score DESC NULLS LAST, title COLLATE NOCASE"
    else:
        sql += f" ORDER BY {order_by} COLLATE NOCASE"
    sql += " LIMIT ?"
    params.append(limit)
    rows = c.execute(sql, params).fetchall()

    if not rows:
        return "none"
    out = [f"[results:{len(rows)}]"]
    for r in rows:
        eps_w = r["eps_watched"] if r["eps_watched"] is not None else ""
        eps_t = r["eps_total"] if r["eps_total"] is not None else ""
        eps = f"{eps_w}/{eps_t}" if eps_w != "" or eps_t != "" else ""
        score = r["score"] if r["score"] is not None else ""
        out.append(
            f"{r['mal_id']}|{r['title']}|{r['status']}|{score}|"
            f"{r['start_date']}|{r['end_date']}|{eps}|{r['priority']}|{r['note'][:80]}"
        )
    return "\n".join(out)


# @mcp.tool()  # removed — use sqlite_query instead
def anime_stats() -> str:
    """Counts and stats from the SQLite anime_list table. Returns total|by-status|mean score."""
    idx = get_vault_index()
    c = idx.conn
    total = c.execute("SELECT COUNT(*) AS n FROM anime_list").fetchone()["n"]
    avg = c.execute("SELECT AVG(score) AS a FROM anime_list WHERE score IS NOT NULL").fetchone()["a"]
    by_status = c.execute(
        "SELECT status, COUNT(*) AS n FROM anime_list GROUP BY status ORDER BY status"
    ).fetchall()
    lines = [
        f"total|{total}",
        f"mean_score|{avg:.2f}" if avg is not None else "mean_score|—",
    ]
    for r in by_status:
        lines.append(f"{r['status']}|{r['n']}")
    return "\n".join(lines)


@mcp.tool()
def anime_upsert(
    mal_id: int,
    title: str = "",
    status: str = "",
    score: int = -1,
    start_date: str = "",
    end_date: str = "",
    eps_watched: int = -1,
    eps_total: int = -1,
    priority: str = "",
    note: str = "",
    auto_push: bool = True,
    skip_verify: bool = False,
    reload_db: bool = True,
) -> str:
    """Insert or update one anime_list row. Pass -1 for numeric fields to skip them. mal_id is required. auto_push=True (default): pushes to MAL after the local write. skip_verify=False (default): if a non-empty title is provided AND it doesn't match MAL's title for that mal_id, the call is REJECTED. Set skip_verify=True only when you're 100% certain (e.g. data pulled from MAL itself). reload_db=True (default) signals Obsidian's SQLite DB Plugin to refresh — pass False inside bulk writes."""
    if mal_id <= 0:
        return "err: mal_id must be a positive integer"
    if status and status not in _ANIME_VALID_STATUS:
        return f"err: status must be one of {sorted(_ANIME_VALID_STATUS)}"

    # SAFEGUARD: verify the supplied title matches MAL's title for this ID
    if title and not skip_verify:
        auth = _load_mal_auth()
        if auth and (auth.get("client_id") or auth.get("access_token")):
            headers_v, err_v = _mal_headers(prefer_oauth=False)
            if not err_v:
                try:
                    client_v = _get_httpx_client()
                    resp = client_v.get(
                        f"{_MAL_BASE}/anime/{mal_id}",
                        params={"fields": "alternative_titles"},
                        headers=headers_v,
                    )
                    if resp.status_code == 404:
                        return f"err: MAL ID {mal_id} does not exist. Use mal_search to find the correct ID for '{title}'."
                    if resp.is_success:
                        data = resp.json()
                        mal_title = data.get("title", "")
                        alt_en = (data.get("alternative_titles") or {}).get("en", "")
                        if not _titles_match(title, mal_title, alt_en):
                            return (
                                f"err: ID/TITLE MISMATCH — mal_id {mal_id} on MAL is '{mal_title}'"
                                f" (alt: '{alt_en}'), but you tried to write '{title}'."
                                f" Use mal_search('{title}') to find the correct ID."
                                f" If you really intend this (e.g. you're correcting MAL's title), pass skip_verify=True."
                            )
                except Exception:
                    pass  # Network failure shouldn't block the local write

    idx = get_vault_index()
    c = idx.conn
    existing = c.execute("SELECT * FROM anime_list WHERE mal_id = ?", (mal_id,)).fetchone()
    now = datetime.now().isoformat(timespec="seconds")

    def pick(new, old, sentinel=None):
        return new if new != sentinel else old

    fields = {
        "title": pick(title, existing["title"] if existing else "", ""),
        "status": pick(status, existing["status"] if existing else "", ""),
        "score": pick(score, existing["score"] if existing else None, -1),
        "start_date": pick(start_date, existing["start_date"] if existing else "", ""),
        "end_date": pick(end_date, existing["end_date"] if existing else "", ""),
        "eps_watched": pick(eps_watched, existing["eps_watched"] if existing else None, -1),
        "eps_total": pick(eps_total, existing["eps_total"] if existing else None, -1),
        "priority": pick(priority, existing["priority"] if existing else "", ""),
        "note": pick(note, existing["note"] if existing else "", ""),
    }
    # Normalize score sentinels
    if fields["score"] == -1:
        fields["score"] = None
    if fields["eps_watched"] == -1:
        fields["eps_watched"] = None
    if fields["eps_total"] == -1:
        fields["eps_total"] = None

    c.execute(
        "INSERT OR REPLACE INTO anime_list "
        "(mal_id, title, status, score, start_date, end_date, eps_watched, eps_total, priority, note, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mal_id, fields["title"], fields["status"], fields["score"],
         fields["start_date"], fields["end_date"], fields["eps_watched"], fields["eps_total"],
         fields["priority"], fields["note"], now),
    )
    c.commit()
    if reload_db: maybe_reload_db_plugin()
    base = f"ok {'updated' if existing else 'inserted'} mal_id:{mal_id}|{fields['title']}|{fields['status']}"

    if auto_push:
        auth = _load_mal_auth()
        if auth and auth.get("access_token"):
            push_result = anime_push_to_mal(dry_run=False, mal_ids=[mal_id])
            return base + "\n[auto-push:on]\n" + push_result
        else:
            return base + "\n[auto-push:skipped — no MAL OAuth]"
    return base


@mcp.tool()
def anime_remove(mal_id: int, auto_push: bool = True, reload_db: bool = True) -> str:
    """Delete one anime from the SQLite mirror by MAL ID. auto_push=True (default): also DELETEs the entry from MyAnimeList (silently skipped if no OAuth)."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM anime_list WHERE mal_id = ?", (mal_id,))
    c.commit()
    if reload_db: maybe_reload_db_plugin()
    base = f"ok removed:{cur.rowcount} mal_id:{mal_id}"

    if auto_push:
        auth = _load_mal_auth()
        if auth and auth.get("access_token"):
            headers, err = _mal_headers(prefer_oauth=True)
            if err:
                return base + f"\n[auto-push:err {err}]"
            try:
                client = _get_httpx_client()
                resp = client.delete(
                    f"{_MAL_BASE}/anime/{mal_id}/my_list_status",
                    headers=headers,
                )
                # MAL returns 200 on success, 404 if entry wasn't on MAL
                if resp.status_code in (200, 204):
                    return base + "\n[auto-push:on] deleted from MAL"
                elif resp.status_code == 404:
                    return base + "\n[auto-push:on] entry wasn't on MAL (404)"
                else:
                    return base + f"\n[auto-push:err HTTP {resp.status_code}]"
            except Exception as e:
                return base + f"\n[auto-push:err {str(e)[:120]}]"
        else:
            return base + "\n[auto-push:skipped — no MAL OAuth]"
    return base


# Relation types from MAL's related_anime field that count as "you should probably watch this if you liked the source".
_ANIME_REC_RELATIONS = {"sequel", "side_story", "alternative_setting", "alternative_version", "spin_off"}


@mcp.tool()
def anime_push_to_mal(
    dry_run: bool = True,
    limit: int = 500,
    mal_ids: list[int] | None = None,
) -> str:
    """Push local anime_list state to MyAnimeList. For each local entry with a MAL ID, fetches the entry's current my_list_status, diffs against local, pushes only the diff. mal_ids=[...] restricts to those IDs (used internally by auto-push). Requires OAuth. Pass dry_run=False to actually push. Returns action|mal_id|title|change per line."""
    auth = _load_mal_auth()
    if not auth or not auth.get("access_token"):
        return (
            "err: OAuth not configured. Run mal_auth_start first.\n"
            "Credentials at " + str(_mal_auth_path()) + " must include access_token."
        )

    idx = get_vault_index()
    c = idx.conn
    headers, err = _mal_headers(prefer_oauth=True)
    if err:
        return err

    # Pull local
    if mal_ids:
        placeholders = ",".join("?" * len(mal_ids))
        local_rows = c.execute(
            f"SELECT mal_id, title, status, score, eps_watched FROM anime_list "
            f"WHERE mal_id IN ({placeholders}) ORDER BY mal_id LIMIT ?",
            [*mal_ids, limit],
        ).fetchall()
    else:
        local_rows = c.execute(
            "SELECT mal_id, title, status, score, eps_watched FROM anime_list "
            "WHERE mal_id IS NOT NULL ORDER BY mal_id LIMIT ?",
            (limit,),
        ).fetchall()

    actions: list[str] = []
    pushed = 0
    skipped = 0
    errors = 0

    import time
    client = _get_httpx_client()

    for r in local_rows:
        mid = r["mal_id"]
        local_status = r["status"] or ""
        local_score = r["score"] if r["score"] is not None else 0
        local_eps = r["eps_watched"] if r["eps_watched"] is not None else 0

        # Fetch this entry's current MAL state (single small request)
        try:
            resp = client.get(
                f"{_MAL_BASE}/anime/{mid}",
                params={"fields": "my_list_status,num_episodes"},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            rem = data.get("my_list_status") or {}
        except Exception as e:
            actions.append(f"err_fetch|{mid}|{r['title']}|{str(e)[:120]}")
            errors += 1
            continue

        # Determine what to push
        fields: dict = {}
        changes: list[str] = []
        rem_status = rem.get("status", "") if rem else ""
        rem_score = rem.get("score", 0) if rem else 0
        rem_eps = rem.get("num_episodes_watched", 0) if rem else 0

        if local_status and rem_status != local_status:
            fields["status"] = local_status
            changes.append(f"status:{rem_status or '∅'}→{local_status}")
        if local_score > 0 and rem_score != local_score:
            fields["score"] = local_score
            changes.append(f"score:{rem_score}→{local_score}")
        if local_eps > 0 and rem_eps != local_eps:
            fields["num_watched_episodes"] = local_eps
            changes.append(f"eps:{rem_eps}→{local_eps}")

        if not fields:
            skipped += 1
            continue

        change_str = ", ".join(changes)
        if dry_run:
            actions.append(f"would_push|{mid}|{r['title']}|{change_str}")
            continue

        # Actually push
        try:
            resp = client.patch(
                f"{_MAL_BASE}/anime/{mid}/my_list_status",
                data=fields,
                headers=headers,
            )
            resp.raise_for_status()
            actions.append(f"pushed|{mid}|{r['title']}|{change_str}")
            pushed += 1
        except Exception as e:
            actions.append(f"err_push|{mid}|{r['title']}|{str(e)[:120]}")
            errors += 1

        time.sleep(0.15)  # light rate limit

    prefix = "dry-run " if dry_run else "ok "
    header = f"{prefix}scanned:{len(local_rows)} would_push:{len([a for a in actions if a.startswith('would_push')]) if dry_run else pushed} unchanged:{skipped} errors:{errors}"
    return header + "\n" + "\n".join(actions[:300])


_ROMAN_TO_INT = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


def _normalize_anime_title(s: str) -> str:
    """Normalize an anime title for fuzzy comparison: lowercase, strip punctuation/articles,
    normalize season indicators (S2 / 2nd Season / II all → '2'), collapse spaces."""
    s = s.lower()
    # Strip wikilink wrappers
    s = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", s)
    # Drop common decorations
    s = re.sub(r"[\(\)\[\];,.:!?'\"’`/\\\-—–_]", " ", s)
    # Normalize season indicators in any order:
    # "season 2" / "2nd season" / "second season" → "s2"
    s = re.sub(r"\b(1st|first)\s+season\b", " s1 ", s)
    s = re.sub(r"\b(2nd|second)\s+season\b", " s2 ", s)
    s = re.sub(r"\b(3rd|third)\s+season\b", " s3 ", s)
    s = re.sub(r"\b(4th|fourth)\s+season\b", " s4 ", s)
    s = re.sub(r"\b(5th|fifth)\s+season\b", " s5 ", s)
    s = re.sub(r"\bseason\s+(\d+)\b", r" s\1 ", s)
    # "Part 1/2/3" → "p1/p2/p3"
    s = re.sub(r"\bpart\s+(\d+)\b", r" p\1 ", s)
    # Roman numerals as standalone words → arabic
    tokens = []
    for tok in s.split():
        if tok in _ROMAN_TO_INT:
            tokens.append(_ROMAN_TO_INT[tok])
        else:
            tokens.append(tok)
    s = " ".join(tokens)
    # Drop noise words that distinguish entries on MAL but humans elide
    s = re.sub(r"\b(the|a|an|of|to|de)\b", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _titles_match(local: str, mal_title: str, mal_alt_en: str = "") -> bool:
    """Heuristic title match. Returns True if local title is close to MAL's title or English title.
    Tolerates seasons numbering, episode-arc suffixes, and partial overlap."""
    if not local or not mal_title:
        return False
    L = _normalize_anime_title(local)
    candidates = [_normalize_anime_title(mal_title)]
    if mal_alt_en:
        candidates.append(_normalize_anime_title(mal_alt_en))
    for cand in candidates:
        if not cand:
            continue
        # Exact match
        if L == cand:
            return True
        # One contains the other (handles "Frieren S2" vs "Sousou no Frieren 2nd Season")
        if cand in L or L in cand:
            return True
        # Significant word overlap (>= 50% of shorter side, or >= 40% if season tokens align)
        L_words = set(L.split())
        C_words = set(cand.split())
        if not L_words or not C_words:
            continue
        common = L_words & C_words
        shorter = min(len(L_words), len(C_words))
        if not shorter:
            continue
        ratio = len(common) / shorter
        if ratio >= 0.5:
            return True
        # If both sides share at least one content word AND a season token (s1/s2/p1 etc.), accept
        season_tokens = {t for t in common if re.match(r"^[sp]\d+$", t)}
        content_tokens = {t for t in common if t not in season_tokens and len(t) >= 3}
        if season_tokens and content_tokens and ratio >= 0.35:
            return True
    return False


@mcp.tool()
def anime_verify_ids(limit: int = 500, fix: bool = False) -> str:
    """Audit each anime_list row: fetches the MAL title for its MAL ID and compares to the local title. Reports mismatches. fix=True will null out the title for rows with strong mismatches (so the next sync can correct). Returns one line per problem: mismatch|mal_id|local_title|mal_title."""
    auth = _load_mal_auth()
    if not auth:
        return "err: no MAL credentials"
    headers, err = _mal_headers(prefer_oauth=False)  # client_id is enough for /anime/{id}
    if err:
        return err

    idx = get_vault_index()
    c = idx.conn
    rows = c.execute(
        "SELECT mal_id, title FROM anime_list WHERE mal_id IS NOT NULL ORDER BY mal_id LIMIT ?",
        (limit,),
    ).fetchall()

    out: list[str] = []
    checked = 0
    mismatches = 0
    errors = 0
    fixed = 0
    import time
    client = _get_httpx_client()

    for r in rows:
        mid = r["mal_id"]
        local_title = r["title"] or ""
        try:
            resp = client.get(
                f"{_MAL_BASE}/anime/{mid}",
                params={"fields": "alternative_titles"},
                headers=headers,
            )
            if resp.status_code == 404:
                out.append(f"not_found|{mid}|{local_title}|(MAL ID does not exist)")
                mismatches += 1
                if fix:
                    c.execute("UPDATE anime_list SET title = '' WHERE mal_id = ?", (mid,))
                    fixed += 1
                checked += 1
                time.sleep(0.1)
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            errors += 1
            out.append(f"err|{mid}|{local_title}|{str(e)[:80]}")
            continue
        checked += 1
        mal_title = data.get("title", "")
        alt_en = (data.get("alternative_titles") or {}).get("en", "")
        if not _titles_match(local_title, mal_title, alt_en):
            mismatches += 1
            out.append(f"mismatch|{mid}|{local_title}|MAL: {mal_title}")
            if fix:
                c.execute("UPDATE anime_list SET title = '' WHERE mal_id = ?", (mid,))
                fixed += 1
        time.sleep(0.1)  # gentle rate-limit

    if fix:
        c.commit()
        if fixed:
            maybe_reload_db_plugin()
    header = f"checked:{checked} mismatches:{mismatches} errors:{errors}" + (f" fixed:{fixed}" if fix else "")
    return header + ("\n" + "\n".join(out) if out else "")


@mcp.tool()
def anime_pull_from_mal(preserve_notes: bool = True, limit: int = 1000) -> str:
    """Pull the user's full MyAnimeList list into the local anime_list table. Entries from MAL are upserted into the DB. preserve_notes=True (default) keeps any existing local `note` column non-empty; pulls don't overwrite manual notes. Requires OAuth. Returns added|updated|errors counts plus per-entry actions for the first 100 changes."""
    auth = _load_mal_auth()
    if not auth or not auth.get("access_token"):
        return (
            "err: OAuth not configured. Run mal_auth_start first.\n"
            "Credentials at " + str(_mal_auth_path()) + " must include access_token."
        )

    headers, err = _mal_headers(prefer_oauth=True)
    if err:
        return err

    idx = get_vault_index()
    c = idx.conn
    now = datetime.now().isoformat(timespec="seconds")

    added = 0
    updated = 0
    errors = 0
    actions: list[str] = []

    # MAL's paging.next URL is unreliable for large lists (it occasionally regresses
    # the offset and returns duplicates), so paginate manually with explicit offset.
    base_url = f"{_MAL_BASE}/users/@me/animelist"
    page_size = 100
    offset = 0
    seen = 0
    seen_ids: set[int] = set()
    try:
        client = _get_httpx_client()
        while seen < limit:
            resp = client.get(
                base_url,
                params={
                    "fields": "list_status,num_episodes",
                    "limit": page_size,
                    "offset": offset,
                    "nsfw": "true",
                },
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            page_items = data.get("data", [])
            if not page_items:
                break
            new_in_page = 0
            for item in page_items:
                if seen >= limit:
                    break
                node = item.get("node", {})
                if node.get("id") in seen_ids:
                    continue  # skip dup from any paging glitch
                seen_ids.add(node.get("id", -1))
                seen += 1
                new_in_page += 1
                ls = item.get("list_status", {}) or {}
                mal_id = node.get("id")
                if not mal_id:
                    continue
                title = node.get("title", "")
                eps_total = node.get("num_episodes") or None
                status = ls.get("status", "") or ""
                score = ls.get("score") if ls.get("score") else None
                eps_watched = ls.get("num_episodes_watched") or None
                start_date = ls.get("start_date", "") or ""
                end_date = ls.get("finish_date", "") or ""

                existing = c.execute(
                    "SELECT mal_id, note, priority FROM anime_list WHERE mal_id = ?", (mal_id,)
                ).fetchone()
                # Preserve existing note and priority unless preserve_notes is False
                if existing and preserve_notes:
                    keep_note = existing["note"] or ""
                    keep_priority = existing["priority"] or ""
                else:
                    keep_note = ""
                    keep_priority = ""

                c.execute(
                    "INSERT OR REPLACE INTO anime_list "
                    "(mal_id, title, status, score, start_date, end_date, eps_watched, eps_total, priority, note, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (mal_id, title, status, score, start_date, end_date,
                     eps_watched, eps_total, keep_priority, keep_note, now),
                )
                if existing:
                    updated += 1
                    if len(actions) < 100:
                        actions.append(f"updated|{mal_id}|{title}|{status}")
                else:
                    added += 1
                    if len(actions) < 100:
                        actions.append(f"added|{mal_id}|{title}|{status}")
            # If the page returned no new IDs, we've reached the end (or paging looped)
            if new_in_page == 0:
                break
            # Advance by the page size we asked for, not by what came back —
            # MAL's response sometimes returns a partial page even mid-list.
            offset += page_size
            if len(page_items) < page_size:
                # Last partial page reached
                break
    except Exception as e:
        errors += 1
        actions.append(f"err|{str(e)[:200]}")

    c.commit()
    # Single reload after the bulk pull (per-row reloads would spam Obsidian).
    if added or updated:
        maybe_reload_db_plugin()
    header = f"ok scanned:{seen} added:{added} updated:{updated} errors:{errors}"
    return header + "\n" + "\n".join(actions)


_ANIME_FULL_LIST_PATH = "30_Episodic/Anime/Anime - Full List.md"


@mcp.tool()
def anime_render_full_list(output_path: str = "") -> str:
    """Regenerate the auto-generated Anime - Full List view from the SQLite anime_list table.
    Read-only static dump grouped by status. The Anime hub uses live ```sql blocks via the
    SQLite DB plugin; this file is a fallback for when the plugin isn't available."""
    target = output_path or _ANIME_FULL_LIST_PATH
    note = safe_path(target)
    idx = get_vault_index()
    c = idx.conn

    rows = c.execute(
        "SELECT mal_id, title, status, score, eps_watched, eps_total, note "
        "FROM anime_list ORDER BY status, score DESC NULLS LAST, title COLLATE NOCASE"
    ).fetchall()

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["status"] or "(unknown)", []).append(r)

    section_order = [
        ("watching", "📺 Watching"),
        ("on_hold", "⏸ On Hold"),
        ("completed", "✅ Completed"),
        ("dropped", "🛑 Dropped"),
        ("plan_to_watch", "📌 Plan to Watch"),
    ]

    now = datetime.now().date().isoformat()
    total = len(rows)

    lines = [
        "---",
        "type: episodic",
        "status: active",
        "tags:",
        "  - anime",
        "  - generated",
        f"last_updated: {now}",
        "---",
        "",
        "# Anime — Full List",
        "",
        f"*Auto-generated from `anime_list` SQLite table on {now}. **Do not edit by hand** — changes will be overwritten next time `anime_render_full_list` runs. The interactive view lives in [[Anime]].*",
        "",
        f"**Total:** {total} entries",
        "",
        "---",
        "",
    ]

    def _emit(status_key: str, heading: str, entries: list) -> None:
        lines.append(f"## {heading} ({len(entries)})")
        lines.append("")
        lines.append("| MAL ID | Title | Score | Eps |")
        lines.append("|------:|-------|------:|-----|")
        for r in entries:
            score = r["score"] if r["score"] is not None else ""
            eps_w = r["eps_watched"] if r["eps_watched"] is not None else ""
            eps_t = r["eps_total"] if r["eps_total"] is not None else ""
            eps = f"{eps_w}/{eps_t}" if eps_w != "" or eps_t != "" else ""
            title_safe = (r["title"] or "").replace("|", "\\|")
            lines.append(f"| {r['mal_id']} | {title_safe} | {score} | {eps} |")
        lines.append("")

    for status_key, heading in section_order:
        entries = groups.get(status_key, [])
        if entries:
            _emit(status_key, heading, entries)

    # Any non-standard status entries
    for status_key, entries in groups.items():
        if status_key in {s for s, _ in section_order}:
            continue
        _emit(status_key, f"❓ {status_key}", entries)

    lines.extend([
        "## Related Notes",
        "",
        "- [[Anime]] — interactive hub with live SQL views",
        "- [[Gaming]]",
    ])

    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _notify_index_of_write(note)
    return f"ok {target}|{total} rows"


@mcp.tool()
def anime_find_missing_sequels(
    min_score: int = 7,
    include_on_hold: bool = True,
    include_watching: bool = True,
    limit: int = 200,
) -> str:
    """For each anime in your list above a score threshold, query MAL related_anime and report sequels/side-stories you haven't logged. Hits MAL ~1 req/source — uses your client_id (no OAuth needed). Returns missing_mal_id|missing_title|relation|source_title|source_score per line, plus a [scanned:N|missing:M] header."""
    import time
    idx = get_vault_index()
    c = idx.conn

    # Build the set of known MAL IDs (anything in anime_list — user has logged it in some state)
    known = {row["mal_id"] for row in c.execute("SELECT mal_id FROM anime_list").fetchall()}
    if not known:
        return "none (anime_list is empty — run anime_pull_from_mal first)"

    # Pick the source rows: high-scored completed + optionally on_hold/watching
    where = []
    where.append(f"(status = 'completed' AND score IS NOT NULL AND score >= {int(min_score)})")
    if include_on_hold:
        where.append("status = 'on_hold'")
    if include_watching:
        where.append("status = 'watching'")
    sql = f"SELECT mal_id, title, status, score FROM anime_list WHERE {' OR '.join(where)} ORDER BY score DESC NULLS LAST, title"
    sources = c.execute(sql).fetchall()

    out: list[str] = []
    scanned = 0
    seen_missing: set[int] = set()  # avoid duplicate rows when multiple sources share a sequel

    for src in sources:
        if len(out) >= limit:
            break
        try:
            data, err = _mal_get(f"/anime/{src['mal_id']}", {"fields": "title,related_anime"})
            scanned += 1
            if err:
                continue
            related = (data or {}).get("related_anime") or []
            for rel in related:
                rel_type = (rel.get("relation_type") or "").lower()
                if rel_type not in _ANIME_REC_RELATIONS:
                    continue
                node = rel.get("node") or {}
                rel_id = node.get("id")
                rel_title = node.get("title", "")
                if not rel_id or rel_id in known or rel_id in seen_missing:
                    continue
                seen_missing.add(rel_id)
                out.append(
                    f"{rel_id}|{rel_title}|{rel_type}|{src['title']}|{src['score'] if src['score'] is not None else '-'}"
                )
                if len(out) >= limit:
                    break
        except Exception:
            continue
        # Light rate-limit so we don't hammer MAL
        time.sleep(0.15)

    if not out:
        return f"[scanned:{scanned}|missing:0]\nnone — your list looks complete for sequels/side-stories of the entries scanned."
    header = f"[scanned:{scanned}|missing:{len(out)}]"
    return header + "\n" + "\n".join(out)


