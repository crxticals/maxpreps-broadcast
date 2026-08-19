"""Configuration and primary-school resolution.

Resolution order for the team argument (first match wins):

    explicit function argument → CLI flag → environment (``MAXPREPS_PRIMARY_*``)
    → config file (``~/.config/maxpreps-broadcast/config.toml``, overridable
    with ``--config``) → error pointing at ``maxpreps init``.

Multiple named profiles (``[profiles.football]``…) are supported because a
broadcast station covers several sports across a year.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field, field_validator

from maxpreps_broadcast import sports as catalogue
from maxpreps_broadcast.errors import PrimarySchoolNotConfiguredError, UnknownSportError
from maxpreps_broadcast.models.team import TeamRef
from maxpreps_broadcast.sports import Sport

DEFAULT_CONFIG_PATH = Path("~/.config/maxpreps-broadcast/config.toml")
ENV_PREFIX = "MAXPREPS_"


class PrimaryConfig(BaseModel):
    state: str | None = None
    city: str | None = None
    school_slug: str | None = None
    sport: str = "football"
    """Catalogue key of the default sport.  ``sports.active`` supersedes it for
    commands that fan out; this stays the single-sport fallback."""

    gender: str | None = None
    level: str | None = None
    season_year: str | None = None
    team_id: str | None = None
    sport_season_id: str | None = None
    all_season_id: str | None = None
    display_name: str | None = None
    abbreviation: str | None = None
    tz_name: str | None = Field(default=None, description="pin the school timezone explicitly")

    def team_ref(self) -> TeamRef | None:
        if not (self.state and self.city and self.school_slug):
            return None
        base = TeamRef(
            state=self.state,
            city=self.city,
            school=self.school_slug,
            sport=self.sport,
            gender=self.gender,
            level=self.level,
            season=self.season_year,
            team_id=self.team_id,
            sport_season_id=self.sport_season_id,
            all_season_id=self.all_season_id,
        )
        # A configured sport name is a catalogue key, not a URL segment: expand
        # it so the gender and season segments come along with it.
        try:
            return base.for_sport(catalogue.get(self.sport)).model_copy(
                update={
                    "team_id": self.team_id,
                    "sport_season_id": self.sport_season_id,
                    "all_season_id": self.all_season_id,
                }
            )
        except UnknownSportError:
            return base


class SportsConfig(BaseModel):
    """Which sports this station is currently covering.

    Selection is deliberately a plain ordered list of catalogue keys: it is what
    a producer-facing UI will POST, what the graphics loop rotates through, and
    what survives a restart in TOML.
    """

    active: list[str] = Field(default_factory=list)

    @field_validator("active")
    @classmethod
    def _known_and_bounded(cls, v: list[str]) -> list[str]:
        return [s.key for s in catalogue.resolve_many(v)]

    def resolved(self, fallback: str | None = None) -> list[Sport]:
        """The active sports, or the primary sport alone when none are set."""
        if self.active:
            return catalogue.resolve_many(self.active)
        if fallback:
            try:
                return [catalogue.get(fallback)]
            except UnknownSportError:
                return []
        return []


class HttpConfig(BaseModel):
    requests_per_second: float = 1.0
    max_concurrency: int = 4
    timeout_seconds: float = 10.0
    max_retries: int = 4
    breaker_failure_threshold: int = 5
    breaker_cooldown_seconds: float = 30.0
    user_agent: str = "maxpreps-broadcast/1.0 (school broadcast use; set contact in config)"
    respect_robots: bool = True


class CacheConfig(BaseModel):
    dir: str = "~/.cache/maxpreps-broadcast"
    ttl_roster: float = 21600
    ttl_schedule: float = 3600
    ttl_rankings: float = 43200
    ttl_live_score: float = 10
    ttl_search: float = 86400
    ttl_athlete: float = 21600
    ttl_standings: float = 21600
    memory_max_entries: int = 256

    def cache_dir(self) -> Path:
        return Path(self.dir).expanduser()


class ExportConfig(BaseModel):
    out_dir: str = "./broadcast-data"
    formats: list[str] = Field(default_factory=lambda: ["json", "mgjson", "csv"])
    atomic_writes: bool = True
    char_budgets: dict[str, int] = Field(
        default_factory=lambda: {
            "score_line": 18,
            "home_abbr": 4,
            "away_abbr": 4,
            "record_display": 16,
            "kickoff_display": 22,
            "next_game_display": 26,
            "lower_third_name": 18,
        }
    )


class OpponentOverride(BaseModel):
    """Scorebug-friendly names, because MaxPreps names are frequently too long."""

    display: str | None = None
    abbr: str | None = None
    logo: str | None = None


class Settings(BaseModel):
    primary: PrimaryConfig = Field(default_factory=PrimaryConfig)
    sports: SportsConfig = Field(default_factory=SportsConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    opponents: dict[str, OpponentOverride] = Field(default_factory=dict)
    profiles: dict[str, PrimaryConfig] = Field(default_factory=dict)
    offline: bool = False
    strict: bool = False

    # --------------------------------------------------------------- sports

    def active_sports(self, profile: str | None = None) -> list[Sport]:
        """Every sport a fan-out command should cover, never empty-by-surprise.

        Falls back to the profile's single configured sport so a station that
        never touches the multi-sport selection keeps its old behavior.
        """
        return self.sports.resolved(fallback=self.primary_for(profile).sport)

    def set_active_sports(self, names: list[str] | tuple[str, ...]) -> list[Sport]:
        """Replace the selection wholesale.  Raises before mutating on a bad
        name or an over-long list, so a rejected API call leaves state intact."""
        resolved = catalogue.resolve_many(names)
        self.sports.active = [s.key for s in resolved]
        return resolved

    def team_ref_for_sport(
        self, sport: Sport, *, profile: str | None = None, season: str | None = None
    ) -> TeamRef:
        """The primary school's ref, re-pointed at one catalogue sport."""
        return self.resolve_team(profile=profile).for_sport(sport, season=season)

    # ------------------------------------------------------------ resolution

    def primary_for(self, profile: str | None = None) -> PrimaryConfig:
        if profile:
            if profile not in self.profiles:
                raise PrimarySchoolNotConfiguredError(
                    f"profile {profile!r} not found; available: {sorted(self.profiles) or 'none'}"
                )
            return self.profiles[profile]
        return self.primary

    def resolve_team(
        self, explicit: TeamRef | str | None = None, *, profile: str | None = None
    ) -> TeamRef:
        if isinstance(explicit, TeamRef):
            return explicit
        if isinstance(explicit, str):
            return TeamRef.from_url(explicit)
        ref = self.primary_for(profile).team_ref()
        if ref is None:
            raise PrimarySchoolNotConfiguredError(
                "no team given and no primary school configured — run `maxpreps init` "
                "or set MAXPREPS_PRIMARY_STATE / _CITY / _SCHOOL_SLUG"
            )
        return ref

    def opponent_display(self, name: str | None) -> str | None:
        if name is None:
            return None
        override = self.opponents.get(name)
        return override.display if override and override.display else name

    def opponent_abbr(self, name: str | None) -> str | None:
        if name is None:
            return None
        override = self.opponents.get(name)
        return override.abbr if override else None


# ---------------------------------------------------------------- load/save


def _env_overrides() -> dict[str, Any]:
    """``MAXPREPS_PRIMARY_STATE=ca`` → ``{"primary": {"state": "ca"}}`` etc."""
    out: dict[str, dict[str, Any]] = {}
    sections = {"PRIMARY": "primary", "HTTP": "http", "CACHE": "cache", "EXPORT": "export"}
    for env_key, value in os.environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        rest = env_key[len(ENV_PREFIX):]
        for prefix, section in sections.items():
            if rest.startswith(prefix + "_"):
                field = rest[len(prefix) + 1 :].lower()
                out.setdefault(section, {})[field] = value
                break
        if rest == "OFFLINE":
            out.setdefault("_root", {})["offline"] = value.lower() in {"1", "true", "yes"}
    return out


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(config_path: Path | str | None = None) -> Settings:
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH.expanduser()
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    env = _env_overrides()
    root_extra = env.pop("_root", {})
    raw = _deep_merge(raw, env)
    raw = _deep_merge(raw, root_extra)
    return Settings.model_validate(raw)


def save_settings(settings: Settings, config_path: Path | str | None = None) -> Path:
    path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG_PATH.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.model_dump(exclude_none=True, exclude_defaults=False, mode="json")
    payload.pop("offline", None)
    payload.pop("strict", None)
    with path.open("wb") as fh:
        tomli_w.dump(payload, fh)
    return path
