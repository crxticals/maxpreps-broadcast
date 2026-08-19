"""Typed models: the single normalized shape every source tier parses into."""

from maxpreps_broadcast.models.athlete import AthleteProfile, StatLine, TeamHistoryEntry
from maxpreps_broadcast.models.contest import (
    Contest,
    ContestSide,
    ContestType,
    GameResult,
    Schedule,
    Tournament,
    Venue,
)
from maxpreps_broadcast.models.envelope import CacheState, ParseMode, ParseWarning, Response, SourceTier
from maxpreps_broadcast.models.ranking import RankingEntry, Rankings
from maxpreps_broadcast.models.roster import Roster, RosterEntry, RosterSort, sort_roster
from maxpreps_broadcast.models.score import (
    ChangeEvent,
    GameStatus,
    ScoreState,
    ScoringPlay,
    SideInfo,
    diff_score_states,
)
from maxpreps_broadcast.models.team import School, TeamInfo, TeamRef

__all__ = [
    "AthleteProfile",
    "CacheState",
    "ChangeEvent",
    "Contest",
    "ContestSide",
    "ContestType",
    "GameResult",
    "GameStatus",
    "ParseMode",
    "ParseWarning",
    "RankingEntry",
    "Rankings",
    "Response",
    "Roster",
    "RosterEntry",
    "RosterSort",
    "Schedule",
    "School",
    "ScoreState",
    "ScoringPlay",
    "SideInfo",
    "SourceTier",
    "StatLine",
    "TeamHistoryEntry",
    "TeamInfo",
    "TeamRef",
    "Tournament",
    "Venue",
    "diff_score_states",
    "sort_roster",
]
