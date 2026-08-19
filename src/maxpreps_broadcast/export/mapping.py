"""Template mappings: ``templates/{name}.mapping.yaml`` maps exported field
paths to After Effects layer names, so a graphics operator can re-wire a
scorebug without touching Python.

A mapping file looks like:

    template: scorebug
    source: live            # live | schedule | roster
    layers:
      "HOME SCORE":  home_score
      "AWAY SCORE":  away_score
      "CLOCK":       clock_display
      "HOME BG":     home_primary_hex

``render`` resolves each field path against the flat export view and emits
``{name}.render.json``: ``{"layers": {"HOME SCORE": "21", ...}}`` — the file
an AE ingest script (or a person against a checklist) applies 1:1 to layers.
Dotted paths (``games.0.opponent``) reach into lists for schedule crawls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from maxpreps_broadcast.export.atomic import atomic_write_json


class TemplateMapping(BaseModel):
    template: str
    source: str = "live"
    layers: dict[str, str] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> TemplateMapping:
        with Path(path).open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return cls.model_validate(raw)


def resolve_path(view: dict[str, Any], dotted: str) -> Any:
    node: Any = view
    for part in dotted.split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        else:
            return None
    return node


def render_mapping(mapping: TemplateMapping, view: dict[str, Any]) -> dict[str, Any]:
    layers: dict[str, Any] = {}
    missing: list[str] = []
    for layer_name, field_path in mapping.layers.items():
        value = resolve_path(view, field_path)
        if value is None:
            value = mapping.defaults.get(layer_name, "")
            missing.append(field_path)
        layers[layer_name] = value
    return {
        "template": mapping.template,
        "source": mapping.source,
        "layers": layers,
        "missing_fields": missing,
    }


def write_render(out_dir: Path, mapping: TemplateMapping, view: dict[str, Any]) -> Path:
    rendered = render_mapping(mapping, view)
    return atomic_write_json(out_dir / f"{mapping.template}.render.json", rendered)


def find_template(name: str, *, search_dirs: list[Path]) -> Path:
    for directory in search_dirs:
        candidate = directory / f"{name}.mapping.yaml"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"no template {name!r}.mapping.yaml in: {', '.join(str(d) for d in search_dirs)}"
    )
