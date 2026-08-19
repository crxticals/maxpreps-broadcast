"""mgJSON (Motion Graphics JSON) export for After Effects data-driven
templates — the format behind File → Import as "MGJSON".

Structure follows the published MGJSON 2.0.0 schema (verified against the
JuanIrache/mgjson reference and demo during reconnaissance; see
docs/AFTER_EFFECTS.md):

* ``dataOutline`` declares every stream: ``dataStatic`` values and
  ``dataDynamic`` sample series (keyed by ``sampleSetID`` — note the real
  schema key is ``sampleSetID``, not "sampleSetting" as some notes claim).
* string statics need ``paddedStringProperties``; numeric streams use
  ``numberStringProperties`` with a digit ``pattern`` and ``range``.
* dynamic samples live in ``dataDynamicSamples``; numberString sample values
  are *strings*; times are ISO-8601 UTC with exactly 3 fractional digits.
* the schema's ``hasExpectedFrequecyB`` typo is faithful — AE expects it.

Scores use ``interpolation: "hold"`` — a score is a step function, and linear
interpolation would animate 14 → 21 through a nonsense 17.5.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from maxpreps_broadcast.export.atomic import atomic_write_text

VERSION = "MGJSON2.0.0"


def format_utc(moment: datetime) -> str:
    utc = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _padded_string_props(value: str) -> dict[str, Any]:
    max_len = max(1, len(value))
    return {
        "maxLen": max_len,
        "maxDigitsInStrLength": len(str(max_len)),
        "eventMarkerB": False,
    }


def _number_props(values: list[float]) -> dict[str, Any]:
    finite = [v for v in values if math.isfinite(v)] or [0.0]
    lo, hi = min(finite), max(finite)
    digits_integer = max(len(str(int(abs(v)))) for v in (lo, hi, 0))
    digits_decimal = 0 if all(float(v).is_integer() for v in finite) else 4
    span = max(1.0, abs(hi - lo))
    return {
        "pattern": {
            "digitsInteger": digits_integer,
            "digitsDecimal": digits_decimal,
            "isSigned": lo < 0,
        },
        "range": {
            "occuring": {"min": lo, "max": hi},
            "legal": {"min": lo - span, "max": hi + span},
        },
    }


def _number_string(value: float, digits_decimal: int) -> str:
    return str(int(value)) if digits_decimal == 0 else f"{value:.{digits_decimal}f}"


@dataclass
class DynamicSeries:
    match_name: str
    display_name: str
    samples: list[tuple[datetime, float]]
    interpolation: str = "hold"  # scores are step functions
    units: str | None = None

    def sample_set_id(self) -> str:
        return self.match_name


@dataclass
class MgjsonBuilder:
    creator: str = "maxpreps-broadcast"
    statics: list[tuple[str, str, Any]] = field(default_factory=list)  # (matchName, displayName, value)
    dynamics: list[DynamicSeries] = field(default_factory=list)

    def add_static(self, match_name: str, display_name: str, value: Any) -> None:
        self.statics.append((match_name, display_name, value))

    def add_series(self, series: DynamicSeries) -> None:
        if series.samples:
            self.dynamics.append(series)

    def build(self) -> dict[str, Any]:
        outline: list[dict[str, Any]] = []
        sample_sets: list[dict[str, Any]] = []
        for match_name, display_name, value in self.statics:
            if isinstance(value, bool):
                value = "true" if value else "false"
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                num = float(value)
                outline.append(
                    {
                        "objectType": "dataStatic",
                        "displayName": display_name,
                        "matchName": match_name,
                        "dataType": {"type": "number", "numberStringProperties": _number_props([num])},
                        "value": num,
                    }
                )
            else:
                text = "" if value is None else str(value)
                outline.append(
                    {
                        "objectType": "dataStatic",
                        "displayName": display_name,
                        "matchName": match_name,
                        "dataType": {
                            "type": "paddedString",
                            "paddedStringProperties": _padded_string_props(text),
                        },
                        "value": {"length": len(text), "str": text},
                    }
                )
        for series in self.dynamics:
            values = [v for _, v in series.samples]
            props = _number_props(values)
            digits_decimal = props["pattern"]["digitsDecimal"]
            outline.append(
                {
                    "objectType": "dataDynamic",
                    "displayName": series.display_name,
                    "matchName": series.match_name,
                    "sampleSetID": series.sample_set_id(),
                    "dataType": {"type": "numberString", "numberStringProperties": props},
                    "interpolation": series.interpolation,
                    "hasExpectedFrequecyB": False,  # (sic) — the schema's own spelling
                    "sampleCount": len(series.samples),
                }
            )
            sample_sets.append(
                {
                    "sampleSetID": series.sample_set_id(),
                    "samples": [
                        {"time": format_utc(t), "value": _number_string(v, digits_decimal)}
                        for t, v in series.samples
                    ],
                }
            )
        doc: dict[str, Any] = {
            "version": VERSION,
            "creator": self.creator,
            "dynamicSamplesPresentB": bool(sample_sets),
            "dynamicDataInfo": {
                "useTimecodeB": False,
                "utcInfo": {"precisionLength": 3, "isGMT": True},
            },
            "dataOutline": outline,
        }
        if sample_sets:
            doc["dataDynamicSamples"] = sample_sets
        return doc


def validate_mgjson(doc: dict[str, Any]) -> list[str]:
    """Structural round-trip validation before anything reaches an AE import."""
    problems: list[str] = []
    if doc.get("version") != VERSION:
        problems.append(f"version must be {VERSION!r}, got {doc.get('version')!r}")
    if "dataOutline" not in doc or not isinstance(doc["dataOutline"], list):
        problems.append("dataOutline missing or not a list")
        return problems
    declared: dict[str, int] = {}
    for i, entry in enumerate(doc["dataOutline"]):
        where = f"dataOutline[{i}]"
        for key in ("objectType", "displayName", "matchName", "dataType"):
            if key not in entry:
                problems.append(f"{where}: missing {key}")
        object_type = entry.get("objectType")
        data_type = entry.get("dataType", {})
        kind = data_type.get("type")
        if object_type == "dataStatic":
            if kind == "paddedString":
                props = data_type.get("paddedStringProperties")
                value = entry.get("value", {})
                if not isinstance(props, dict):
                    problems.append(f"{where}: paddedString without paddedStringProperties")
                elif isinstance(value, dict):
                    text = value.get("str", "")
                    if value.get("length") != len(text):
                        problems.append(f"{where}: value.length != len(str)")
                    if len(text) > props.get("maxLen", 0):
                        problems.append(f"{where}: str longer than maxLen")
                else:
                    problems.append(f"{where}: paddedString value must be {{length, str}}")
            elif kind == "number":
                if not isinstance(entry.get("value"), (int, float)):
                    problems.append(f"{where}: number static without numeric value")
            else:
                problems.append(f"{where}: unsupported static type {kind!r}")
        elif object_type == "dataDynamic":
            sample_set_id = entry.get("sampleSetID")
            if not sample_set_id:
                problems.append(f"{where}: dataDynamic without sampleSetID")
            else:
                declared[sample_set_id] = entry.get("sampleCount", -1)
            if entry.get("interpolation") not in {"linear", "hold"}:
                problems.append(f"{where}: interpolation must be linear|hold")
            if "hasExpectedFrequecyB" not in entry:
                problems.append(f"{where}: missing hasExpectedFrequecyB (schema spelling)")
        else:
            problems.append(f"{where}: unknown objectType {object_type!r}")
    sample_sets = doc.get("dataDynamicSamples", [])
    if declared and not sample_sets:
        problems.append("dynamic streams declared but dataDynamicSamples missing")
    seen: set[str] = set()
    for i, sample_set in enumerate(sample_sets):
        where = f"dataDynamicSamples[{i}]"
        set_id = sample_set.get("sampleSetID")
        seen.add(set_id)
        if set_id not in declared:
            problems.append(f"{where}: sampleSetID {set_id!r} not declared in dataOutline")
            continue
        samples = sample_set.get("samples", [])
        if declared[set_id] != len(samples):
            problems.append(f"{where}: sampleCount {declared[set_id]} != {len(samples)} samples")
        for j, sample in enumerate(samples):
            time_str = sample.get("time", "")
            if not (time_str.endswith("Z") and "T" in time_str and "." in time_str
                    and len(time_str.split(".")[-1]) == 4):
                problems.append(f"{where}.samples[{j}]: time {time_str!r} not ISO-8601 with ms precision + Z")
                break
            if not isinstance(sample.get("value"), str):
                problems.append(f"{where}.samples[{j}]: numberString sample value must be a string")
                break
    missing = set(declared) - seen
    if missing:
        problems.append(f"declared sample sets with no samples: {sorted(missing)}")
    if doc.get("dynamicSamplesPresentB") != bool(sample_sets):
        problems.append("dynamicSamplesPresentB inconsistent with dataDynamicSamples")
    return problems


def write_mgjson(path: Path, builder: MgjsonBuilder) -> Path:
    doc = builder.build()
    problems = validate_mgjson(doc)
    if problems:
        raise ValueError("mgJSON failed validation: " + "; ".join(problems))
    return atomic_write_text(path, json.dumps(doc, indent=2) + "\n")


# ------------------------------------------------------- view → builders


HistoryPoint = tuple[datetime, int | None, int | None, int | None]


def mgjson_for_live(
    view: dict[str, Any], *, history: list[HistoryPoint] | None = None
) -> MgjsonBuilder:
    """``view`` is the flat live.json dict.  ``history`` optionally carries
    (utc_time, home_score, away_score, period) points accumulated by watch
    mode, exported as hold-interpolated dynamic streams."""
    builder = MgjsonBuilder()
    for key, value in view.items():
        if isinstance(value, (dict, list)):
            continue
        builder.add_static(key, key.replace("_", " ").title(), value)
    if history:
        home = [(t, float(h)) for t, h, _, _ in history if h is not None]
        away = [(t, float(a)) for t, _, a, _ in history if a is not None]
        periods = [(t, float(p)) for t, _, _, p in history if p is not None]
        builder.add_series(DynamicSeries("home_score_t", "Home Score", home))
        builder.add_series(DynamicSeries("away_score_t", "Away Score", away))
        builder.add_series(DynamicSeries("period_t", "Period", periods))
    return builder
