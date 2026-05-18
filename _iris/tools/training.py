"""Skill goals, injuries, and training sessions.

The "physical capability" half of Iris's health-tracking sphere — sits
alongside the meal/weight tools in `_iris/tools/health.py` and shares
the same dashboard real-estate. Three tables:

    skill_goals       — Long-running targets (free handstand, muscle-up,
                        one-arm pull-up, asian squat, planche, etc.) with
                        text current_level → target progression Iris
                        suggests, plus links to constraining injuries so
                        recommendations stay safe.
    injuries          — First-class rows for active / managing / healed
                        body issues. Hyun-Min's left shoulder is the
                        motivating example. `restrictions` is a free-text
                        list of movements to avoid; Iris reads this BEFORE
                        suggesting any progression that touches the joint.
    training_sessions — Lightweight session log (date, kind, duration,
                        RPE, summary, skill_ids worked). The raw set/rep
                        detail still lives in `30_Episodic/Personal/Gym.md`
                        — this table is for "how many sessions in the last
                        30 days" / "which goals am I actually training" /
                        scheduled health-card aggregates.

Behaviour Iris should follow (mirrored in the system prompt):
    - When recommending a plan for a skill_goal, ALWAYS pull
      `injuries_active` first and respect their `restrictions`.
    - When seeding a progression for the first time, write the multi-step
      plan to `progression` so the user (and Iris in later turns) can see
      it without regenerating from scratch.
    - "Planche" and similar moonshot goals are valid `priority=3` /
      `status='paused'` entries — capture them, mark as someday-maybe,
      don't pretend they're imminent.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .. import mcp
from ..core import get_vault_index, maybe_reload_db_plugin


_VALID_SKILL_STATUS = {"active", "paused", "achieved", "dropped"}
_VALID_INJURY_STATUS = {"active", "managing", "healed"}
_VALID_SEVERITY = {"", "mild", "moderate", "severe"}
_VALID_SIDE = {"", "left", "right", "both"}
_VALID_SESSION_KIND = {
    "", "gym", "calisthenics", "mobility", "physio", "cardio",
    "outdoor", "team_sport", "other",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_when(value: str) -> str:
    """Same as health._parse_when but localised here to keep the module
    self-contained. Accepts 'now', empty, ISO datetime, or date-only.
    """
    s = (value or "").strip().lower()
    now = datetime.now().replace(microsecond=0)
    if s in ("", "now"):
        return now.isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(value).replace(microsecond=0).isoformat(timespec="seconds")
    except ValueError:
        pass
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(
            hour=12, minute=0, second=0
        ).isoformat(timespec="seconds")
    except ValueError:
        return now.isoformat(timespec="seconds")


# =============================================================================
# Skill goals
# =============================================================================


@mcp.tool()
def skill_upsert(
    name: str,
    category: str = "",
    target: str = "",
    current_level: str = "",
    status: str = "",
    priority: Optional[int] = None,
    progression: str = "",
    constraints: str = "",
    constraint_ref_ids: str = "",
    note_path: str = "",
    notes: str = "",
    target_date: str = "",
    achieved_at: str = "",
    reload_db: bool = True,
) -> str:
    """Insert or update a physical-skill goal.

    Args:
        name: Required, unique-ish (matched case-insensitively for updates,
            so "free-standing handstand" updates the existing entry rather
            than creating a near-duplicate).
        category: 'balance' / 'strength' / 'mobility' / 'skill' / 'cardio'.
            Free-form but stick to the canonical set for clean filtering.
        target: Plain-text description of the goal state, e.g.
            "10 s free-standing handstand on flat ground".
        current_level: Plain-text current capability, e.g.
            "30 s wall handstand, head touch".
        status: 'active' (default for new) / 'paused' / 'achieved' /
            'dropped'. Pass empty on UPDATE to leave existing untouched.
        priority: 1 (top focus) / 2 (active) / 3 (someday-maybe). Defaults
            to 2 for new rows.
        progression: Multi-line plan Iris has suggested. Write this when
            seeding so it doesn't get re-derived each conversation.
        constraints: Free-text restrictions ("avoid overhead pressing
            until shoulder cleared"). Iris reads this when suggesting
            sessions.
        constraint_ref_ids: Comma-separated `injuries.id` values this
            goal is gated by. Lets queries find "which goals are blocked
            by the shoulder issue?" without parsing free-text.
        note_path: Vault path to a dedicated note for this goal, if any.
        target_date: Optional 'YYYY-MM-DD' deadline.
        achieved_at: Set when status flips to 'achieved'. ISO date or 'now'.

    Update semantics: passing an empty string for a text field on UPDATE
    leaves the existing value untouched (so you can fix one thing without
    re-supplying everything). To clear a field, edit via `sqlite_query`.
    """
    if not name.strip():
        return "err: name required"
    if status and status not in _VALID_SKILL_STATUS:
        return f"err: status must be one of {sorted(_VALID_SKILL_STATUS)}"

    idx = get_vault_index()
    c = idx.conn
    now = _now_iso()
    existing = c.execute(
        "SELECT * FROM skill_goals WHERE name = ? COLLATE NOCASE",
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

    achieved_iso = _parse_when(achieved_at) if achieved_at.strip() else (
        existing["achieved_at"] if existing else ""
    )
    # When setting status='achieved' without an explicit achieved_at, stamp it now.
    if status == "achieved" and not achieved_iso:
        achieved_iso = now

    if existing:
        c.execute(
            "UPDATE skill_goals SET "
            "  category = ?, target = ?, current_level = ?, "
            "  status = ?, priority = ?, progression = ?, "
            "  constraints = ?, constraint_ref_ids = ?, note_path = ?, "
            "  notes = ?, target_date = ?, achieved_at = ?, updated_at = ? "
            "WHERE id = ?",
            (
                merge(category, "category"),
                merge(target, "target"),
                merge(current_level, "current_level"),
                status or existing["status"],
                priority if priority is not None else existing["priority"],
                merge(progression, "progression"),
                merge(constraints, "constraints"),
                merge(constraint_ref_ids, "constraint_ref_ids"),
                merge(note_path, "note_path"),
                merge(notes, "notes"),
                merge(target_date, "target_date"),
                achieved_iso,
                now,
                existing["id"],
            ),
        )
        c.commit()
        if reload_db:
            maybe_reload_db_plugin()
        return f"ok updated id:{existing['id']}|{name}"

    cur = c.execute(
        "INSERT INTO skill_goals "
        "(name, category, target, current_level, status, priority, "
        " progression, constraints, constraint_ref_ids, note_path, "
        " notes, started_at, target_date, achieved_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name.strip(), category.strip(), target.strip(),
            current_level.strip(), status or "active",
            priority if priority is not None else 2,
            progression.strip(), constraints.strip(),
            constraint_ref_ids.strip(), note_path.strip(),
            notes.strip(),
            now[:10],  # started_at = today (date only)
            target_date.strip(),
            achieved_iso,
            now,
        ),
    )
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok inserted id:{cur.lastrowid}|{name}"


@mcp.tool()
def skill_remove(skill_id: int, reload_db: bool = True) -> str:
    """Delete a skill goal by id. Prefer setting status='dropped' via
    skill_upsert for soft-delete — only use this for typos / dupes."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM skill_goals WHERE id = ?", (skill_id,))
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} id:{skill_id}"


@mcp.tool()
def skill_list(status: str = "active", limit: int = 50) -> str:
    """List skill goals, optionally filtered by status.

    Args:
        status: 'active' (default), 'paused', 'achieved', 'dropped', or
            '' for all. Returns by priority asc, then name.
        limit: 1-200, default 50.
    """
    if status and status not in _VALID_SKILL_STATUS:
        return f"err: status must be one of {sorted(_VALID_SKILL_STATUS)} or '' for all"
    limit = max(1, min(int(limit), 200))
    idx = get_vault_index()
    c = idx.conn
    if status:
        rows = c.execute(
            "SELECT id, name, category, target, current_level, status, "
            " priority, constraints "
            "FROM skill_goals WHERE status = ? "
            "ORDER BY priority ASC, name COLLATE NOCASE LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT id, name, category, target, current_level, status, "
            " priority, constraints "
            "FROM skill_goals "
            "ORDER BY status, priority ASC, name COLLATE NOCASE LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        return f"none — no skill goals with status='{status or 'any'}'"
    parts = [f"{len(rows)} skill goal(s):"]
    for r in rows:
        marker = {1: "🔥", 2: "·", 3: "💭"}.get(r["priority"], "·")
        cat = f" [{r['category']}]" if r["category"] else ""
        cur = f" — current: {r['current_level']}" if r["current_level"] else ""
        constraint_tag = " ⚠️ constrained" if r["constraints"] else ""
        parts.append(
            f"  {marker} id:{r['id']} {r['name']}{cat} → {r['target']}{cur}{constraint_tag}"
        )
    return "\n".join(parts)


# =============================================================================
# Injuries
# =============================================================================


@mcp.tool()
def injury_upsert(
    body_part: str,
    side: str = "",
    status: str = "",
    description: str = "",
    severity: str = "",
    started_at: str = "",
    healed_at: str = "",
    physio_started_at: str = "",
    therapist: str = "",
    restrictions: str = "",
    note_path: str = "",
    notes: str = "",
    reload_db: bool = True,
) -> str:
    """Insert or update an injury record.

    Uniqueness is (body_part, side) — calling with the same pair updates
    the existing row rather than creating a duplicate. To track recurring
    issues separately, use a different `description` and pass `side=''`
    or `body_part='left shoulder rotator'` etc.

    Args:
        body_part: Required, e.g. 'shoulder', 'knee', 'wrist'.
        side: 'left' / 'right' / 'both' / ''.
        status: 'active' / 'managing' / 'healed'.
        severity: '' / 'mild' / 'moderate' / 'severe'.
        started_at: ISO date when the injury started, or 'now'.
        healed_at: ISO date when fully resolved (set when flipping to
            status='healed').
        physio_started_at: ISO date physio began, if applicable.
        therapist: Free-text therapist or clinic name.
        restrictions: Free-text comma-separated list of movements to
            avoid, e.g. "no overhead pressing, no full-load bench, no
            handstands". Iris reads this BEFORE recommending any session
            or progression that touches the affected joint.
        note_path: Vault path to a longer-form note about this injury.

    Update semantics match `skill_upsert`: empty strings keep existing
    values.
    """
    if not body_part.strip():
        return "err: body_part required"
    if side and side not in _VALID_SIDE:
        return f"err: side must be one of {sorted(_VALID_SIDE - {''})} or '' (got '{side}')"
    if status and status not in _VALID_INJURY_STATUS:
        return f"err: status must be one of {sorted(_VALID_INJURY_STATUS)}"
    if severity and severity not in _VALID_SEVERITY:
        return f"err: severity must be one of {sorted(_VALID_SEVERITY - {''})} or '' (got '{severity}')"

    idx = get_vault_index()
    c = idx.conn
    now = _now_iso()
    existing = c.execute(
        "SELECT * FROM injuries WHERE body_part = ? COLLATE NOCASE AND side = ?",
        (body_part.strip(), side.strip()),
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

    started_iso = _parse_when(started_at) if started_at.strip() else (
        existing["started_at"] if existing else now[:10]
    )
    healed_iso = _parse_when(healed_at) if healed_at.strip() else (
        existing["healed_at"] if existing else ""
    )
    if status == "healed" and not healed_iso:
        healed_iso = now[:10]
    physio_iso = _parse_when(physio_started_at) if physio_started_at.strip() else (
        existing["physio_started_at"] if existing else ""
    )

    if existing:
        c.execute(
            "UPDATE injuries SET "
            "  status = ?, description = ?, severity = ?, "
            "  started_at = ?, healed_at = ?, physio_started_at = ?, "
            "  therapist = ?, restrictions = ?, note_path = ?, "
            "  notes = ?, updated_at = ? "
            "WHERE id = ?",
            (
                status or existing["status"],
                merge(description, "description"),
                severity if severity else existing["severity"],
                started_iso, healed_iso, physio_iso,
                merge(therapist, "therapist"),
                merge(restrictions, "restrictions"),
                merge(note_path, "note_path"),
                merge(notes, "notes"),
                now,
                existing["id"],
            ),
        )
        c.commit()
        if reload_db:
            maybe_reload_db_plugin()
        return f"ok updated id:{existing['id']}|{body_part}/{side or '-'}"

    cur = c.execute(
        "INSERT INTO injuries "
        "(body_part, side, status, description, severity, "
        " started_at, healed_at, physio_started_at, therapist, "
        " restrictions, note_path, notes, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            body_part.strip(), side.strip(), status or "active",
            description.strip(), severity, started_iso, healed_iso,
            physio_iso, therapist.strip(), restrictions.strip(),
            note_path.strip(), notes.strip(), now,
        ),
    )
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok inserted id:{cur.lastrowid}|{body_part}/{side or '-'}"


@mcp.tool()
def injury_remove(injury_id: int, reload_db: bool = True) -> str:
    """Delete an injury record by id. Prefer setting status='healed' via
    injury_upsert for normal recovery flow."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM injuries WHERE id = ?", (injury_id,))
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} id:{injury_id}"


@mcp.tool()
def injury_list(status: str = "active") -> str:
    """List injuries by status. 'active' (default) shows current concerns
    only; '' shows all including healed. Iris should ALWAYS call this with
    status='active' before recommending a training session or progression."""
    if status and status not in (_VALID_INJURY_STATUS | {"active_managing"}):
        return f"err: status must be one of {sorted(_VALID_INJURY_STATUS)} or 'active_managing' or '' for all"
    idx = get_vault_index()
    c = idx.conn
    if status == "active_managing":
        rows = c.execute(
            "SELECT * FROM injuries WHERE status IN ('active', 'managing') "
            "ORDER BY started_at DESC"
        ).fetchall()
    elif status:
        rows = c.execute(
            "SELECT * FROM injuries WHERE status = ? ORDER BY started_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM injuries ORDER BY status, started_at DESC"
        ).fetchall()
    if not rows:
        return f"none — no injuries with status='{status or 'any'}'"
    parts = [f"{len(rows)} injur(y/ies):"]
    for r in rows:
        marker = {"active": "🔴", "managing": "🟡", "healed": "✅"}.get(r["status"], "·")
        sev = f" {r['severity']}" if r["severity"] else ""
        side_label = f"{r['side']} " if r["side"] else ""
        restr = f" · avoid: {r['restrictions']}" if r["restrictions"] else ""
        parts.append(
            f"  {marker} id:{r['id']} {side_label}{r['body_part']}{sev} "
            f"({r['status']}, since {r['started_at']}){restr}"
        )
    return "\n".join(parts)


# =============================================================================
# Training sessions
# =============================================================================


@mcp.tool()
def log_training(
    summary: str,
    kind: str = "gym",
    session_at: str = "now",
    duration_min: Optional[int] = None,
    rpe: Optional[int] = None,
    skill_ids: str = "",
    note_path: str = "",
    notes: str = "",
    distance_km: Optional[float] = None,
    kcal_burned: Optional[int] = None,
    avg_hr: Optional[int] = None,
    max_hr: Optional[int] = None,
    steps: Optional[int] = None,
    elevation_gain_m: Optional[int] = None,
    avg_pace_sec_per_km: Optional[int] = None,
    data_source: str = "",
    reload_db: bool = True,
) -> str:
    """Log a training session — strength, cardio, mobility, outdoor, whatever.

    The first half (summary/kind/duration/rpe/skill_ids/notes) covers any
    training kind. The second half (distance_km, kcal_burned, avg_hr,
    max_hr, steps, elevation_gain_m, avg_pace_sec_per_km, data_source) is
    optional cardio/outdoor metric data — leave NULL for strength sessions.

    Raw set/rep detail for strength work still goes in
    `30_Episodic/Personal/Gym.md` (point `note_path` at it). For
    cardio/outdoor sessions the structured columns ARE the record — no
    need for a separate markdown note unless there's narrative to add.

    Args:
        summary: Required short description. Ex: "Morning walk along the
            Limmat" / "Pull workout — lats + active hang".
        kind: 'gym' / 'calisthenics' / 'mobility' / 'physio' / 'cardio' /
            'outdoor' / 'team_sport' / 'other'. Use 'outdoor' for walks,
            hikes, runs outside; 'cardio' for indoor cycling, treadmill,
            rower; 'gym' for free weights + machines.
        session_at: ISO datetime, "now" (default), or just date.
        duration_min: Total minutes (warmup + work + cooldown).
        rpe: 1-10 effort scale ("how hard, 10 = max").
        skill_ids: Comma-separated `skill_goals.id` values worked this
            session.
        note_path: Vault path to a dedicated note for narrative detail.

        Cardio / outdoor metrics (all optional):
        distance_km: Total distance in kilometres (e.g. 5.2 for a 5.2 km walk).
        kcal_burned: Calories burned per the source app's estimate.
        avg_hr / max_hr: Heart rate in BPM. Pulled from Apple Health,
            Strava, Garmin, Fitbit, etc.
        steps: Step count for walks/hikes/runs.
        elevation_gain_m: Total ascent in metres.
        avg_pace_sec_per_km: Average pace in seconds per km (more granular
            than min/km — e.g. 7:30 min/km = 450 sec/km).
        data_source: Where the numbers came from ("Apple Health",
            "Strava", "Garmin", "manual", screenshot OCR, etc.).
    """
    if not summary.strip():
        return "err: summary required"
    if kind and kind not in _VALID_SESSION_KIND:
        return f"err: kind must be one of {sorted(_VALID_SESSION_KIND - {''})} (got '{kind}')"
    if rpe is not None and not 1 <= rpe <= 10:
        return f"err: rpe must be 1-10 (got {rpe})"
    if duration_min is not None and duration_min < 0:
        return f"err: duration_min must be non-negative (got {duration_min})"
    # Light sanity checks on cardio columns — catch obvious typos (1000 BPM HR, etc.)
    if avg_hr is not None and not 30 <= avg_hr <= 250:
        return f"err: avg_hr {avg_hr} outside plausible range (30-250 bpm)"
    if max_hr is not None and not 30 <= max_hr <= 250:
        return f"err: max_hr {max_hr} outside plausible range (30-250 bpm)"
    if distance_km is not None and distance_km < 0:
        return f"err: distance_km must be non-negative (got {distance_km})"
    if kcal_burned is not None and kcal_burned < 0:
        return f"err: kcal_burned must be non-negative (got {kcal_burned})"
    if steps is not None and steps < 0:
        return f"err: steps must be non-negative (got {steps})"
    when = _parse_when(session_at)
    now = _now_iso()
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute(
        "INSERT INTO training_sessions "
        "(session_at, kind, duration_min, rpe, summary, skill_ids, "
        " note_path, notes, distance_km, kcal_burned, avg_hr, max_hr, "
        " steps, elevation_gain_m, avg_pace_sec_per_km, data_source, "
        " created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            when, kind, duration_min, rpe, summary.strip(),
            skill_ids.strip(), note_path.strip(), notes.strip(),
            distance_km, kcal_burned, avg_hr, max_hr,
            steps, elevation_gain_m, avg_pace_sec_per_km,
            data_source.strip(), now,
        ),
    )
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    # Build a compact return line — include metrics when present
    extras: list[str] = []
    if distance_km is not None:
        extras.append(f"dist:{distance_km}km")
    if duration_min is not None:
        extras.append(f"dur:{duration_min}min")
    if kcal_burned is not None:
        extras.append(f"kcal:{kcal_burned}")
    if avg_hr is not None:
        extras.append(f"avgHR:{avg_hr}")
    if steps is not None:
        extras.append(f"steps:{steps}")
    extras_str = (" · " + " ".join(extras)) if extras else ""
    return f"ok inserted id:{cur.lastrowid} kind:{kind} at:{when}{extras_str}"


@mcp.tool()
def remove_training(session_id: int, reload_db: bool = True) -> str:
    """Delete a training session row by id."""
    idx = get_vault_index()
    c = idx.conn
    cur = c.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    return f"ok removed:{cur.rowcount} id:{session_id}"


@mcp.tool()
def recent_training(days: int = 14, limit: int = 30) -> str:
    """Show recent training sessions with skill-goal cross-references.

    Useful for "have I trained handstands recently?" / weekly adherence
    sanity-checks.
    """
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 200))
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    idx = get_vault_index()
    c = idx.conn
    rows = c.execute(
        "SELECT id, session_at, kind, duration_min, rpe, summary, skill_ids, "
        " distance_km, kcal_burned, avg_hr, steps, data_source "
        "FROM training_sessions WHERE session_at >= ? "
        "ORDER BY session_at DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    if not rows:
        return f"none — no training sessions in the last {days} day(s)"
    parts = [f"{len(rows)} session(s) in the last {days} day(s):"]
    for r in rows:
        dur = f" · {r['duration_min']}min" if r["duration_min"] else ""
        eff = f" · RPE {r['rpe']}" if r["rpe"] else ""
        skills_tag = f" · skills:{r['skill_ids']}" if r["skill_ids"] else ""
        # Inline cardio metrics when present so cardio sessions actually
        # show their interesting data (distance / HR / kcal / steps).
        cardio_bits: list[str] = []
        if r["distance_km"] is not None:
            cardio_bits.append(f"{r['distance_km']:.2f}km")
        if r["kcal_burned"] is not None:
            cardio_bits.append(f"{r['kcal_burned']}kcal")
        if r["avg_hr"] is not None:
            cardio_bits.append(f"avgHR {r['avg_hr']}")
        if r["steps"] is not None:
            cardio_bits.append(f"{r['steps']} steps")
        cardio_tag = f" · {' · '.join(cardio_bits)}" if cardio_bits else ""
        src_tag = f" [src:{r['data_source']}]" if r["data_source"] else ""
        parts.append(
            f"  id:{r['id']} {r['session_at']} [{r['kind']}]{dur}{eff}{cardio_tag}{skills_tag}{src_tag}\n"
            f"     {r['summary']}"
        )
    return "\n".join(parts)
