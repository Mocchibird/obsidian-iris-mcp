"""Web search & fetch

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


# ─── from original L8684-8835: Web search & fetch ───
# =============================================================================
# Web search & fetch tools
# =============================================================================

_HTTPX_TIMEOUT = 30  # 15s wasn't enough for Cloudflare-fronted sites (Digitec etc.)

# Browser-realistic User-Agent. Sites with bot filtering (Cloudflare bot
# challenge, Akamai, etc.) instantly block the obvious "ObsidianMemoryBot"
# UA we used to send. Mimicking a recent stable Chrome on macOS gets us
# past basic UA filters. Pages that require JS rendering still won't work
# — for those you'd need a headless-browser tool.
_WEB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_WEB_HEADERS = {
    "User-Agent": _WEB_UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "DNT": "1",
}

# Suppress noisy library loggers for web tools
import logging as _logging
for _ln in ("ddgs", "httpx", "httpcore"):
    _logging.getLogger(_ln).setLevel(_logging.WARNING)


def _get_httpx_client(json_only: bool = False) -> "httpx.Client":
    """Return a configured httpx.Client.

    json_only=True relaxes the browser headers (just UA + Accept JSON) for
    talking to APIs (Reddit, etc.). For HTML scraping always use the full
    set so Cloudflare/Akamai/etc. don't bounce us."""
    import httpx
    if json_only:
        headers = {
            "User-Agent": _WEB_UA,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
    else:
        headers = dict(_WEB_HEADERS)
    return httpx.Client(
        timeout=_HTTPX_TIMEOUT,
        headers=headers,
        follow_redirects=True,
    )


def _html_to_text(html: str, max_chars: int = 8000) -> str:
    """Extract readable text from HTML, stripping boilerplate."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    # Remove script/style/nav/footer noise
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    # Prefer <article> or <main> if present
    body = soup.find("article") or soup.find("main") or soup.find("body") or soup
    text = body.get_text(separator="\n", strip=True)
    # Collapse blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


@mcp.tool()
def web_search(
    query: str,
    kind: str = "web",
    limit: int = 8,
    region: str = "wt-wt",
    time_range: str = "",
) -> str:
    """Search the web via DuckDuckGo. kind: web|news|reddit. time_range: d|w|m|y (empty=all). Returns title|url|snippet per line."""
    limit = max(1, min(limit, 25))
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return "err: pip install ddgs"

    out: list[str] = []
    try:
        ddgs = DDGS()
        if kind == "news":
            results = ddgs.news(query, region=region, timelimit=time_range or None, max_results=limit)
            for r in results:
                title = r.get("title", "")
                url = r.get("url", "")
                snippet = r.get("body", "")[:150]
                source = r.get("source", "")
                date = r.get("date", "")[:10]
                out.append(f"{title}|{url}|{source}|{date}|{snippet}")
        elif kind == "reddit":
            reddit_q = f"site:reddit.com {query}"
            results = ddgs.text(reddit_q, region=region, timelimit=time_range or None, max_results=limit)
            for r in results:
                out.append(f"{r.get('title', '')}|{r.get('href', '')}|{r.get('body', '')[:150]}")
        else:
            results = ddgs.text(query, region=region, timelimit=time_range or None, max_results=limit)
            for r in results:
                out.append(f"{r.get('title', '')}|{r.get('href', '')}|{r.get('body', '')[:150]}")
    except Exception as e:
        return f"err: {str(e)[:300]}"

    if not out:
        return "none"
    header = f"[results:{len(out)}]"
    return f"{header}\n" + "\n".join(out)


_BOT_BLOCK_MARKERS = (
    "cf-browser-verification",      # Cloudflare interstitial
    "Just a moment...",             # CF challenge title
    "Checking your browser before",  # generic CF text
    "Please enable JS and disable",  # Akamai bot manager
    "Access denied | Access denied",  # Akamai 403 page title
    "Pardon Our Interruption",      # Distil Networks
    "captcha-delivery.com",         # DataDome
    "px-captcha",                   # PerimeterX
)


@mcp.tool()
def fetch_url(url: str, max_chars: int = 8000, raw: bool = False) -> str:
    """Fetch a URL and extract readable text. raw=True returns raw HTML (truncated). Max 8k chars default.

    Sends realistic browser headers so most basic bot filters (Cloudflare's
    cheaper tiers, Akamai user-agent blocking) let us through. Sites with
    JS-challenge-only bot detection or strict Cloudflare Turnstile will
    still bounce us — when that happens we surface a clear error so the
    caller knows it's a fundamental block, not a transient timeout.
    """
    max_chars = max(500, min(max_chars, 30000))
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        client = _get_httpx_client()
        resp = client.get(url)
        resp.raise_for_status()
    except Exception as e:
        return f"err: {str(e)[:300]}"

    content_type = resp.headers.get("content-type", "")
    body = resp.text

    # Detect bot-challenge responses BEFORE the caller wastes time on them.
    # These typically arrive as 200 OK with a small CF/Akamai/whatever page.
    if "text/html" in content_type or not content_type:
        head = body[:8000].lower()
        if any(m.lower() in head for m in _BOT_BLOCK_MARKERS):
            return ("err: bot challenge detected (Cloudflare/Akamai/similar). "
                    f"The site is blocking automated fetches of {url}. "
                    "Search results / cached snippets may still be usable; "
                    "for live pricing or stock you may need to open the URL "
                    "in a browser manually.")

    if raw or "text/plain" in content_type or "application/json" in content_type:
        return body[:max_chars]
    if "text/html" in content_type or not content_type:
        return _html_to_text(body, max_chars=max_chars)
    return f"err: unsupported content-type {content_type[:80]}"


@mcp.tool()
def search_reddit(
    query: str,
    subreddit: str = "",
    sort: str = "relevance",
    time_filter: str = "all",
    limit: int = 10,
) -> str:
    """Search Reddit via JSON API. sort: relevance|hot|top|new|comments. time_filter: hour|day|week|month|year|all. Returns title|url|score|comments|snippet per line."""
    limit = max(1, min(limit, 25))
    if subreddit:
        base = f"https://old.reddit.com/r/{subreddit}/search.json"
        params = {"q": query, "restrict_sr": "on", "sort": sort, "t": time_filter, "limit": str(limit)}
    else:
        base = "https://old.reddit.com/search.json"
        params = {"q": query, "sort": sort, "t": time_filter, "limit": str(limit)}

    try:
        client = _get_httpx_client(json_only=True)
        resp = client.get(base, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"err: {str(e)[:300]}"

    posts = data.get("data", {}).get("children", [])
    if not posts:
        return "none"

    out: list[str] = []
    for post in posts[:limit]:
        d = post.get("data", {})
        title = d.get("title", "")[:120]
        permalink = "https://reddit.com" + d.get("permalink", "")
        score = d.get("score", 0)
        num_comments = d.get("num_comments", 0)
        selftext = d.get("selftext", "")[:150].replace("\n", " ")
        sub = d.get("subreddit", "")
        out.append(f"{title}|{permalink}|r/{sub}|↑{score}|💬{num_comments}|{selftext}")

    header = f"[results:{len(out)}]"
    return f"{header}\n" + "\n".join(out)


