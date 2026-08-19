"""The standard response envelope every public function returns."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

SourceTier = Literal["json_api", "hydration", "html"]
CacheState = Literal["fresh", "cached", "stale", "last_known_good"]


class ParseMode(str, Enum):
    """``lenient`` logs and preserves drift; ``strict`` (CI) makes it fatal."""

    LENIENT = "lenient"
    STRICT = "strict"


class ParseWarning(BaseModel):
    """A non-fatal parse issue: unknown field, missing optional, suspicious value."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    path: str | None = None


class Response(BaseModel, Generic[T]):
    """Envelope carrying provenance and freshness alongside the data.

    A stale value flagged as stale beats a missing value every time, so the
    envelope makes freshness impossible to ignore: ``cache_state``, ``stale``
    and ``data_age_seconds`` travel with every payload.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    data: T
    fetched_at: datetime = Field(description="tz-aware UTC time the payload was fetched from origin")
    source_tier: SourceTier
    cache_state: CacheState
    data_age_seconds: float
    stale: bool
    warnings: list[ParseWarning] = Field(default_factory=list)
    request_id: str

    @classmethod
    def wrap(
        cls,
        data: T,
        *,
        fetched_at: datetime,
        source_tier: SourceTier,
        cache_state: CacheState,
        warnings: list[ParseWarning],
        request_id: str,
        now: datetime | None = None,
    ) -> Response[T]:
        now = now or datetime.now(UTC)
        age = max(0.0, (now - fetched_at).total_seconds())
        return cls(
            data=data,
            fetched_at=fetched_at,
            source_tier=source_tier,
            cache_state=cache_state,
            data_age_seconds=round(age, 3),
            stale=cache_state in ("stale", "last_known_good"),
            warnings=warnings,
            request_id=request_id,
        )
