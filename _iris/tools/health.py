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

import re
import shutil
from datetime import datetime, timedelta
from typing import Optional

from .. import mcp
from ..core import get_vault_index, get_vault_root, maybe_reload_db_plugin


# =============================================================================
# Helpers
# =============================================================================

_VALID_SOURCES = {"manual", "photo", "label", "barcode", "restaurant"}
_VALID_CONFIDENCE = {"high", "medium", "low"}

# Photos that come in via Discord land at `90_Inbox/inbox/<timestamp>_<name>`
# (see _save_attachments_to_inbox in bot.py). When we log a meal with one of
# those as its photo_path, we route it to a permanent food-log location:
#   40_Attachments/Food Log/YYYY-MM/YYYY-MM-DD_HHMM_<slug>.<ext>
# The slug is derived from the meal description so the on-disk filename is
# self-describing instead of a timestamp+hash blob.
_INBOX_PREFIX = "90_Inbox/inbox/"
_FOOD_LOG_DIR = "40_Attachments/Food Log"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn a meal description into a filename-safe slug.

    Examples:
      "braised pork + brown rice + salad"  → "braised-pork-brown-rice-salad"
      "Coca-Cola Zero (330 ml)"            → "coca-cola-zero-330-ml"
      "🍣 Salmon nigiri × 6"               → "salmon-nigiri-6"

    Strips emoji + punctuation, lowercases, collapses whitespace, truncates
    on a word boundary, returns "meal" as a fallback when the result would
    be empty.
    """
    if not text:
        return "meal"
    # Normalise to ASCII-ish: lowercase, replace non-alphanum runs with '-'
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    if not s:
        return "meal"
    if len(s) > max_len:
        # Truncate, then back off to the last hyphen so we don't break mid-word
        cut = s[:max_len]
        last_dash = cut.rfind("-")
        if last_dash > max_len * 0.6:  # only back off if it doesn't strand us at the start
            cut = cut[:last_dash]
        s = cut.strip("-")
    return s or "meal"


def _route_food_photo(
    inbox_rel: str,
    description: str,
    eaten_at_iso: str,
) -> tuple[str, str]:
    """Move a photo from the inbox to the food-log archive with a
    descriptive name. Returns (new_vault_rel_path, status_message).

    Skips the move (and returns the original path) when:
      - The source isn't actually in the inbox (already routed, or some
        other vault path Iris picked up by hand).
      - The source file doesn't exist on disk (shouldn't normally happen,
        but be defensive — log + leave the DB row pointing at the original).

    Always preserves the original file extension. Collisions are resolved
    with a numeric suffix (rare, but possible if two meals get logged with
    the same description within the same minute).
    """
    if not inbox_rel or not inbox_rel.startswith(_INBOX_PREFIX):
        return inbox_rel, "kept original path (not in inbox)"
    root = get_vault_root()
    src = root / inbox_rel
    if not src.exists():
        return inbox_rel, f"source file not found at {src}, kept original path"

    # Parse the eaten_at timestamp so the new filename mirrors WHEN the meal
    # was eaten (not when the photo was uploaded — these can differ when the
    # user back-logs a meal from earlier in the day).
    try:
        dt = datetime.fromisoformat(eaten_at_iso)
    except ValueError:
        dt = datetime.now()
    yyyymm = dt.strftime("%Y-%m")
    slug = _slugify(description)
    ext = src.suffix.lower() or ".jpg"
    base_name = f"{dt.strftime('%Y-%m-%d_%H%M')}_{slug}{ext}"

    dest_dir = root / _FOOD_LOG_DIR / yyyymm
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / base_name
    # Collision: append numeric suffix until free.
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{dt.strftime('%Y-%m-%d_%H%M')}_{slug}_{counter}{ext}"
        counter += 1

    try:
        shutil.move(str(src), str(dest))
    except OSError as e:
        return inbox_rel, f"move failed ({e}), kept original path"

    new_rel = str(dest.relative_to(root)).replace("\\", "/")
    return new_rel, f"routed {inbox_rel} → {new_rel}"


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
        photo_path: Vault-relative path to the source photo, if any. When
            this points into `90_Inbox/inbox/...` (typical for a fresh
            Discord upload), the file is automatically routed: renamed to
            `YYYY-MM-DD_HHMM_<meal-slug>.<ext>` and moved into
            `40_Attachments/Food Log/YYYY-MM/`. The stored path reflects
            the new location, so the meal row + file stay in sync and
            you don't end up with stale `inbox/<timestamp>_image.png`
            references that get cleaned up later.
        notes: Free-text context — useful for "after gym", "Bu's cooking",
            "shared plate, ~70 % of this".

    Returns:
        Status string with the new row id, e.g. "ok inserted id:42 kcal:670".
        Includes a "photo: routed → ..." breadcrumb when a photo got moved.
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

    # Route an inbox photo to the permanent food-log archive (no-op for paths
    # that are already routed or external). Done BEFORE the DB insert so the
    # stored photo_path is always the final on-disk location.
    photo_clean = (photo_path or "").strip()
    routing_msg = ""
    if photo_clean:
        new_path, routing_msg = _route_food_photo(photo_clean, desc, when)
        photo_clean = new_path

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
         photo_clean or None,
         (notes or "").strip() or None,
         now),
    )
    c.commit()
    if reload_db:
        maybe_reload_db_plugin()
    base = (
        f"ok inserted id:{cur.lastrowid} kcal:{int(kcal)} "
        f"at:{when} source:{source}"
    )
    if photo_clean and routing_msg and "kept" not in routing_msg:
        base += f" · photo: {routing_msg}"
    return base


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


# =============================================================================
# Health-expert: BMR / TDEE / target intake / activity profile
# =============================================================================

# Mifflin-St Jeor activity multipliers — standard textbook values used by
# basically every fitness calculator. Keys are the canonical strings the
# rest of Iris uses; aliases get normalised in _resolve_activity().
_ACTIVITY_MULTIPLIERS: dict[str, float] = {
    "sedentary":   1.20,   # desk job, little to no exercise
    "light":       1.375,  # light exercise 1-3 days/week
    "moderate":    1.55,   # moderate exercise 3-5 days/week
    "active":      1.725,  # hard exercise 6-7 days/week
    "very_active": 1.90,   # athlete-level / physical job 2x training/day
}
_ACTIVITY_ALIASES: dict[str, str] = {
    "sedentary": "sedentary", "none": "sedentary", "desk": "sedentary",
    "light": "light", "lightly_active": "light", "1-3": "light",
    "moderate": "moderate", "moderately_active": "moderate", "3-5": "moderate",
    "active": "active", "very": "active", "hard": "active", "6-7": "active",
    "very_active": "very_active", "athlete": "very_active",
    "extra_active": "very_active",
}
_VALID_SEX = {"male", "female", "other"}


def _resolve_activity(level: str) -> Optional[str]:
    key = (level or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _ACTIVITY_ALIASES.get(key)


def _age_years(dob_iso: str, on_date: Optional[datetime] = None) -> Optional[int]:
    """Compute age in whole years from an ISO date string. Returns None when
    the DoB is missing or unparseable.
    """
    if not dob_iso:
        return None
    try:
        dob = datetime.strptime(dob_iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    today = (on_date or datetime.now()).date()
    years = today.year - dob.year
    # Subtract a year if birthday hasn't happened yet this year.
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return max(0, years)


def _latest_weight_kg() -> Optional[float]:
    idx = get_vault_index()
    c = idx.conn
    row = c.execute(
        "SELECT kg FROM weights ORDER BY measured_at DESC LIMIT 1"
    ).fetchone()
    return float(row["kg"]) if row else None


def _load_profile() -> Optional[dict]:
    idx = get_vault_index()
    c = idx.conn
    row = c.execute("SELECT * FROM health_profile WHERE id = 1").fetchone()
    return dict(row) if row else None


def _mifflin_st_jeor(weight_kg: float, height_cm: float, age: int, sex: str) -> float:
    """Return BMR in kcal/day using the Mifflin-St Jeor equation.

    For ``sex='other'`` (or anything unrecognised), we return the midpoint of
    the male and female formulas — gives a reasonable estimate without
    forcing the user to misrepresent themselves to use the tool. Caller can
    override with sex='male' or 'female' for accuracy.
    """
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    if sex == "male":
        return base - 5.0
    if sex == "female":
        return base - 161.0
    # 'other' or unknown — midpoint of the two (difference is 156 kcal → -83).
    return base - 83.0


@mcp.tool()
def health_profile_set(
    height_cm: Optional[float] = None,
    date_of_birth: str = "",
    sex: str = "",
    activity_level: str = "",
    target_kg: Optional[float] = None,
    target_weekly_loss_kg: Optional[float] = None,
    notes: str = "",
) -> str:
    """Create or update Hyun-Min's health profile (singleton row).

    Only sets fields that are explicitly passed — pass an empty string or
    None for fields you want to leave unchanged. Call this when Hyun-Min
    shares any of: height ("I'm 178cm"), age/DoB, sex/biological sex (for
    BMR formula accuracy), activity level, target weight, or weight-loss
    pace.

    Args:
        height_cm: Height in centimetres (e.g. 178).
        date_of_birth: ISO date "YYYY-MM-DD". Age is derived from this so
            it never goes stale.
        sex: 'male' / 'female' / 'other'. Affects the BMR constant — the
            gap between male and female formulas is ~156 kcal/day, which
            is meaningful for cutting math. 'other' uses the midpoint.
        activity_level: 'sedentary' / 'light' / 'moderate' / 'active' /
            'very_active'. Aliases like '1-3' for 'light' are accepted.
        target_kg: Goal weight in kg.
        target_weekly_loss_kg: Pace, kg/week. Safe range 0.25–1.0; 0.5
            (~550 kcal/day deficit) is the textbook recommendation.
        notes: Free-text context.
    """
    updates: dict[str, object] = {}
    if height_cm is not None and height_cm > 0:
        if not 50 <= height_cm <= 250:
            return f"err: height_cm {height_cm} outside plausible range (50–250)"
        updates["height_cm"] = float(height_cm)
    if date_of_birth.strip():
        if _age_years(date_of_birth) is None:
            return f"err: date_of_birth must be YYYY-MM-DD (got '{date_of_birth}')"
        updates["date_of_birth"] = date_of_birth.strip()[:10]
    if sex.strip():
        sex_l = sex.strip().lower()
        if sex_l not in _VALID_SEX:
            return f"err: sex must be one of {sorted(_VALID_SEX)} (got '{sex}')"
        updates["sex"] = sex_l
    if activity_level.strip():
        resolved = _resolve_activity(activity_level)
        if resolved is None:
            return (
                f"err: activity_level must be one of "
                f"{sorted(_ACTIVITY_MULTIPLIERS)} (got '{activity_level}')"
            )
        updates["activity_level"] = resolved
    if target_kg is not None and target_kg > 0:
        if not 20 <= target_kg <= 400:
            return f"err: target_kg {target_kg} outside plausible range (20–400)"
        updates["target_kg"] = float(target_kg)
    if target_weekly_loss_kg is not None:
        if not -2.0 <= target_weekly_loss_kg <= 2.0:
            return (
                f"err: target_weekly_loss_kg {target_weekly_loss_kg} outside "
                f"safe range (-2.0 to 2.0 — anything beyond 1 kg/week is "
                f"considered aggressive)"
            )
        updates["target_weekly_loss_kg"] = float(target_weekly_loss_kg)
    if notes.strip():
        updates["notes"] = notes.strip()

    if not updates:
        return "ok no-op — no fields provided"

    idx = get_vault_index()
    c = idx.conn
    now = datetime.now().isoformat(timespec="seconds")
    updates["updated_at"] = now
    existing = c.execute("SELECT 1 FROM health_profile WHERE id = 1").fetchone()
    if existing:
        cols = ", ".join(f"{k} = ?" for k in updates)
        c.execute(f"UPDATE health_profile SET {cols} WHERE id = 1", list(updates.values()))
    else:
        # Fresh insert — fill non-updated columns with NULL.
        all_cols = [
            "height_cm", "date_of_birth", "sex", "activity_level",
            "target_kg", "target_weekly_loss_kg", "notes", "updated_at",
        ]
        values = [updates.get(k) for k in all_cols]
        placeholders = ", ".join(["?"] * (len(all_cols) + 1))
        c.execute(
            f"INSERT INTO health_profile (id, {', '.join(all_cols)}) "
            f"VALUES ({placeholders})",
            [1] + values,
        )
    c.commit()
    fields_set = ", ".join(
        f"{k}={v}" for k, v in updates.items() if k != "updated_at"
    )
    return f"ok updated · {fields_set}"


@mcp.tool()
def health_profile_get() -> str:
    """Show the current health profile + derived values (age from DoB,
    latest weight from `weights`).
    """
    profile = _load_profile()
    if not profile:
        return (
            "no profile set yet — ask Hyun-Min for: height (cm), date of "
            "birth (YYYY-MM-DD), sex (for BMR formula), activity level, "
            "and optionally target weight + weekly loss pace. Then call "
            "`health_profile_set(...)` to seed it."
        )
    lines = ["Health profile:"]
    if profile.get("height_cm"):
        lines.append(f"  height: {profile['height_cm']:.0f} cm")
    age = _age_years(profile.get("date_of_birth") or "")
    if age is not None:
        lines.append(f"  age: {age} years (DoB {profile['date_of_birth']})")
    if profile.get("sex"):
        lines.append(f"  sex: {profile['sex']}")
    if profile.get("activity_level"):
        mult = _ACTIVITY_MULTIPLIERS.get(profile["activity_level"], 0)
        lines.append(
            f"  activity: {profile['activity_level']} (×{mult:.3f} BMR)"
        )
    if profile.get("target_kg"):
        lines.append(f"  target: {profile['target_kg']:.1f} kg")
    if profile.get("target_weekly_loss_kg"):
        lines.append(
            f"  pace: {profile['target_weekly_loss_kg']:+.2f} kg/week"
        )
    latest = _latest_weight_kg()
    if latest is not None:
        lines.append(f"  latest weight: {latest:.1f} kg")
    if profile.get("notes"):
        lines.append(f"  notes: {profile['notes']}")
    return "\n".join(lines)


@mcp.tool()
def tdee_estimate(weight_kg: Optional[float] = None) -> str:
    """Estimate Total Daily Energy Expenditure (TDEE) = BMR × activity.

    Uses the Mifflin-St Jeor formula for BMR. By default pulls weight from
    the latest `weights` row; pass ``weight_kg`` to override. Requires the
    health profile to have height, date of birth, sex, and activity_level
    set — returns a friendly "missing X" message otherwise.
    """
    profile = _load_profile()
    if not profile:
        return "err: no health profile set — call `health_profile_set(...)` first"
    missing: list[str] = [
        f for f in ("height_cm", "date_of_birth", "sex", "activity_level")
        if not profile.get(f)
    ]
    if missing:
        return (
            f"err: profile missing {', '.join(missing)} — "
            f"call `health_profile_set(...)` with the missing fields"
        )
    w = weight_kg if weight_kg is not None else _latest_weight_kg()
    if w is None:
        return "err: no weight on file — log one with `log_weight(<kg>)` first"
    age = _age_years(profile["date_of_birth"])
    if age is None:
        return f"err: could not parse date_of_birth '{profile['date_of_birth']}'"
    bmr = _mifflin_st_jeor(w, profile["height_cm"], age, profile["sex"])
    mult = _ACTIVITY_MULTIPLIERS[profile["activity_level"]]
    tdee = bmr * mult
    return (
        f"BMR: {bmr:.0f} kcal/day  (Mifflin-St Jeor @ {w:.1f} kg, "
        f"{profile['height_cm']:.0f} cm, {age} y, {profile['sex']})\n"
        f"TDEE: {tdee:.0f} kcal/day  (× {mult:.3f} for {profile['activity_level']})\n"
        f"→ call `target_intake()` for a deficit-adjusted intake target."
    )


@mcp.tool()
def target_intake(
    weekly_loss_kg: Optional[float] = None,
    weight_kg: Optional[float] = None,
) -> str:
    """Compute recommended daily calorie intake for a weight-loss pace,
    using TDEE − deficit math.

    Rule of thumb: 1 kg of body fat ≈ 7700 kcal, so 0.5 kg/week needs a
    ~550 kcal/day deficit, 1 kg/week needs ~1100 kcal/day. Safety floor:
    intake can never drop below the BMR — chronic sub-BMR eating tanks
    metabolism and is contraindicated for sustained loss.

    Args:
        weekly_loss_kg: Target pace in kg/week. Defaults to the value in
            the profile (`target_weekly_loss_kg`), or 0.5 if unset.
        weight_kg: Override the weight used for TDEE. Defaults to latest.
    """
    profile = _load_profile()
    if not profile:
        return "err: no health profile set — call `health_profile_set(...)` first"
    if weekly_loss_kg is None:
        weekly_loss_kg = profile.get("target_weekly_loss_kg") or 0.5
    if not -2.0 <= weekly_loss_kg <= 2.0:
        return (
            f"err: weekly_loss_kg {weekly_loss_kg} outside safe range "
            f"(-2 to 2 kg/week)"
        )
    missing: list[str] = [
        f for f in ("height_cm", "date_of_birth", "sex", "activity_level")
        if not profile.get(f)
    ]
    if missing:
        return f"err: profile missing {', '.join(missing)}"
    w = weight_kg if weight_kg is not None else _latest_weight_kg()
    if w is None:
        return "err: no weight on file — log one with `log_weight(<kg>)` first"
    age = _age_years(profile["date_of_birth"])
    if age is None:
        return f"err: could not parse date_of_birth '{profile['date_of_birth']}'"
    bmr = _mifflin_st_jeor(w, profile["height_cm"], age, profile["sex"])
    mult = _ACTIVITY_MULTIPLIERS[profile["activity_level"]]
    tdee = bmr * mult
    # 1 kg of body fat ≈ 7700 kcal. Daily deficit = 7700 × weekly_loss / 7.
    daily_deficit = 7700.0 * weekly_loss_kg / 7.0
    raw_intake = tdee - daily_deficit
    intake = max(raw_intake, bmr)  # safety floor
    floored = raw_intake < bmr

    pace_label = (
        "gain" if weekly_loss_kg < 0 else
        "loss" if weekly_loss_kg > 0 else "maintenance"
    )
    lines = [
        f"TDEE: {tdee:.0f} kcal/day (BMR {bmr:.0f} × {mult:.3f})",
        (f"Target pace: {weekly_loss_kg:+.2f} kg/week → "
         f"{daily_deficit:+.0f} kcal/day {pace_label} deficit"),
        f"→ Recommended intake: ~{intake:.0f} kcal/day",
    ]
    if floored:
        lines.append(
            f"⚠️  raw target ({raw_intake:.0f}) was below BMR ({bmr:.0f}) — "
            f"floored at BMR. The pace {weekly_loss_kg:.2f} kg/week is too "
            f"aggressive at this weight + activity level; pick something "
            f"slower (0.5 kg/week is a safe default)."
        )
    elif weekly_loss_kg > 1.0:
        lines.append(
            f"💡 {weekly_loss_kg:.1f} kg/week is on the aggressive end. "
            f"0.5–0.75 kg/week is the sweet spot for sustained loss with "
            f"less muscle loss."
        )
    return "\n".join(lines)


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


# =============================================================================
# Scheduled summaries — markdown output formatted for the morning-brief style
# section parser in discord.py (`# Title`, then `## Section` blocks).
# =============================================================================


def _target_intake_kcal() -> Optional[int]:
    """Compute the safety-floored daily intake target from the current
    profile + latest weight. Returns None when the profile / weight is
    incomplete (e.g. before bootstrapping) so the summary can fall back
    to "target not yet configured" instead of inventing a number.
    """
    profile = _load_profile()
    if not profile:
        return None
    for f in ("height_cm", "date_of_birth", "sex", "activity_level"):
        if not profile.get(f):
            return None
    w = _latest_weight_kg()
    if w is None:
        return None
    age = _age_years(profile["date_of_birth"])
    if age is None:
        return None
    bmr = _mifflin_st_jeor(w, profile["height_cm"], age, profile["sex"])
    tdee = bmr * _ACTIVITY_MULTIPLIERS[profile["activity_level"]]
    weekly = profile.get("target_weekly_loss_kg") or 0.5
    daily_deficit = 7700.0 * weekly / 7.0
    return int(round(max(tdee - daily_deficit, bmr)))


@mcp.tool()
def health_daily_summary(date: str = "yesterday") -> str:
    """Markdown summary of one day's health data (default: yesterday).

    Designed for the scheduled health-channel fire in bot.py — produces
    a `# Title` + `## Section` structure that `_parse_md_sections` can
    split into Discord embed fields. Same output is also useful in
    chat: "show me yesterday's health" / "today's intake so far".

    Sections rendered when there's data for them:
        ⚖️ Weight, 🍽️ Intake, 🎯 Target, 📝 Notes (always-on tail).
    """
    today = datetime.now().date()
    if date.lower() == "today":
        d = today
    elif date.lower() == "yesterday":
        d = today - timedelta(days=1)
    else:
        try:
            d = datetime.strptime(date[:10], "%Y-%m-%d").date()
        except ValueError:
            return f"err: date must be 'today', 'yesterday', or YYYY-MM-DD (got '{date}')"
    day = d.isoformat()
    day_label = d.strftime("%A, %Y-%m-%d")
    idx = get_vault_index()
    c = idx.conn

    lines: list[str] = [f"# Health · {day_label}"]

    # ── Weight section: latest reading + delta vs reading on or before `day`
    latest_row = c.execute(
        "SELECT measured_at, kg FROM weights ORDER BY measured_at DESC LIMIT 1"
    ).fetchone()
    if latest_row:
        lines.append("\n## ⚖️ Weight")
        lines.append(
            f"- Latest: **{latest_row['kg']:.1f} kg** "
            f"({latest_row['measured_at'][:10]})"
        )
        # 7-day delta if we have a prior reading
        prior = c.execute(
            "SELECT kg FROM weights "
            "WHERE measured_at < ? AND measured_at >= ? "
            "ORDER BY measured_at ASC LIMIT 1",
            (latest_row["measured_at"],
             (datetime.fromisoformat(latest_row["measured_at"])
              - timedelta(days=8)).isoformat(timespec="seconds")),
        ).fetchone()
        if prior is not None:
            delta = latest_row["kg"] - prior["kg"]
            sign = "+" if delta >= 0 else ""
            lines.append(f"- 7-day delta: {sign}{delta:.1f} kg")

    # ── Intake section: meals for the day from meals_daily view
    intake = c.execute(
        "SELECT meal_count, total_kcal, total_kcal_high, "
        " total_protein_g, total_carbs_g, total_fat_g "
        "FROM meals_daily WHERE day = ?",
        (day,),
    ).fetchone()
    target = _target_intake_kcal()
    lines.append("\n## 🍽️ Intake")
    if intake and intake["meal_count"]:
        lines.append(
            f"- **{intake['meal_count']} meal(s) · {intake['total_kcal']} kcal**"
        )
        if intake["total_kcal_high"] and intake["total_kcal_high"] != intake["total_kcal"]:
            lines.append(
                f"  (uncertainty high-end: {intake['total_kcal_high']} kcal)"
            )
        macros: list[str] = []
        if intake["total_protein_g"]:
            macros.append(f"P {intake['total_protein_g']:.0f}g")
        if intake["total_carbs_g"]:
            macros.append(f"C {intake['total_carbs_g']:.0f}g")
        if intake["total_fat_g"]:
            macros.append(f"F {intake['total_fat_g']:.0f}g")
        if macros:
            lines.append(f"- Macros: {' · '.join(macros)}")
    else:
        lines.append(f"- No meals logged for {day}.")

    # ── Target section: deficit/surplus vs computed daily target
    lines.append("\n## 🎯 Target")
    if target is None:
        lines.append(
            "- Daily target not yet configured — run "
            "`health_profile_set(...)` with height / DoB / sex / "
            "activity_level so I can compute it."
        )
    else:
        consumed = intake["total_kcal"] if (intake and intake["meal_count"]) else 0
        diff = consumed - target
        if intake and intake["meal_count"]:
            if diff < 0:
                lines.append(
                    f"- Target: **{target} kcal/day**  ·  "
                    f"{abs(diff)} kcal **under** "
                    f"({consumed} / {target})"
                )
            elif diff > 0:
                lines.append(
                    f"- Target: **{target} kcal/day**  ·  "
                    f"{diff} kcal **over** "
                    f"({consumed} / {target})"
                )
            else:
                lines.append(
                    f"- Target: **{target} kcal/day**  ·  bang on "
                    f"({consumed} / {target})"
                )
        else:
            lines.append(f"- Target: **{target} kcal/day** (nothing logged yet)")

    # ── Notes section: light coaching / nudge based on the data
    nudges: list[str] = []
    # Stale weigh-in nudge
    if latest_row:
        last_dt = datetime.fromisoformat(latest_row["measured_at"])
        days_since = (datetime.now() - last_dt).days
        if days_since >= 7:
            nudges.append(
                f"⚖️ Last weigh-in was {days_since} day(s) ago — "
                f"consider stepping on the scale today."
            )
    else:
        nudges.append(
            "⚖️ No weight on file yet — log one with "
            "`log_weight(<kg>)` to start the trend."
        )
    # Missing-target nudge — only show on the daily card, not on backfills
    if target is None and d == today:
        nudges.append(
            "ℹ️  Profile isn't fully set up yet. I'll bootstrap it as "
            "you share stats (height / DoB / sex / activity)."
        )
    if nudges:
        lines.append("\n## 📝 Notes")
        for n in nudges:
            lines.append(f"- {n}")

    return "\n".join(lines)


@mcp.tool()
def health_weekly_summary(date: str = "today") -> str:
    """Markdown summary of the week ending on ``date`` (default: today,
    so a Monday-fire produces last Mon-Sun's recap).

    Sections rendered: ⚖️ Weight change, 🍽️ Intake (avg + adherence),
    🎯 vs target, 📝 Notes.
    """
    if date.lower() == "today":
        end = datetime.now().date()
    elif date.lower() == "yesterday":
        end = datetime.now().date() - timedelta(days=1)
    else:
        try:
            end = datetime.strptime(date[:10], "%Y-%m-%d").date()
        except ValueError:
            return f"err: date must be 'today', 'yesterday', or YYYY-MM-DD (got '{date}')"
    start = end - timedelta(days=6)
    week_label = f"{start.isoformat()} → {end.isoformat()}"
    idx = get_vault_index()
    c = idx.conn

    lines: list[str] = [f"# Weekly Health · {week_label}"]

    # Weight delta over the window: first and last in-range readings
    first_row = c.execute(
        "SELECT measured_at, kg FROM weights "
        "WHERE substr(measured_at, 1, 10) >= ? "
        "AND substr(measured_at, 1, 10) <= ? "
        "ORDER BY measured_at ASC LIMIT 1",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    last_row = c.execute(
        "SELECT measured_at, kg FROM weights "
        "WHERE substr(measured_at, 1, 10) >= ? "
        "AND substr(measured_at, 1, 10) <= ? "
        "ORDER BY measured_at DESC LIMIT 1",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    weight_count = c.execute(
        "SELECT COUNT(*) AS n FROM weights "
        "WHERE substr(measured_at, 1, 10) >= ? "
        "AND substr(measured_at, 1, 10) <= ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()["n"]

    if first_row and last_row and first_row["measured_at"] != last_row["measured_at"]:
        delta = last_row["kg"] - first_row["kg"]
        sign = "+" if delta >= 0 else ""
        lines.append("\n## ⚖️ Weight")
        lines.append(
            f"- **{first_row['kg']:.1f} → {last_row['kg']:.1f} kg "
            f"({sign}{delta:.1f} kg)**"
        )
        lines.append(f"- {weight_count} weigh-in(s) this week")
    elif last_row:
        lines.append("\n## ⚖️ Weight")
        lines.append(
            f"- {last_row['kg']:.1f} kg (only one reading this week — "
            f"no delta yet)"
        )
    else:
        lines.append("\n## ⚖️ Weight")
        lines.append("- No weigh-ins this week.")

    # Intake: avg daily kcal + days-with-meals adherence
    daily_rows = c.execute(
        "SELECT day, meal_count, total_kcal, total_kcal_high, "
        " total_protein_g, total_carbs_g, total_fat_g "
        "FROM meals_daily "
        "WHERE day >= ? AND day <= ? ORDER BY day",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    days_logged = len(daily_rows)
    total_kcal = sum(r["total_kcal"] for r in daily_rows)
    total_meals = sum(r["meal_count"] for r in daily_rows)
    avg_kcal = (total_kcal // days_logged) if days_logged else 0
    lines.append("\n## 🍽️ Intake")
    if daily_rows:
        lines.append(
            f"- **{total_meals} meals over {days_logged}/7 days** · "
            f"avg {avg_kcal} kcal/day"
        )
        # Protein average (only across days that have protein data)
        p_days = [r for r in daily_rows if r["total_protein_g"]]
        if p_days:
            avg_p = sum(r["total_protein_g"] for r in p_days) / len(p_days)
            lines.append(f"- Avg protein: {avg_p:.0f} g/day "
                         f"(across {len(p_days)} day(s) with macros)")
    else:
        lines.append("- No meals logged this week.")

    # Target alignment
    target = _target_intake_kcal()
    if target and daily_rows:
        diff = avg_kcal - target
        sign = "+" if diff >= 0 else ""
        # Estimated weekly weight change from observed average deficit
        weekly_change = (diff * 7) / 7700.0  # kg
        lines.append("\n## 🎯 Target")
        lines.append(
            f"- Daily target: **{target} kcal**  ·  "
            f"avg {sign}{diff} kcal/day vs target"
        )
        wc_sign = "+" if weekly_change >= 0 else ""
        lines.append(
            f"- Implied weight change this week: "
            f"~{wc_sign}{weekly_change:.2f} kg "
            f"(from intake vs TDEE alone; actual scale movement may "
            f"differ due to water + activity variance)"
        )

    # Adherence note
    nudges: list[str] = []
    if days_logged < 5:
        nudges.append(
            f"📉 Only {days_logged}/7 days had meal logs this week — "
            f"the more days you log, the better the trend math gets."
        )
    if weight_count < 2:
        nudges.append(
            f"⚖️ {weight_count} weigh-in(s) this week — even 2-3 "
            f"morning readings smooth out water-weight noise."
        )
    if nudges:
        lines.append("\n## 📝 Notes")
        for n in nudges:
            lines.append(f"- {n}")

    return "\n".join(lines)
