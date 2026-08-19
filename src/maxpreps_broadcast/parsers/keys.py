"""Positional key lists for MaxPreps' wire format.

MaxPreps ships roster and schedule payloads as bare positional arrays with the
field names stripped, rehydrated client-side by the site's own
``deserializeObject(keys, row)``.  The three lists below are lifted from the
site bundle (via the MIT-licensed reverse-engineering in
github.com/chrischall/maxpreps-mcp, captured 2026-08-01 against buildId
1785513693) and verified against real payloads in ``tests/fixtures``.

``TEAM_KEYS`` repeats ``teamId``, ``sportSeasonId`` and ``resultString``.  That
is faithful to the site: its deserializer assigns in order so the last
occurrence wins, and the duplicates are load-bearing for index alignment.
See docs/ENDPOINTS.md for how to re-derive the lists if the site changes.
"""

from __future__ import annotations

from typing import Any

ROSTER_KEYS: tuple[str, ...] = (
    "linkedAthlete", "linkedParents", "canStartChat", "accountInformation", "athleteId",
    "firstName", "lastName", "classYear", "jersey", "heightInches", "heightFeet", "weight",
    "position1", "position2", "position3", "hasStats", "isCaptain", "isDeleted", "photoUrl",
    "secondaryPhotoUrl", "weightClass", "isPlayerOfTheGame", "isFemale", "bio", "hasPhoto",
    "rosterId", "schoolId", "sportSeasonId", "sportSeasonName", "careerProfileId", "createdOn",
    "canonicalUrl", "formattedPositions", "formattedName", "formattedHeight", "calculatedHeight",
    "formattedClassYear",
)

CONTEST_KEYS: tuple[str, ...] = (
    "teams", "contestId", "createdOn", "isDeleted", "hasResult", "location", "details", "state",
    "city", "name", "dateCode", "date", "tournamentBracketId", "tournamentId", "sportSeasonId",
    "contestState", "allowEditContestResults", "hasContestPage", "canonicalUrl", "isDateTba",
    "isTimeTba", "contestAlias", "isLiveGameInProgress", "overtimePeriodsPlayed",
    "overtimeShortAlias", "currentLivePeriod", "currentScorerUserId", "rolesWhoCanEnterScores",
    "reasonWhyCannotEnterScores", "description", "isLiveScoringEnabled", "isGameChangerConnected",
    "hasGameChangerImportedStats", "bracketGameIndex", "bracketGamesInMatchup", "goFanUrl",
    "nfhsStreamUrl", "currentTeam", "opponentTeam", "bracketMatchupId", "bracketIsPublished",
)

TEAM_KEYS: tuple[str, ...] = (
    "id", "teamId", "sportSeasonId", "resultString", "index", "result", "score", "isTeamTBA",
    "isForfeit", "isDeleted", "hasStats", "homeAwayType", "contestType", "teamCanonicalUrl",
    "name", "city", "state", "address", "zipCode", "formattedName", "mascotUrl", "mascot",
    "color1", "color2", "schoolNameAcronym", "teamId", "contestId", "sportSeasonId", "teamId",
    "calculatedTeamContestResult", "currentLiveScore", "resultString",
)

# Wire semantics, verified against real payloads (see docs/ENDPOINTS.md):
HOME_AWAY_HOME = 0  # homeAwayType: 0 = home, 1 = away
CONTEST_STATE_SCHEDULED = 1
CONTEST_STATE_FINAL = 4
CALC_RESULT_WIN = 2  # calculatedTeamContestResult
CALC_RESULT_LOSS = 3


def deserialize_object(keys: tuple[str, ...], row: object) -> dict[str, Any] | None:
    """Rehydrate one positional row, mirroring the site's semantics exactly:
    duplicate keys overwrite in order; None stays None; an already-hydrated
    dict passes through untouched (safe to apply twice)."""
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    if not isinstance(row, list):
        return None
    out: dict[str, Any] = {}
    for i, key in enumerate(keys):
        out[key] = row[i] if i < len(row) else None
    return out
