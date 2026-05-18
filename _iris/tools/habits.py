"""Habit tracker — daily check-offs with GitHub-style heatmap rendering.

The "did you do the thing today?" companion to skill goals + training
sessions. Hyun-Min's motivating habits: daily BunPro SRS, Robokana,
kanji, asian squat hold, shoulder rehab exercises. Each is a row in
the `habits` table; each done-day is a row in `habit_logs`. The
UNIQUE(habit_id, day) constraint makes "mark done" idempotent —
calling it twice on the same day doesn't double-count.

Public tools (all @mcp.tool() registered):
    habit_upsert       — create/update a habit (cadence, target_time, …)
    habit_remove       — hard-delete (prefer status='archived')
    habit_list         — list habits with last-done + 7d / 30d count
    habit_done         — mark today (or any day) done
    habit_undo         — un-mark a day
    habit_streak       — current consecutive-day streak for a habit
    habit_heatmap      — GitHub-style 7×N square heatmap (markdown)
    habit_pending_today — list habits not yet done today (for reminders)
    habit_status_today — concise "what's left for today" rollup

Heatmap rendering uses Unicode coloured squares (🟩 ⬜ ⬛) so it works
in plain markdown without needing image generation. The matplotlib-
backed PNG version is a planned follow-up; the unicode version is
already readable in Discord embeds and Obsidian notes today.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from .. import mcp
from ..core import get_vault_index, maybe_reload_db_plugin


_VALID_CADENCE = {"daily", "weekdays", "weekly", "every_n_days"}
_VALID_STATUS = {"active", "paused", "archived"}

# Heatmap glyphs — same visual language as GitHub's contribution chart.
_HM_DONE     = "🟩"   # done that day
_HM_PARTIAL  = "🟨"   # logged but partial (done=0.5 → reserved; not used by current API)
_HM_MISSED   = "⬜"   # was an active day; not done
_HM_INACTIVE = "⬛"   # before habit creation, paused, or weekday-off-day


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today_iso() -> str:
    return date.today().isoformat()


def _parse_day(value: str) -> str:
    """Accept 'today' / 'yesterday' / ISO date / ISO datetime → ISO date string."""
    s = (value or "").strip().lower()
    if s in ("", "today"):
        return _today_iso()
    if s == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return _today_iso()


def _cadence_active_on(day: date, cadence: str, cadence_n: Optional[int],
                       habit_created: Optional[date]) -> bool:
    """Should the habit have been done on this day, given its cadence?

    - 'daily' → every day
    - 'weekdays' → Mon-Fri (isoweekday 1-5)
    - 'weekly' → only Mondays (chosen as a pragmatic default; could be
      extended to a specific weekday later)
    - 'every_n_days' → days where (day - created).days % n == 0
    """
    if habit_created and day < habit_created:
        return False
    if cadence == "daily":
        return True
    if cadence == "weekdays":
        return day.isoweekday() <= 5
    if cadence == "weekly":
        return day.isoweekday() == 1
    if cadence == "every_n_days" and cadence_n:
        if habit_created is None:
            return True
        delta = (day - habit_created).days
        return delta >= 0 and delta % max(1, cadence_n) == 0
    return True


# =============================================================================
# Habit CRUD
# =============================================================================


@mcp.tool()
def habit_upsert(
    name: str,
    category: str = "",
    cadence: str = "daily",
    cadence_n: Optional[int] = None,
    target_time: str = "",
    grace_min: Optional[int] = None,
    skill_id: Optional[int] = None,
    injury_id: Optional[int] = None,
    description: str = "",
    icon: str = "",
    status: str = "",
    reload_db: bool = True,
) -> str:
    """Insert or update a habit.

    Uniqueness is by name (case-sensitive in the table, but updates match
    case-insensitively here). Update semantics: passing an empty string
    keeps the existing value, so partial updates work without re-supplying
    every field.

    Args:
        name: Required, unique. e.g. "BunPro reviews", "Asian squat hold".
        category: 'language' / 'rehab' / 'training' / 'mobility' / 'general'.
            Free-form but stick to a small canonical set for clean filtering.
        cadence: 'daily' (default) / 'weekdays' / 'weekly' / 'every_n_days'.
            Drives whether the heatmap renders a square as "missed" vs
            "inactive" on a given day.
        cadence_n: For 'every_n_days', the N. Ignored otherwise.
        target_time: 'HH:MM' for the daily reminder. Empty disables the
            reminder (the habit can still be logged manually).
        grace_min: Minutes after `target_time` before Iris pings. Default
            120 — wide enough that "I'll do it after dinner" doesn't get
            buzzed at, but narrow enough to actually nudge.
        skill_id: Optional FK-by-value to `skill_goals.id`. Use for habits
            that are practice toward a specific goal (e.g. asian squat
            habit ↔ asian squat skill goal).
        injury_id: Optional FK-by-value to `injuries.id`. Use for rehab
            habits (e.g. shoulder rehab habit ↔ left shoulder injury).
        icon: Optional emoji shown in dashboard rows (📖, 🦵, 💪, etc.).
        status: 'active' / 'paused' / 'archived'.
    """
    if not name.strip():
        return "err: name required"
    if cadence and cadence not in _VALID_CADENCE:
        return f"err: cadence must be one of {sorted(_VALID_CADENCE)}"
    if status and status not in _VALID_STATUS:
        return f"err: status must be one of {sorted(_VALID_STATUS)}"
    if target_time and not _looks_like_time(target_time):
        return f"err: target_time must be HH:MM (got '{target_time}')"

    idx = get_vault_index()
    c = idx.conn
    now = _now_iso()
    existing = c.execute(
        "SELECT * FROM habits WHERE name = ? COLLATE NOCASE",
        (name.strip(),),
    ).fetchone()

    def merge(passed: str, col: str) -> str:
        if passed.strip():
            return passed.strip()
        if existing is not None:
            try:
                return existing[col] or ""
            except (KeyError, IndexError):
                return ""
        return ""

    if existing:
        c.execute(
            "UPDATE habits SET "
            "  category = ?, cadence = ?, cadence_n = ?, target_time = ?, "
            "  grace_min = ?, skill_id = ?, injury_id = ?, "
            "  description = ?, icon = ?, status = ?, updated_at = ? "
            "WHERE id = ?",
            (
                merge(category, "category"),
                cadence if cadence else existing["cadence"],
                cadence_n if cadence_n is not None else existing["cadence_n"],
                merge(target_time, "target_time"),
                grace_min if grace_min is not None else existing["grace_min"],
                skill_id if skill_id is not None else existing["skill_id"],
                injury_id if injury_id is not None else existing["injury_id"],
                merge(description, "description"),
                merge(icon, "icon"),
                status if status else existing["status"],
                now,
                existing["id"],
            ),
        )
        c.commit()
        if reload_db:
            maybe_reload_db_plugin()
        return f"ok updated id:{existing['id']}|{name}"

    cur = c.execute(
        "INSERT INTO habits "
        "(name, category, cadence, cadence_n, target_time, grace_min, "
        " skill_id, injury_id, description, icon, status, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name.strip(), category.strip(), cadence or "daily",
            cadence_n, target_time.strip(),
            grace_min if grace_min is not None else 120,
            skill_id, injury_id,
            description.strip(), icon.strip(),
            status or "active", now, now,
        ),
    )
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok inserted id:{cur.lastrowid}|{name}"


def _looks_like_time(s: str) -> bool:
    s = s.strip()
    if len(s) != 5 or s[2] != ":":
        return False
    try:
        hh = int(s[:2])
        mm = int(s[3:])
        return 0 <= hh <= 23 and 0 <= mm <= 59
    except ValueError:
        return False


@mcp.tool()
def habit_remove(habit_id: int, reload_db: bool = True) -> str:
    """Hard-delete a habit and all its logs (cascade). Prefer
    `habit_upsert(status='archived')` to keep history."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} id:{habit_id}"


@mcp.tool()
def habit_list(status: str = "active", limit: int = 50) -> str:
    """List habits with last-done date + 7d / 30d done count.

    Uses the `habits_active` view when status='active', otherwise direct
    table query. Sorted by category then name.
    """
    if status and status not in _VALID_STATUS:
        return f"err: status must be one of {sorted(_VALID_STATUS)} or '' for all"
    limit = max(1, min(int(limit), 200))
    idx = get_vault_index()
    c = idx.conn
    if status == "active":
        rows = c.execute(
            "SELECT id, name, category, cadence, target_time, icon, "
            " last_done, done_7d, done_30d "
            "FROM habits_active LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        sql = (
            "SELECT id, name, category, cadence, target_time, icon, status, "
            " (SELECT MAX(day) FROM habit_logs "
            "  WHERE habit_id = habits.id AND done = 1) AS last_done "
            "FROM habits"
        )
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY status, category, name COLLATE NOCASE LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
    if not rows:
        return f"none — no habits with status='{status or 'any'}'"
    parts = [f"{len(rows)} habit(s):"]
    for r in rows:
        icon = (r["icon"] or "·").strip() or "·"
        cat = f" [{r['category']}]" if r["category"] else ""
        sched = f" @ {r['target_time']}" if r["target_time"] else ""
        last = f" · last: {r['last_done']}" if r["last_done"] else " · never logged"
        counts = ""
        try:
            counts = f" · 7d:{r['done_7d']} 30d:{r['done_30d']}"
        except (KeyError, IndexError):
            pass
        parts.append(
            f"  {icon} id:{r['id']} {r['name']}{cat}{sched}{last}{counts}"
        )
    return "\n".join(parts)


# =============================================================================
# Logging done/undone
# =============================================================================


@mcp.tool()
def habit_done(
    habit_id: int,
    day: str = "today",
    duration_min: Optional[int] = None,
    notes: str = "",
    reload_db: bool = True,
) -> str:
    """Mark a habit done on a day. Idempotent — calling twice on the same
    day updates the existing row instead of duplicating.

    Args:
        habit_id: From `habit_list`.
        day: 'today' (default), 'yesterday', or ISO date.
        duration_min: Optional minutes spent. Useful for habits like
            "asian squat hold" or "shoulder rehab" where total time is
            the interesting metric.
        notes: Free-text per-day notes ("did the easier variation",
            "shoulder felt 7/10 today").
    """
    d = _parse_day(day)
    now = _now_iso()
    idx = get_vault_index()
    c = idx.conn
    habit = c.execute("SELECT id, name FROM habits WHERE id = ?", (habit_id,)).fetchone()
    if not habit:
        return f"err: no habit with id {habit_id}"
    c.execute(
        "INSERT INTO habit_logs (habit_id, day, done, duration_min, notes, created_at) "
        "VALUES (?, ?, 1, ?, ?, ?) "
        "ON CONFLICT(habit_id, day) DO UPDATE SET "
        "  done = 1, "
        "  duration_min = COALESCE(excluded.duration_min, duration_min), "
        "  notes = CASE WHEN excluded.notes != '' THEN excluded.notes ELSE notes END",
        (habit_id, d, duration_min, notes.strip(), now),
    )
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok done · habit:{habit['name']} day:{d}" + (
        f" duration:{duration_min}min" if duration_min else ""
    )


@mcp.tool()
def habit_undo(
    habit_id: int,
    day: str = "today",
    reload_db: bool = True,
) -> str:
    """Un-mark a habit on a day (deletes the log row). Use when you
    mis-logged."""
    d = _parse_day(day)
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute(
        "DELETE FROM habit_logs WHERE habit_id = ? AND day = ?",
        (habit_id, d),
    )
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} habit:{habit_id} day:{d}"


# =============================================================================
# Status / streak / pending
# =============================================================================


@mcp.tool()
def habit_streak(habit_id: int) -> str:
    """Current consecutive-day streak for a habit (counting back from
    today). Returns 0 if today isn't done yet AND yesterday wasn't done.
    A "today not done but yesterday done" streak still counts via the
    last_done date — described in the output.
    """
    idx = get_vault_index()
    c = idx.conn
    h = c.execute("SELECT name, cadence FROM habits WHERE id = ?", (habit_id,)).fetchone()
    if not h:
        return f"err: no habit with id {habit_id}"
    # Pull all done-days in last 365 days, walk back from today.
    rows = c.execute(
        "SELECT day FROM habit_logs "
        "WHERE habit_id = ? AND done = 1 AND day >= date('now', '-365 days') "
        "ORDER BY day DESC",
        (habit_id,),
    ).fetchall()
    done_days = {r["day"] for r in rows}
    if not done_days:
        return f"habit:{h['name']} · streak:0 (never logged)"
    today = date.today()
    streak = 0
    cursor = today
    # If today not done yet, allow starting the streak from yesterday so
    # "I did it yesterday but not today yet" still reads as a live streak.
    if cursor.isoformat() not in done_days:
        cursor -= timedelta(days=1)
    while cursor.isoformat() in done_days:
        streak += 1
        cursor -= timedelta(days=1)
    today_done = today.isoformat() in done_days
    today_tag = " (today ✓)" if today_done else " (today not yet ⏳)"
    return f"habit:{h['name']} · streak:{streak}{today_tag}"


@mcp.tool()
def habit_pending_today() -> str:
    """List active habits not yet done today. Used by the bot's reminder
    loop to decide what to ping about; also useful in chat ("what's left
    for today?").

    Respects `cadence`: 'weekdays' habits don't appear on weekends.
    """
    today = date.today()
    today_iso = today.isoformat()
    idx = get_vault_index()
    c = idx.conn
    rows = c.execute(
        "SELECT h.id, h.name, h.category, h.cadence, h.cadence_n, "
        " h.target_time, h.grace_min, h.icon, h.created_at "
        "FROM habits h "
        "WHERE h.status = 'active' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM habit_logs "
        "    WHERE habit_id = h.id AND day = ? AND done = 1"
        "  )",
        (today_iso,),
    ).fetchall()
    pending: list[dict] = []
    for r in rows:
        created = None
        if r["created_at"]:
            try:
                created = datetime.fromisoformat(r["created_at"]).date()
            except ValueError:
                pass
        if not _cadence_active_on(today, r["cadence"], r["cadence_n"], created):
            continue
        pending.append(dict(r))
    if not pending:
        return "✅ all habits done for today"
    parts = [f"{len(pending)} habit(s) pending today:"]
    for h in pending:
        icon = (h["icon"] or "·").strip() or "·"
        sched = f" @ {h['target_time']}" if h["target_time"] else ""
        parts.append(f"  {icon} id:{h['id']} {h['name']}{sched}")
    return "\n".join(parts)


@mcp.tool()
def habit_status_today() -> str:
    """One-line "X / Y done today" rollup, plus a short list of what's
    pending. Compact enough for a Discord reply."""
    idx = get_vault_index()
    c = idx.conn
    today = date.today()
    today_iso = today.isoformat()
    rows = c.execute(
        "SELECT h.id, h.name, h.cadence, h.cadence_n, h.icon, h.created_at, "
        " EXISTS (SELECT 1 FROM habit_logs "
        "         WHERE habit_id = h.id AND day = ? AND done = 1) AS done "
        "FROM habits h WHERE h.status = 'active'",
        (today_iso,),
    ).fetchall()
    active_today: list[dict] = []
    for r in rows:
        created = None
        if r["created_at"]:
            try:
                created = datetime.fromisoformat(r["created_at"]).date()
            except ValueError:
                pass
        if _cadence_active_on(today, r["cadence"], r["cadence_n"], created):
            active_today.append(dict(r))
    if not active_today:
        return "no active habits today (or weekend / off-day for all of them)"
    done = [h for h in active_today if h["done"]]
    pending = [h for h in active_today if not h["done"]]
    summary = f"**{len(done)}/{len(active_today)} done today**"
    if pending:
        names = ", ".join((h["icon"] or "·").strip() + " " + h["name"] for h in pending)
        return f"{summary} · pending: {names}"
    return f"{summary} ✅"


# =============================================================================
# GitHub-style heatmap
# =============================================================================


@mcp.tool()
def habit_heatmap(habit_id: int, weeks: int = 10) -> str:
    """Render a GitHub-style 7×N heatmap for a habit, where each column
    is a week and each row is a day-of-week (Mon top, Sun bottom).

    The rightmost column is ALWAYS the current week, so each new week
    shifts the whole grid one column to the left (the oldest week
    scrolls off). 10 columns = ~2.5 months of recent history, which fits
    cleanly in a Discord embed field without line-wrapping.

    Glyphs:
        🟩 done · ⬜ missed (active day) · ⬛ inactive (before habit
        created, or off-day for the cadence, or future date)

    Use `weeks=52` for the full GitHub-style year-view; that's wide but
    renders fine in Obsidian. Keep ≤ 10 for Discord embed fields.

    Returns a markdown block with header (date range + directional
    marker), grid, and legend.
    """
    weeks = max(1, min(int(weeks), 52))
    idx = get_vault_index()
    c = idx.conn
    h = c.execute(
        "SELECT id, name, cadence, cadence_n, created_at, icon "
        "FROM habits WHERE id = ?",
        (habit_id,),
    ).fetchone()
    if not h:
        return f"err: no habit with id {habit_id}"
    created = None
    if h["created_at"]:
        try:
            created = datetime.fromisoformat(h["created_at"]).date()
        except ValueError:
            pass

    today = date.today()
    # Align so the rightmost column ends on today; columns are full weeks.
    # Start of the rightmost column = the Monday of this week.
    monday_this_week = today - timedelta(days=today.isoweekday() - 1)
    start = monday_this_week - timedelta(weeks=weeks - 1)
    end = monday_this_week + timedelta(days=6)
    # Pull all done-days in the window in one query.
    done_rows = c.execute(
        "SELECT day FROM habit_logs WHERE habit_id = ? AND done = 1 "
        "AND day >= ? AND day <= ?",
        (habit_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    done = {r["day"] for r in done_rows}

    # Build the grid: 7 rows (Mon-Sun) × `weeks` columns. Each cell glyph
    # depends on (active day for the cadence) × (done? today? future?).
    grid: list[list[str]] = [[] for _ in range(7)]
    for w in range(weeks):
        col_monday = start + timedelta(weeks=w)
        for dow in range(7):
            cell_date = col_monday + timedelta(days=dow)
            if cell_date > today:
                glyph = _HM_INACTIVE
            elif not _cadence_active_on(cell_date, h["cadence"], h["cadence_n"], created):
                glyph = _HM_INACTIVE
            elif cell_date.isoformat() in done:
                glyph = _HM_DONE
            else:
                glyph = _HM_MISSED
            grid[dow].append(glyph)

    # Render: 7 rows with day-of-week labels.
    # Each emoji glyph is double-width, so the directional axis line uses
    # 2 spaces of padding per column to stay aligned. The "→" anchor is
    # on the right because the rightmost column is THIS WEEK.
    dow_labels = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    body_lines = [f"`{dow_labels[i]}` {''.join(grid[i])}" for i in range(7)]
    axis_line = (
        f"     `{start.strftime('%b %d')}`"
        + " " * max(0, 2 * (weeks - 2))
        + "← past · now → "
        + f"`{end.strftime('%b %d')}`"
    )
    # Stats below
    total_active_days = sum(
        1 for w in range(weeks) for dow in range(7)
        if (start + timedelta(weeks=w, days=dow)) <= today
        and _cadence_active_on(
            start + timedelta(weeks=w, days=dow),
            h["cadence"], h["cadence_n"], created,
        )
    )
    # Only count done-days that ALSO pass the cadence + creation check —
    # otherwise back-dated logs from before the habit existed (or on
    # off-days) inflate the percentage above 100 %.
    done_count = sum(
        1 for d in done
        if (_d := datetime.strptime(d, "%Y-%m-%d").date()) <= today
        and _cadence_active_on(_d, h["cadence"], h["cadence_n"], created)
    )
    rate = (done_count / total_active_days * 100) if total_active_days else 0
    icon = (h["icon"] or "·").strip()
    header = (
        f"**{icon} {h['name']}** — last {weeks} weeks "
        f"({done_count}/{total_active_days} active days, {rate:.0f}%)"
    )
    legend = f"_{_HM_DONE} done  {_HM_MISSED} missed  {_HM_INACTIVE} inactive · each column = one week, rows = Mon→Sun_"
    return "\n".join([header, "", *body_lines, axis_line, "", legend])
