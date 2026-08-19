from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from maxpreps_broadcast.config import CacheConfig, ExportConfig, PrimaryConfig, Settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture()
def fixture():
    return load_fixture


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        primary=PrimaryConfig(
            state="ca", city="irvine", school_slug="northwood-timberwolves",
            sport="football", display_name="Northwood", abbreviation="NW",
        ),
        cache=CacheConfig(dir=str(tmp_path / "cache")),
        export=ExportConfig(out_dir=str(tmp_path / "out")),
    )


@pytest.fixture()
def offline_settings(settings: Settings) -> Settings:
    settings.offline = True
    return settings
