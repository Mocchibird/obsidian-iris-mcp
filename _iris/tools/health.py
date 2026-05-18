"""Health tracking — calorie / macro / weight logging.

MCP tools backing Hyun-Min's weight-loss journey (starting May 2026 at
107.5 kg). The goal is friction-free logging from Discord conversations:
drop a food photo → Iris estimates → offers to log; type "weighed X" →
Iris records and surfaces a trend.

Data model:
    meals     — one row per meal/snack/drink, with kcal + optional macros,
                source (photo / label / barcode / restaurant / manual) and
                a confidence band so noisy photo estimates carry their
                uncertainty.
    weights   — one row per weigh-in (manual for now; future smart-scale
                integration would set source='scale').

Helper views (created in core.py):
    meals_daily      — per-day kcal + macro rollup
    weights_weekly   — per-ISO-week min/max/avg weight + reading count

Calorie-estimation philosophy (also baked into the system prompt):
    - Prefer barcode / nutrition-label photos: high confidence, fill macros.
    - Restaurant menu items: medium confidence, typical-portion lookup.
    - Home-cooked photos: low confidence, explicit ±15-25 % bracket via
      kcal_low / kcal_high. Bias the `kcal` field toward the HIGH end of
      the bracket when cutting weight — better to slightly over-count.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from .. import mcp
from ..core import get_vault_index, maybe_reload_db_plugin


# =============================================================================
# Helpers
# =============================================================================

_VALID_SOURCES = {"manual", "photo", "label", "barcode", "restaurant"}
_VALID_CONFIDENCE = {"high", "medium", "low"}


def _parse_when(value: str) -> str:
    """Accept 'now', empty, or ISO-8601 datetime (with or without time) and
    return a normalised ISO-8601 string with second precision.

    We deliberately don't try to be too clever with natural-language parsing
    here — Iris is expected to resolve "after lunch" / "2h ago" upstream and
    pass a real datetime in. This keeps the tool behaviour predictable and
    keeps weird parsing bugs out of the data path.
    """
    s = (value or "").strip().lower()
    now = datetime.now().replace(microsecond=0)
    if s in ("", "now"):
        return now.isoformat(timespec="seconds")
    # Try ISO datetime first, then date-only (assume noon when only date)
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(microsecond=0).isoformat(timespec="seconds")
    except ValueError:
        pass
    try:
        d = datetime.strptime(value[:10], "%Y-%m-%d")
        return d.replace(hour=12, minute=0, second=0).isoformat(timespec="seconds")
    except ValueError:
        return now.isoformat(timespec="seconds")


# =============================================================================
# Meal logging
# =============================================================================


@mcp.tool()
def log_meal(
    description: str,
    kcal: int,
    eaten_at: str = "now",
    kcal_low: Optional[int] = None,
    kcal_high: Optional[int] = None,
    protein_g: Optional[float] = None,
    carbs_g: Optional[float] = None,
    fat_g: Optional[float] = None,
    source: str = "manual",
    confidence: str = "medium",
    photo_path: str = "",
    notes: str = "",
    reload_db: bool = True,
) -> str:
    """Log a meal / snack / drink with calories and optional macros.

    Args:
        description: Free-text meal description, e.g. "braised pork +
            brown rice + side salad".
        kcal: Best single calorie estimate. When working from a photo
            estimate with uncertainty, use the HIGH end of the kcal_low /
            kcal_high range here (cutting-weight bias — better to slightly
            over-count than under-count).
        eaten_at: When the meal was consumed. Accepts "now" (default),
            ISO datetime ("2026-05-18T13:30:00") or just date
            ("2026-05-18", logged at noon). Iris should resolve natural
            language like "lunch" / "an hour ago" upstream.
        kcal_low / kcal_high: Optional uncertainty bracket. Required when
            source='photo' or 'restaurant' so the daily rollup can show
            both the working estimate and a worst-case total.
        protein_g / carbs_g / fat_g: Optional macros in grams. Nutrition
            labels and barcode lookups should fill these; pure home-cooked
            photo estimates may leave them NULL.
        source: One of 'manual', 'photo', 'label', 'barcode', 'restaurant'.
        confidence: 'high' (label / barcode), 'medium' (restaurant / known
            recipe), or 'low' (ambiguous home-cooked photo).
        photo_path: Vault-relative path to the source photo, if any.
        notes: Free-text context — useful for "after gym", "Bu's cooking",
            "shared plate, ~70 % of this".

    Returns:
        Status string with the new row id, e.g. "ok inserted id:42 kcal:670".
    """
    desc = (description or "").strip()
    if not desc:
        return "err: description required"
    if kcal is None or kcal < 0:
        return f"err: kcal must be a non-negative integer (got {kcal!r})"
    if source not in _VALID_SOURCES:
        return f"err: source must be one of {sorted(_VALID_SOURCES)} (got '{source}')"
    if confidence not in _VALID_CONFIDENCE:
        return f"err: confidence must be one of {sorted(_VALID_CONFIDENCE)} (got '{confidence}')"
    if kcal_low is not None and kcal_high is not None and kcal_low > kcal_high:
        return f"err: kcal_low ({kcal_low}) must not exceed kcal_high ({kcal_high})"

    when = _parse_when(eaten_at)
    now = datetime.now().isoformat(timespec="seconds")
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute(
        "INSERT INTO meals "
        "(eaten_at, description, kcal, kcal_low, kcal_high, "
        " protein_g, carbs_g, fat_g, source, confidence, photo_path, "
        " notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (when, desc, int(kcal), kcal_low, kcal_high,
         protein_g, carbs_g, fat_g, source, confidence,
         (photo_path or "").strip() or None,
         (notes or "").strip() or None,
         now),
    )
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return (
        f"ok inserted id:{cur.lastrowid} kcal:{int(kcal)} "
        f"at:{when} source:{source}"
    )


@mcp.tool()
def remove_meal(meal_id: int, reload_db: bool = True) -> str:
    """Delete a meal log row by its id. Useful for fixing duplicates or
    walking back a mis-logged photo estimate."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} id:{meal_id}"


@mcp.tool()
def recent_meals(days: int = 1, limit: int = 50) -> str:
    """Show meals logged in the last N days, most recent first.

    For drill-down or auditing the photo-estimate pipeline. Use
    `daily_calories` for the rollup view.
    """
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 500))
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    idx = get_vault_index()
    c = idx.conn
    rows = c.execute(
        "SELECT id, eaten_at, description, kcal, kcal_low, kcal_high, "
        " source, confidence, notes "
        "FROM meals WHERE eaten_at >= ? "
        "ORDER BY eaten_at DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    if not rows:
        return f"none — no meals logged in the last {days} day(s)"
    parts: list[str] = [f"{len(rows)} meal(s) in the last {days} day(s):"]
    for r in rows:
        bracket = ""
        if r["kcal_low"] is not None and r["kcal_high"] is not None:
            bracket = f" ({r['kcal_low']}–{r['kcal_high']})"
        note = f" — {r['notes']}" if r["notes"] else ""
        parts.append(
            f"  id:{r['id']} {r['eaten_at']} · "
            f"{r['kcal']} kcal{bracket} · "
            f"{r['source']}/{r['confidence']} · {r['description']}{note}"
        )
    return "\n".join(parts)


@mcp.tool()
def daily_calories(date: str = "today") -> str:
    """Show the calorie + macro rollup for a single day (default: today).

    Reads from the `meals_daily` view. Returns a short human-readable
    summary that's safe to drop into a Discord reply.
    """
    if date.lower() == "today":
        day = datetime.now().strftime("%Y-%m-%d")
    elif date.lower() == "yesterday":
        day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        try:
            day = datetime.strptime(date[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return f"err: date must be 'today', 'yesterday', or YYYY-MM-DD (got '{date}')"

    idx = get_vault_index()
    c = idx.conn
    row = c.execute(
        "SELECT meal_count, total_kcal, total_kcal_high, "
        " total_protein_g, total_carbs_g, total_fat_g "
        "FROM meals_daily WHERE day = ?",
        (day,),
    ).fetchone()
    if row is None or row["meal_count"] == 0:
        return f"{day}: no meals logged."
    parts = [
        f"{day}: {row['meal_count']} meal(s), "
        f"{row['total_kcal']} kcal"
    ]
    if row["total_kcal_high"] and row["total_kcal_high"] != row["total_kcal"]:
        parts.append(f"(high-end: {row['total_kcal_high']} kcal)")
    macros: list[str] = []
    if row["total_protein_g"]:
        macros.append(f"P {row['total_protein_g']:.0f}g")
    if row["total_carbs_g"]:
        macros.append(f"C {row['total_carbs_g']:.0f}g")
    if row["total_fat_g"]:
        macros.append(f"F {row['total_fat_g']:.0f}g")
    if macros:
        parts.append("· " + " / ".join(macros))
    return " ".join(parts)


# =============================================================================
# Weight logging
# =============================================================================


@mcp.tool()
def log_weight(
    kg: float,
    measured_at: str = "now",
    notes: str = "",
    source: str = "manual",
    reload_db: bool = True,
) -> str:
    """Log a weigh-in.

    Args:
        kg: Weight in kilograms (e.g. 107.5). Validation only rejects
            values outside 20–400 kg to catch obvious typos.
        measured_at: ISO datetime, "now" (default), or just date.
        notes: Free-text context, e.g. "morning, post-bathroom".
        source: 'manual' (default) or 'scale' (future smart-scale).
    """
    if kg is None or not (20.0 <= float(kg) <= 400.0):
        return f"err: kg must be in [20, 400] (got {kg!r})"
    when = _parse_when(measured_at)
    now = datetime.now().isoformat(timespec="seconds")
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute(
        "INSERT INTO weights (measured_at, kg, notes, source, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (when, float(kg), (notes or "").strip() or None, source, now),
    )
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok inserted id:{cur.lastrowid} kg:{kg} at:{when}"


@mcp.tool()
def remove_weight(weight_id: int, reload_db: bool = True) -> str:
    """Delete a weigh-in row by its id. Useful when logging the same
    morning twice or fixing a typo'd value."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM weights WHERE id = ?", (weight_id,))
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} id:{weight_id}"


@mcp.tool()
def weight_trend(days: int = 30) -> str:
    """Show recent weight readings + delta vs. the earliest in-window
    reading. Default window: 30 days.

    Returns a compact summary suitable for a Discord reply:
      "30 readings · 107.5 → 104.2 kg (-3.3 kg in 28 days) · last: 2026-05-18"

    For a per-week rollup, query the `weights_weekly` view directly via
    `sqlite_query`.
    """
    days = max(1, min(int(days), 3650))
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    idx = get_vault_index()
    c = idx.conn
    rows = c.execute(
        "SELECT measured_at, kg FROM weights WHERE measured_at >= ? "
        "ORDER BY measured_at ASC",
        (cutoff,),
    ).fetchall()
    if not rows:
        return f"none — no weights logged in the last {days} day(s)"
    first = rows[0]
    last = rows[-1]
    delta = last["kg"] - first["kg"]
    sign = "+" if delta >= 0 else ""
    span_days = max(
        1,
        (datetime.fromisoformat(last["measured_at"])
         - datetime.fromisoformat(first["measured_at"])).days,
    )
    return (
        f"{len(rows)} reading(s) · {first['kg']:.1f} → {last['kg']:.1f} kg "
        f"({sign}{delta:.1f} kg in {span_days} day(s)) · "
        f"last: {last['measured_at'][:10]}"
    )
