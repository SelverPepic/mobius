"""Zone-aware cron schedules — durable IANA identity, truthful wall clocks.

Cron itself evaluates wall-clock time in the server's local timezone, which
is not a durable way to express "5:00 AM in Europe/Belgrade": the server's
offset to that zone changes at daylight-saving transitions, in either zone.

The durable schedule identity is therefore an IANA timezone name plus a
zone-local wall-time cron expression, declared in the app's ``init-cron.sh``
(``SCHEDULE_TZ`` / ``SCHEDULE_SOURCE``). Its live crontab materialization runs
the supervised job gate every minute; the gate compares real instants with the
declared zone clock and claims at most one run per local date.

Only fixed wall times may carry a timezone: numeric minute and hour, ``*``
day-of-month and month, and every weekday or a weekday list (``0 9 * * 1-5``).
DST edge behavior is explicit:

* an ambiguous wall time runs once, at its first occurrence (``fold=0``);
* a nonexistent wall time runs at the first valid minute after the gap;
* a civil date with no valid minute at or after the requested time is skipped.

That policy gives each scheduled day one deterministic launch on ordinary and
DST-transition dates without pretending a static server-local cron expression
can preserve an IANA wall clock.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_WALL_CLOCK_CRON_RE = re.compile(
  r"[ \t]*([0-9]{1,2})[ \t]+([0-9]{1,2})[ \t]+\*[ \t]+\*"
  r"[ \t]+(\*|[0-7](?:-[0-7])?(?:,[0-7](?:-[0-7])?)*)[ \t]*",
  re.ASCII,
)
# Written by init-cron-scaffold.sh; parsed (never executed) from init-cron.sh.
_DECL_TZ_RE = re.compile(r'^SCHEDULE_TZ="([A-Za-z0-9_+/-]+)"\s*$', re.M)
_DECL_SOURCE_RE = re.compile(r'^SCHEDULE_SOURCE="([^"\n]+)"\s*$', re.M)
WALL_CLOCK_CRON = "* * * * *"


def valid_timezone(name: str) -> bool:
  """True when ``name`` is a resolvable IANA timezone identifier."""
  if not isinstance(name, str) or not name or len(name) > 64:
    return False
  if not re.fullmatch(r"[A-Za-z0-9_+/-]+", name):
    return False
  try:
    ZoneInfo(name)
  except Exception:
    return False
  return True


def parse_wall_clock_cron(
  expr: str,
) -> tuple[int, int, frozenset[int]] | None:
  """(minute, hour, weekdays) for a fixed wall time, else None.

  ``weekdays`` uses cron numbering with Sunday as 0; ``*`` is all seven.
  """
  m = _WALL_CLOCK_CRON_RE.fullmatch(expr or "")
  if not m:
    return None
  minute, hour = int(m.group(1)), int(m.group(2))
  if minute > 59 or hour > 23:
    return None
  days: set[int] = set()
  for part in m.group(3).replace("*", "0-6").split(","):
    low, _, high = part.partition("-")
    if int(high or low) < int(low):
      return None
    days.update(day % 7 for day in range(int(low), int(high or low) + 1))
  return minute, hour, frozenset(days)


def materialize_zone_cron(
  zone_cron: str,
  tz_name: str,
) -> str:
  """Return the honest live cadence for a zone-local wall-time schedule.

  The actual wall-clock decision belongs to the supervised job gate. A static
  offset expression is intentionally never returned: it would drift or misfire
  at the next target-zone or server-zone transition.
  """
  if parse_wall_clock_cron(zone_cron) is None:
    raise ValueError(
      "A timezone-owned schedule must be a fixed wall time "
      f"('m h * * *' or 'm h * * 1-5'), got: {zone_cron!r}"
    )
  if not valid_timezone(tz_name):
    raise ValueError(f"Unknown IANA timezone: {tz_name!r}")
  return WALL_CLOCK_CRON


def _valid_wall_instants(local: datetime, tz: ZoneInfo) -> list[datetime]:
  """UTC instants that round-trip to ``local``, ordered earliest first."""
  instants: set[datetime] = set()
  for fold in (0, 1):
    candidate = local.replace(tzinfo=tz, fold=fold)
    instant = candidate.astimezone(timezone.utc)
    round_trip = instant.astimezone(tz)
    if (
      round_trip.replace(tzinfo=None) == local
      and round_trip.fold == fold
    ):
      instants.add(instant)
  return sorted(instants)


def wall_clock_occurrence(
  local_date: date,
  zone_cron: str,
  tz_name: str,
) -> datetime | None:
  """The UTC instant selected for one local civil date.

  Ambiguous times choose the first occurrence. Nonexistent times advance to
  the first valid minute on the same civil date. ``None`` means the schedule
  skips that weekday, or the remainder of that civil date does not exist in
  the zone.
  """
  parsed = parse_wall_clock_cron(zone_cron)
  if parsed is None:
    raise ValueError(
      "A timezone-owned schedule must be a fixed wall time "
      f"('m h * * *' or 'm h * * 1-5'), got: {zone_cron!r}"
    )
  if not valid_timezone(tz_name):
    raise ValueError(f"Unknown IANA timezone: {tz_name!r}")
  minute, hour, weekdays = parsed
  if local_date.isoweekday() % 7 not in weekdays:
    return None
  tz = ZoneInfo(tz_name)
  requested = datetime(
    local_date.year, local_date.month, local_date.day, hour, minute,
  )
  candidate = requested
  while candidate.date() == local_date:
    instants = _valid_wall_instants(candidate, tz)
    if instants:
      return instants[0]
    candidate += timedelta(minutes=1)
  return None


def due_wall_clock_date(
  zone_cron: str,
  tz_name: str,
  *,
  now: datetime | None = None,
) -> date | None:
  """The local date due at ``now``'s UTC minute, otherwise ``None``."""
  moment = now or datetime.now(timezone.utc)
  if moment.tzinfo is None:
    raise ValueError("now must be timezone-aware")
  moment = moment.astimezone(timezone.utc).replace(second=0, microsecond=0)
  tz = ZoneInfo(tz_name) if valid_timezone(tz_name) else None
  if tz is None:
    raise ValueError(f"Unknown IANA timezone: {tz_name!r}")
  local_date = moment.astimezone(tz).date()
  occurrence = wall_clock_occurrence(local_date, zone_cron, tz_name)
  return local_date if occurrence == moment else None


def parse_zone_declaration(init_cron_text: str) -> tuple[str, str] | None:
  """Extracts (timezone, zone_cron) from init-cron.sh text, if declared.

  Returns ``None`` only when both variables are absent. A partial or invalid
  platform-managed declaration raises ``ValueError`` so reconciliation fails
  closed instead of turning its every-minute gate into an every-minute job.
  """
  tz_match = _DECL_TZ_RE.search(init_cron_text or "")
  source_match = _DECL_SOURCE_RE.search(init_cron_text or "")
  if not tz_match and not source_match:
    return None
  if not tz_match or not source_match:
    raise ValueError("Incomplete IANA wall-clock schedule declaration")
  tz_name = tz_match.group(1)
  zone_cron = source_match.group(1).strip()
  if not valid_timezone(tz_name):
    raise ValueError(f"Unknown IANA timezone: {tz_name!r}")
  if parse_wall_clock_cron(zone_cron) is None:
    raise ValueError(f"Invalid zone-local wall-time cron: {zone_cron!r}")
  return tz_name, zone_cron


def server_timezone_name() -> str:
  """The server clock's IANA identity: TZ env, /etc/localtime, else UTC.

  Containers conventionally run UTC with neither signal present; "UTC" is a
  valid IANA identifier, so the fallback stays truthful for them.
  """
  tz_env = os.environ.get("TZ", "").strip()
  if tz_env and valid_timezone(tz_env):
    return tz_env
  try:
    target = Path("/etc/localtime").resolve()
    parts = target.parts
    if "zoneinfo" in parts:
      name = "/".join(parts[parts.index("zoneinfo") + 1:])
      if valid_timezone(name):
        return name
  except OSError:
    pass
  return "UTC"
