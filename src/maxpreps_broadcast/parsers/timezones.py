"""School timezone derivation.

MaxPreps dates are naive local time, so every contest gets the school's IANA
zone attached (``zoneinfo`` handles DST, including the November transition
mid-season).  Derivation: state → default zone, refined by ZIP prefix for the
states that straddle a boundary.  The ZIP table covers the well-known
exceptions, not every edge parcel; when a split state resolves without a ZIP
match we return the majority zone plus a warning so the operator can pin
``tz_name`` in config if the school sits on the wrong side.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from maxpreps_broadcast.models.envelope import ParseWarning

STATE_DEFAULT_TZ: dict[str, str] = {
    "al": "America/Chicago", "ak": "America/Anchorage", "az": "America/Phoenix",
    "ar": "America/Chicago", "ca": "America/Los_Angeles", "co": "America/Denver",
    "ct": "America/New_York", "de": "America/New_York", "dc": "America/New_York",
    "fl": "America/New_York", "ga": "America/New_York", "hi": "Pacific/Honolulu",
    "id": "America/Boise", "il": "America/Chicago", "in": "America/Indiana/Indianapolis",
    "ia": "America/Chicago", "ks": "America/Chicago", "ky": "America/New_York",
    "la": "America/Chicago", "me": "America/New_York", "md": "America/New_York",
    "ma": "America/New_York", "mi": "America/Detroit", "mn": "America/Chicago",
    "ms": "America/Chicago", "mo": "America/Chicago", "mt": "America/Denver",
    "ne": "America/Chicago", "nv": "America/Los_Angeles", "nh": "America/New_York",
    "nj": "America/New_York", "nm": "America/Denver", "ny": "America/New_York",
    "nc": "America/New_York", "nd": "America/Chicago", "oh": "America/New_York",
    "ok": "America/Chicago", "or": "America/Los_Angeles", "pa": "America/New_York",
    "ri": "America/New_York", "sc": "America/New_York", "sd": "America/Chicago",
    "tn": "America/Chicago", "tx": "America/Chicago", "ut": "America/Denver",
    "vt": "America/New_York", "va": "America/New_York", "wa": "America/Los_Angeles",
    "wv": "America/New_York", "wi": "America/Chicago", "wy": "America/Denver",
}

# States where a single default is wrong for part of the state.
SPLIT_STATES = {"fl", "id", "in", "ks", "ky", "mi", "nd", "ne", "or", "sd", "tn", "tx", "az", "nv"}

# ZIP prefix → zone, for the notable exceptions inside split states.
ZIP_PREFIX_TZ: dict[str, str] = {
    # Florida panhandle (Central)
    "324": "America/Chicago", "325": "America/Chicago",
    # Texas far west (Mountain): El Paso / Hudspeth
    "798": "America/Denver", "799": "America/Denver", "885": "America/Denver",
    # Tennessee east (Eastern)
    "376": "America/New_York", "377": "America/New_York", "378": "America/New_York",
    "379": "America/New_York", "374": "America/New_York",
    # Kentucky west (Central)
    "420": "America/Chicago", "421": "America/Chicago", "423": "America/Chicago",
    "424": "America/Chicago", "425": "America/Chicago", "426": "America/Chicago",
    "427": "America/Chicago",
    # Indiana northwest + southwest (Central)
    "463": "America/Chicago", "464": "America/Chicago", "476": "America/Chicago",
    "477": "America/Chicago",
    # Michigan western UP (Central)
    "499": "America/Menominee",
    # Idaho panhandle (Pacific)
    "838": "America/Los_Angeles",
    # Oregon: Malheur County (Mountain)
    "979": "America/Boise",
    # North Dakota southwest (Mountain)
    "586": "America/Denver",
    # South Dakota west (Mountain)
    "577": "America/Denver",
    # Nebraska panhandle (Mountain)
    "691": "America/Denver", "693": "America/Denver",
    # Kansas far west (Mountain)
    "677": "America/Denver", "678": "America/Denver",
}


def tz_for_school(
    state: str | None, zip_code: str | None = None
) -> tuple[str, ParseWarning | None]:
    """(IANA zone name, optional warning).  Falls back to UTC with a warning."""
    st = (state or "").strip().lower()
    zip5 = "".join(ch for ch in (zip_code or "") if ch.isdigit())[:5]
    if zip5:
        for width in (3,):
            zone = ZIP_PREFIX_TZ.get(zip5[:width])
            if zone:
                return zone, None
    default = STATE_DEFAULT_TZ.get(st)
    if default is None:
        return "UTC", ParseWarning(
            code="tz_unknown_state",
            message=f"unknown state {state!r}; timestamps left in UTC — set tz_name in config",
            path="team.stateCode",
        )
    if st in SPLIT_STATES:
        return default, ParseWarning(
            code="tz_split_state_default",
            message=(
                f"state {st!r} spans timezones; using majority zone {default}. "
                "Pin tz_name in config if the school sits on the other side."
            ),
            path="team.stateCode",
        )
    return default, None


def localize_naive(
    naive: datetime, tz_name: str
) -> tuple[datetime, datetime]:
    """Attach the school zone to a naive local datetime → (local, utc).

    ``zoneinfo`` resolves DST, so a 19:00 kickoff is UTC-7 before the November
    transition and UTC-8 after it without any special-casing here.
    """
    zone = ZoneInfo(tz_name)
    local = naive.replace(tzinfo=zone)
    return local, local.astimezone(UTC)


def parse_naive_iso(value: str) -> datetime | None:
    """Parse the wire's naive ISO stamps (``2026-08-21T19:00:00``)."""
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt
