"""Matplotlib chart rendering for Discord embeds + Obsidian.

Generates PNGs from vault data (weight, kcal, macros, habits, etc.),
saves them under `40_Attachments/Charts/YYYY-MM/` so they archive
alongside meals + sessions, and queues them as Discord embed
attachments via the standard embed-queue mechanism.

Design:
- Matplotlib's 'Agg' backend is set at import time → headless, no
  X11 needed, safe inside Docker.
- Style is dark to match Discord embed cards (#2b2d31 background,
  light grid, accent colors from the existing palette in
  `_iris/tools/discord.py`).
- Every chart writes to a deterministic vault path so re-running the
  same tool with the same args overwrites instead of accumulating
  cruft.
- The PNG is referenced in the embed via `attachment://filename.png`;
  the actual `discord.File` upload is wired in `docker/bot.py`'s
  drain loop.

Public tools (all @mcp.tool):
- embed_weight_chart      — line, kg over time + target line
- embed_kcal_chart        — daily kcal bars vs target intake line
- embed_macro_pie         — pie chart of P/C/F split for a day or window
- embed_habit_duration    — line/bar, duration_min for one habit over time
- embed_habit_consistency — stacked-bar of daily habit completion across all
- embed_chart             — generic SQL-driven chart (line/bar/pie)
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# Headless backend — must be set BEFORE pyplot/matplotlib.figure imports.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter, AutoDateLocator
from matplotlib.figure import Figure

from .. import mcp
from ..core import get_vault_index, get_vault_root


# =============================================================================
# Style + output helpers
# =============================================================================

# Discord embed card background is roughly #2b2d31; the embed image area on
# top of that renders at ~#1e1f22. Pick the slightly-darker option so the
# chart "fits" visually inside the embed.
_BG = "#1e1f22"
_FG = "#dbdee1"
_GRID = "#3a3c41"
_ACCENT = {
    "green":   "#10B981",
    "yellow":  "#F59E0B",
    "red":     "#EF4444",
    "blue":    "#3B82F6",
    "violet":  "#8B5CF6",
    "pink":    "#EC4899",
    "gray":    "#6B7280",
    "indigo":  "#6366F1",
}
# Default ordering for multi-series plots (matches the embed-color palette).
_PALETTE: list[str] = [
    _ACCENT["green"], _ACCENT["blue"], _ACCENT["violet"], _ACCENT["yellow"],
    _ACCENT["pink"], _ACCENT["red"], _ACCENT["indigo"], _ACCENT["gray"],
]

_CHARTS_DIR = "40_Attachments/Charts"

# Map slug → embed color name for the title sidebar in Discord.
_DEFAULT_COLORS: dict[str, str] = {
    "weight": "green",
    "kcal":   "blue",
    "macro":  "violet",
    "habit":  "green",
    "chart":  "indigo",
}


def _apply_style(fig: Figure, ax) -> None:
    """Common dark-mode styling for a single-axes figure."""
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    for spine in ax.spines.values():
        spine.set_color(_GRID)
    ax.tick_params(colors=_FG, which="both", labelsize=9)
    ax.title.set_color(_FG)
    ax.xaxis.label.set_color(_FG)
    ax.yaxis.label.set_color(_FG)
    ax.grid(True, color=_GRID, linewidth=0.6, alpha=0.7)
    ax.set_axisbelow(True)


def _safe_filename(text: str, max_len: int = 60) -> str:
    """Slug-like filename component — alphanum + dash, no spaces."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "chart").lower()).strip("-")
    return (s[:max_len] or "chart").strip("-") or "chart"


def _output_path(slug: str, when: Optional[datetime] = None) -> Path:
    """Build a deterministic vault-relative path for the PNG and make
    the parent directory. Returns the absolute Path on the host."""
    dt = when or datetime.now()
    yyyymm = dt.strftime("%Y-%m")
    root = get_vault_root()
    out_dir = root / _CHARTS_DIR / yyyymm
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{dt.strftime('%Y-%m-%d_%H%M')}_{_safe_filename(slug)}.png"


def _save(fig: Figure, slug: str) -> tuple[str, str]:
    """Render the figure to PNG under the vault and close it. Returns
    (vault_rel_path, absolute_path_str). Closing the figure is essential
    in a long-running process — matplotlib's pyplot state leaks otherwise.
    """
    path = _output_path(slug)
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor(), dpi=130, bbox_inches="tight")
    plt.close(fig)
    rel = str(path.relative_to(get_vault_root())).replace("\\", "/")
    return rel, str(path)


def _build_chart_embed(
    title: str,
    description: str,
    color: str,
    vault_rel_path: str,
    abs_path: str,
    footer: str = "",
) -> dict:
    """Build the embed dict that gets queued. The bot's drain loop reads
    `image.attachment_path` and converts it to a `discord.File` on send.
    """
    embed: dict[str, Any] = {
        "title": title[:256],
        "color": _color_int(color),
        "image": {
            "url": f"attachment://{Path(abs_path).name}",
            "attachment_path": abs_path,
        },
    }
    if description:
        embed["description"] = description[:2048]
    if footer:
        embed["footer"] = footer
    # Drop a wikilink-friendly note so the same chart is also browsable
    # in Obsidian (the PNG lives under the vault, after all).
    return embed


def _color_int(name: str) -> int:
    """Convert palette name to int (for Discord embed color)."""
    hex_str = _ACCENT.get((name or "").strip().lower())
    if not hex_str:
        return int("3B82F6", 16)  # blue default
    return int(hex_str.lstrip("#"), 16)


def _enqueue(channel_id: Optional[int], embed: dict) -> str:
    """Delegate to discord.py's `_enqueue_embed` (with attachment-aware
    extension). Imported lazily so charts.py can be imported on systems
    where discord.py isn't installed (e.g. ad-hoc CLI usage).
    """
    from .discord import _enqueue_embed  # noqa: PLC0415
    return _enqueue_embed(channel_id, embed)


# =============================================================================
# Tool 1: Weight chart (line)
# =============================================================================


@mcp.tool()
def embed_weight_chart(
    days: int = 60,
    color: str = "green",
    channel_id: Optional[int] = None,
) -> str:
    """Line chart of weight (kg) over the last N days, with the target
    weight as a dashed reference line.

    Args:
        days: Window size. Default 60 (~2 months — long enough to see
            real trend through daily water-weight noise).
        color: Line color name (green / blue / violet / yellow / pink /
            red / indigo / gray).
    """
    days = max(7, min(int(days), 730))
    idx = get_vault_index()
    c = idx.conn
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = c.execute(
        "SELECT substr(measured_at, 1, 10) AS day, kg "
        "FROM weights WHERE measured_at >= ? "
        "ORDER BY measured_at ASC",
        (cutoff,),
    ).fetchall()
    if not rows:
        return f"err: no weight readings in the last {days} days — log one with `log_weight(kg)`"

    xs = [datetime.strptime(r["day"], "%Y-%m-%d") for r in rows]
    ys = [float(r["kg"]) for r in rows]

    fig, ax = plt.subplots(figsize=(9, 4.5))
    _apply_style(fig, ax)
    ax.plot(xs, ys, marker="o", markersize=5, linewidth=2,
            color=_ACCENT.get(color, _ACCENT["green"]), label="Weight (kg)")

    # Target line from health_profile (if set).
    target_row = c.execute(
        "SELECT target_kg FROM health_profile WHERE id = 1"
    ).fetchone()
    target_kg = target_row["target_kg"] if target_row else None
    if target_kg:
        ax.axhline(target_kg, linestyle="--", linewidth=1.4,
                   color=_ACCENT["yellow"], alpha=0.85,
                   label=f"Target ({target_kg:.1f} kg)")
        ax.legend(loc="upper right", facecolor=_BG, edgecolor=_GRID,
                  labelcolor=_FG, fontsize=9)

    ax.set_title(f"Weight trend · last {days} days", fontsize=12)
    ax.set_ylabel("kg")
    ax.xaxis.set_major_locator(AutoDateLocator())
    ax.xaxis.set_major_formatter(DateFormatter("%b %d"))
    fig.autofmt_xdate(rotation=30, ha="right")

    rel, abs_path = _save(fig, "weight-trend")
    delta = ys[-1] - ys[0]
    sign = "+" if delta >= 0 else ""
    desc = (
        f"**{ys[0]:.1f} → {ys[-1]:.1f} kg** "
        f"({sign}{delta:.1f} kg over {(xs[-1] - xs[0]).days or 1} days, "
        f"{len(ys)} reading{'s' if len(ys) != 1 else ''})"
    )
    if target_kg:
        to_go = ys[-1] - target_kg
        desc += f" · {to_go:+.1f} kg from target"
    embed = _build_chart_embed(
        title="⚖️ Weight trend",
        description=desc,
        color=color,
        vault_rel_path=rel,
        abs_path=abs_path,
        footer=f"embed_weight_chart · {rel}",
    )
    return _enqueue(channel_id, embed)


# =============================================================================
# Tool 2: Daily kcal chart (bar + target line)
# =============================================================================


@mcp.tool()
def embed_kcal_chart(
    days: int = 14,
    color: str = "blue",
    channel_id: Optional[int] = None,
) -> str:
    """Bar chart of daily kcal intake over the last N days, with the
    target intake overlaid as a horizontal line. Bars are coloured green
    when under target, yellow when within ±10 %, red when over.

    Args:
        days: Window size. Default 14 (two weeks — captures weekly cycle).
        color: Bar color name (used for fallback / target-met bars).
    """
    days = max(3, min(int(days), 365))
    idx = get_vault_index()
    c = idx.conn
    today = date.today()
    start = today - timedelta(days=days - 1)
    rows = c.execute(
        "SELECT day, total_kcal FROM meals_daily "
        "WHERE day >= ? AND day <= ? ORDER BY day",
        (start.isoformat(), today.isoformat()),
    ).fetchall()
    if not rows:
        return f"err: no meals logged in the last {days} days — log one with `log_meal(...)`"

    # Build a dense series so missing days show as 0 bars (visually obvious).
    days_seq: list[date] = [start + timedelta(days=i) for i in range(days)]
    by_day = {r["day"]: int(r["total_kcal"]) for r in rows}
    ys = [by_day.get(d.isoformat(), 0) for d in days_seq]

    # Target line from health (target_intake). Re-use the helper from
    # health.py rather than duplicating the BMR math.
    try:
        from .health import _target_intake_kcal  # noqa: PLC0415
        target = _target_intake_kcal()
    except ImportError:
        target = None

    # Per-bar colour based on target alignment.
    bar_colors: list[str] = []
    for kcal in ys:
        if kcal == 0:
            bar_colors.append(_ACCENT["gray"])
            continue
        if target is None:
            bar_colors.append(_ACCENT.get(color, _ACCENT["blue"]))
            continue
        if kcal < target * 0.9:
            bar_colors.append(_ACCENT["green"])
        elif kcal <= target * 1.1:
            bar_colors.append(_ACCENT["yellow"])
        else:
            bar_colors.append(_ACCENT["red"])

    fig, ax = plt.subplots(figsize=(9, 4.5))
    _apply_style(fig, ax)
    ax.bar(days_seq, ys, color=bar_colors, edgecolor=_BG, linewidth=0.5)
    if target:
        ax.axhline(target, linestyle="--", linewidth=1.4,
                   color=_ACCENT["blue"], alpha=0.85,
                   label=f"Target ({target} kcal)")
        ax.legend(loc="upper right", facecolor=_BG, edgecolor=_GRID,
                  labelcolor=_FG, fontsize=9)

    ax.set_title(f"Daily kcal · last {days} days", fontsize=12)
    ax.set_ylabel("kcal")
    ax.xaxis.set_major_locator(AutoDateLocator())
    ax.xaxis.set_major_formatter(DateFormatter("%b %d"))
    fig.autofmt_xdate(rotation=30, ha="right")

    rel, abs_path = _save(fig, "kcal")
    logged_days = sum(1 for k in ys if k)
    avg = (sum(ys) / logged_days) if logged_days else 0
    desc = (
        f"**{logged_days}/{days} days logged** · "
        f"avg {avg:.0f} kcal/day"
    )
    if target:
        desc += f" · target {target} kcal"
    embed = _build_chart_embed(
        title="🍽️ Daily kcal",
        description=desc,
        color=color,
        vault_rel_path=rel,
        abs_path=abs_path,
        footer=f"embed_kcal_chart · {rel}",
    )
    return _enqueue(channel_id, embed)


# =============================================================================
# Tool 3: Macro pie chart
# =============================================================================


@mcp.tool()
def embed_macro_pie(
    date_or_window: str = "today",
    color: str = "violet",
    channel_id: Optional[int] = None,
) -> str:
    """Pie chart of macronutrient split (Protein / Carbs / Fat) by calories.

    Args:
        date_or_window: 'today' (default), 'yesterday', ISO date, OR
            'last_7d' / 'last_30d' for windowed totals.

    Macros are converted to kcal (P×4, C×4, F×9) before pie-ing so the
    slices reflect actual energy contribution, not gram-weight.
    """
    idx = get_vault_index()
    c = idx.conn
    label = date_or_window.strip().lower()
    where = ""
    params: list = []
    if label == "today":
        where = "WHERE substr(eaten_at, 1, 10) = date('now')"
        title_suffix = "today"
    elif label == "yesterday":
        where = "WHERE substr(eaten_at, 1, 10) = date('now', '-1 day')"
        title_suffix = "yesterday"
    elif label == "last_7d":
        where = "WHERE eaten_at >= datetime('now', '-7 days')"
        title_suffix = "last 7 days"
    elif label == "last_30d":
        where = "WHERE eaten_at >= datetime('now', '-30 days')"
        title_suffix = "last 30 days"
    else:
        try:
            d = datetime.strptime(label[:10], "%Y-%m-%d").date()
        except ValueError:
            return f"err: date_or_window must be 'today' / 'yesterday' / ISO date / 'last_7d' / 'last_30d' (got '{date_or_window}')"
        where = "WHERE substr(eaten_at, 1, 10) = ?"
        params.append(d.isoformat())
        title_suffix = d.isoformat()

    row = c.execute(
        "SELECT "
        " COALESCE(SUM(protein_g), 0) AS p, "
        " COALESCE(SUM(carbs_g), 0)   AS c, "
        " COALESCE(SUM(fat_g), 0)     AS f, "
        " COUNT(*) AS n "
        f"FROM meals {where}",
        params,
    ).fetchone()
    if row["n"] == 0:
        return f"err: no meals logged for {title_suffix} — log one with `log_meal(...)`"
    p, ca, fa = float(row["p"]), float(row["c"]), float(row["f"])
    p_kcal = p * 4
    c_kcal = ca * 4
    f_kcal = fa * 9
    total_kcal = p_kcal + c_kcal + f_kcal
    if total_kcal <= 0:
        return (
            f"err: meals for {title_suffix} have no macro data — log meals "
            "with protein_g / carbs_g / fat_g (e.g. from a nutrition label)"
        )

    fig, ax = plt.subplots(figsize=(7, 5))
    _apply_style(fig, ax)
    ax.set_facecolor(_BG)
    # Hide axis ticks/grid for the pie — pies don't need them.
    ax.grid(False)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    labels = [
        f"Protein\n{p:.0f} g · {p_kcal:.0f} kcal",
        f"Carbs\n{ca:.0f} g · {c_kcal:.0f} kcal",
        f"Fat\n{fa:.0f} g · {f_kcal:.0f} kcal",
    ]
    sizes = [p_kcal, c_kcal, f_kcal]
    colors = [_ACCENT["green"], _ACCENT["blue"], _ACCENT["yellow"]]
    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=colors,
        autopct=lambda pct: f"{pct:.0f}%" if pct >= 4 else "",
        startangle=90, counterclock=False,
        wedgeprops={"edgecolor": _BG, "linewidth": 2},
        textprops={"color": _FG, "fontsize": 10},
    )
    for at in autotexts:
        at.set_color(_BG)
        at.set_fontweight("bold")
    ax.set_title(f"Macro split · {title_suffix} ({total_kcal:.0f} kcal)", fontsize=12)

    rel, abs_path = _save(fig, f"macro-{label}")
    p_pct = p_kcal / total_kcal * 100
    desc = (
        f"**P {p_pct:.0f}% · C {c_kcal/total_kcal*100:.0f}% · "
        f"F {f_kcal/total_kcal*100:.0f}%** · "
        f"{row['n']} meal{'s' if row['n'] != 1 else ''} contributing"
    )
    embed = _build_chart_embed(
        title=f"🥗 Macros · {title_suffix}",
        description=desc,
        color=color,
        vault_rel_path=rel,
        abs_path=abs_path,
        footer=f"embed_macro_pie · {rel}",
    )
    return _enqueue(channel_id, embed)


# =============================================================================
# Tool 4: Habit duration over time (line/bar)
# =============================================================================


@mcp.tool()
def embed_habit_duration(
    habit_id: int,
    days: int = 30,
    chart_kind: str = "bar",
    color: str = "green",
    channel_id: Optional[int] = None,
) -> str:
    """Plot `duration_min` for a single habit's logs over the last N days.

    Use case: track best handstand hold over time, asian-squat duration
    progression, daily meditation length, etc. Missing days render as
    zero-height bars so streaks are visually obvious.

    Args:
        habit_id: From `habit_list`.
        days: Window. Default 30.
        chart_kind: 'bar' (default) or 'line'.
    """
    days = max(3, min(int(days), 365))
    if chart_kind not in ("bar", "line"):
        return f"err: chart_kind must be 'bar' or 'line' (got '{chart_kind}')"
    idx = get_vault_index()
    c = idx.conn
    h = c.execute("SELECT id, name, icon FROM habits WHERE id = ?", (habit_id,)).fetchone()
    if not h:
        return f"err: no habit with id {habit_id}"
    today = date.today()
    start = today - timedelta(days=days - 1)
    rows = c.execute(
        "SELECT day, duration_min FROM habit_logs "
        "WHERE habit_id = ? AND day >= ? AND day <= ? AND done = 1 "
        "ORDER BY day",
        (habit_id, start.isoformat(), today.isoformat()),
    ).fetchall()
    if not rows:
        return f"err: no logs for habit {h['name']!r} in the last {days} days"
    by_day = {r["day"]: (r["duration_min"] or 0) for r in rows}
    days_seq: list[date] = [start + timedelta(days=i) for i in range(days)]
    ys = [by_day.get(d.isoformat(), 0) for d in days_seq]

    fig, ax = plt.subplots(figsize=(9, 4.5))
    _apply_style(fig, ax)
    accent = _ACCENT.get(color, _ACCENT["green"])
    if chart_kind == "bar":
        bar_colors = [accent if y > 0 else _ACCENT["gray"] for y in ys]
        ax.bar(days_seq, ys, color=bar_colors, edgecolor=_BG, linewidth=0.5)
    else:
        ax.plot(days_seq, ys, marker="o", markersize=4, linewidth=2, color=accent)
    icon = (h["icon"] or "·").strip()
    ax.set_title(f"{icon} {h['name']} · duration over last {days} days", fontsize=12)
    ax.set_ylabel("minutes")
    ax.xaxis.set_major_locator(AutoDateLocator())
    ax.xaxis.set_major_formatter(DateFormatter("%b %d"))
    fig.autofmt_xdate(rotation=30, ha="right")

    rel, abs_path = _save(fig, f"habit-{habit_id}-duration")
    durations = [y for y in ys if y > 0]
    best = max(durations) if durations else 0
    avg = (sum(durations) / len(durations)) if durations else 0
    desc = (
        f"**{len(durations)}/{days} days logged** · "
        f"avg {avg:.1f} min · best {best} min"
    )
    embed = _build_chart_embed(
        title=f"⏱️ {icon} {h['name']} duration",
        description=desc,
        color=color,
        vault_rel_path=rel,
        abs_path=abs_path,
        footer=f"embed_habit_duration · habit:{habit_id} · {rel}",
    )
    return _enqueue(channel_id, embed)


# =============================================================================
# Tool 5: Habit consistency (stacked bar, all habits done per day)
# =============================================================================


@mcp.tool()
def embed_habit_consistency(
    days: int = 30,
    color: str = "green",
    channel_id: Optional[int] = None,
) -> str:
    """Bar chart of "how many habits did I complete each day" over the
    last N days. Coloured by completion rate (green = ≥80% of active
    habits done, yellow = 40-80%, red = <40%).

    Args:
        days: Window. Default 30.
    """
    days = max(3, min(int(days), 365))
    idx = get_vault_index()
    c = idx.conn
    today = date.today()
    start = today - timedelta(days=days - 1)
    rows = c.execute(
        "SELECT day, COUNT(*) AS n FROM habit_logs "
        "WHERE day >= ? AND day <= ? AND done = 1 GROUP BY day",
        (start.isoformat(), today.isoformat()),
    ).fetchall()
    active_count = c.execute(
        "SELECT COUNT(*) AS n FROM habits WHERE status = 'active'"
    ).fetchone()["n"]
    if active_count == 0:
        return "err: no active habits to chart — add one with `habit_upsert(...)`"

    days_seq: list[date] = [start + timedelta(days=i) for i in range(days)]
    by_day = {r["day"]: int(r["n"]) for r in rows}
    ys = [by_day.get(d.isoformat(), 0) for d in days_seq]
    rates = [y / active_count for y in ys]
    bar_colors = [
        _ACCENT["green"] if rate >= 0.8 else
        _ACCENT["yellow"] if rate >= 0.4 else
        (_ACCENT["red"] if y > 0 else _ACCENT["gray"])
        for y, rate in zip(ys, rates)
    ]

    fig, ax = plt.subplots(figsize=(9, 4.5))
    _apply_style(fig, ax)
    ax.bar(days_seq, ys, color=bar_colors, edgecolor=_BG, linewidth=0.5)
    ax.axhline(active_count, linestyle="--", linewidth=1.4,
               color=_ACCENT["green"], alpha=0.6,
               label=f"All {active_count} habits")
    ax.legend(loc="upper right", facecolor=_BG, edgecolor=_GRID,
              labelcolor=_FG, fontsize=9)
    ax.set_title(f"Habit consistency · last {days} days", fontsize=12)
    ax.set_ylabel("habits done")
    ax.set_ylim(0, max(active_count, max(ys, default=0)) + 1)
    ax.xaxis.set_major_locator(AutoDateLocator())
    ax.xaxis.set_major_formatter(DateFormatter("%b %d"))
    fig.autofmt_xdate(rotation=30, ha="right")

    rel, abs_path = _save(fig, "habit-consistency")
    perfect = sum(1 for y in ys if y >= active_count)
    avg = sum(ys) / len(ys) if ys else 0
    desc = (
        f"**avg {avg:.1f}/{active_count} habits/day** · "
        f"{perfect}/{days} perfect days"
    )
    embed = _build_chart_embed(
        title="📊 Habit consistency",
        description=desc,
        color=color,
        vault_rel_path=rel,
        abs_path=abs_path,
        footer=f"embed_habit_consistency · {rel}",
    )
    return _enqueue(channel_id, embed)


# =============================================================================
# Tool 6: Generic SQL-driven chart
# =============================================================================


_VALID_KINDS = {"line", "bar", "pie"}


@mcp.tool()
def embed_chart(
    sql: str,
    chart_kind: str = "line",
    title: str = "",
    x: str = "",
    y: str = "",
    color: str = "blue",
    channel_id: Optional[int] = None,
) -> str:
    """Generic chart escape hatch — run any read-only SQL and render the
    result as a line / bar / pie chart.

    Args:
        sql: Read-only SELECT. Same safety rules as `sqlite_query` (no
            INSERT/UPDATE/DELETE/DROP/ALTER).
        chart_kind: 'line' / 'bar' / 'pie'.
        x: Column name for the x-axis (line/bar). For 'pie' this is the
            label column.
        y: Column name for the y-axis (line/bar). For 'pie' this is the
            value column.
        title: Chart title shown above the plot AND on the embed.
        color: Series color name.

    Auto-detection: if `x` / `y` aren't given, the first two columns of
    the result are used. For 'pie' the first column is treated as labels.
    """
    if chart_kind not in _VALID_KINDS:
        return f"err: chart_kind must be one of {sorted(_VALID_KINDS)}"
    sql = sql.strip().rstrip(";")
    if not sql:
        return "err: empty SQL"
    # Reuse the string-literal-aware safety helpers from sqlite.py so a
    # WHERE clause containing a write keyword in a quoted string doesn't
    # trip the safety check.
    try:
        from .sqlite import _strip_sql_strings_and_comments, _SQL_WRITE_RE  # type: ignore
        sql_clean = _strip_sql_strings_and_comments(sql)
        if _SQL_WRITE_RE.search(sql_clean):
            return "err: write operations are not allowed in embed_chart SQL"
        upper_clean = sql_clean.lstrip().upper()
        if not (upper_clean.startswith("SELECT") or upper_clean.startswith("WITH")):
            return "err: only SELECT (and WITH … SELECT) queries are allowed"
    except ImportError:
        # Fallback — naive but safe-failing if the helpers somehow aren't importable.
        bad = re.compile(r"\b(insert|update|delete|drop|alter|attach|pragma)\b", re.I)
        if bad.search(sql):
            return "err: SQL contains a forbidden write keyword"
    idx = get_vault_index()
    c = idx.conn
    try:
        rows = c.execute(sql).fetchall()
    except sqlite3.Error as e:
        return f"err: SQL execution failed — {e}"
    if not rows:
        return "err: SQL returned 0 rows"
    cols = list(rows[0].keys())
    x_col = x.strip() or cols[0]
    y_col = y.strip() or (cols[1] if len(cols) > 1 else cols[0])
    if x_col not in cols:
        return f"err: x column {x_col!r} not in result columns {cols}"
    if y_col not in cols:
        return f"err: y column {y_col!r} not in result columns {cols}"

    xs_raw = [r[x_col] for r in rows]
    ys_raw = [r[y_col] for r in rows]
    title_text = title or f"{y_col} by {x_col}"

    # Try to interpret x as ISO dates for nicer time-axis formatting.
    xs: list[Any] = []
    is_dates = True
    for v in xs_raw:
        if not isinstance(v, str):
            is_dates = False
            break
        try:
            xs.append(datetime.strptime(v[:10], "%Y-%m-%d"))
        except ValueError:
            is_dates = False
            break
    if not is_dates:
        xs = list(xs_raw)

    fig, ax = plt.subplots(figsize=(9, 4.5))
    _apply_style(fig, ax)
    accent = _ACCENT.get(color, _ACCENT["blue"])

    if chart_kind == "pie":
        try:
            sizes = [float(v) for v in ys_raw]
        except (TypeError, ValueError):
            return f"err: y column {y_col!r} must be numeric for a pie chart"
        ax.grid(False)
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        colors = _PALETTE[: len(sizes)]
        ax.pie(
            sizes, labels=[str(x) for x in xs_raw], colors=colors,
            autopct=lambda pct: f"{pct:.0f}%" if pct >= 4 else "",
            startangle=90, counterclock=False,
            wedgeprops={"edgecolor": _BG, "linewidth": 2},
            textprops={"color": _FG, "fontsize": 10},
        )
    elif chart_kind == "line":
        try:
            ys_num = [float(v) if v is not None else 0.0 for v in ys_raw]
        except (TypeError, ValueError):
            return f"err: y column {y_col!r} must be numeric for a line chart"
        ax.plot(xs, ys_num, marker="o", markersize=4, linewidth=2, color=accent)
        ax.set_ylabel(y_col)
        if is_dates:
            ax.xaxis.set_major_locator(AutoDateLocator())
            ax.xaxis.set_major_formatter(DateFormatter("%b %d"))
            fig.autofmt_xdate(rotation=30, ha="right")
    else:  # bar
        try:
            ys_num = [float(v) if v is not None else 0.0 for v in ys_raw]
        except (TypeError, ValueError):
            return f"err: y column {y_col!r} must be numeric for a bar chart"
        ax.bar(xs, ys_num, color=accent, edgecolor=_BG, linewidth=0.5)
        ax.set_ylabel(y_col)
        if is_dates:
            ax.xaxis.set_major_locator(AutoDateLocator())
            ax.xaxis.set_major_formatter(DateFormatter("%b %d"))
            fig.autofmt_xdate(rotation=30, ha="right")

    ax.set_title(title_text, fontsize=12)
    rel, abs_path = _save(fig, _safe_filename(title_text))
    embed = _build_chart_embed(
        title=title_text,
        description=f"{len(rows)} row{'s' if len(rows) != 1 else ''} · `{x_col}` × `{y_col}`",
        color=color,
        vault_rel_path=rel,
        abs_path=abs_path,
        footer=f"embed_chart · {rel}",
    )
    return _enqueue(channel_id, embed)
