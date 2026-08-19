"""Export layer: atomicity under crash, mgJSON validation, budgets, WCAG,
CSV/XML shape, template mapping resolution."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from maxpreps_broadcast.export import csv_out, mapping, xml_out
from maxpreps_broadcast.export.atomic import atomic_write_json, atomic_write_text
from maxpreps_broadcast.export.colors import (
    ColorEntry,
    TeamColorBlock,
    cache_mascot,
    contrast_ratio,
    contrast_text_for,
)
from maxpreps_broadcast.export.mgjson import (
    DynamicSeries,
    MgjsonBuilder,
    format_utc,
    validate_mgjson,
    write_mgjson,
)
from maxpreps_broadcast.export.strings import kickoff_display, score_line
from maxpreps_broadcast.parsers.normalize import fit_budget


class TestAtomicWrites:
    def test_replace_never_leaves_partial(self, tmp_path):
        target = tmp_path / "live.json"
        atomic_write_json(target, {"v": 1})
        original = target.read_text()

        # Crash mid-write: fsync explodes.  The old file must be untouched
        # and no temp litter left behind.
        with mock.patch("os.fsync", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                atomic_write_json(target, {"v": 2})
        assert target.read_text() == original
        assert list(tmp_path.iterdir()) == [target]

    def test_writes_are_complete_json(self, tmp_path):
        target = tmp_path / "x.json"
        atomic_write_json(target, {"a": "б", "n": None})
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded == {"a": "б", "n": None}

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "deep" / "er" / "x.txt"
        atomic_write_text(target, "hi")
        assert target.read_text() == "hi"


class TestColors:
    def test_wcag_text_on_dark_navy(self):
        assert contrast_text_for("#022C66") == "#FFFFFF"

    def test_wcag_text_on_athletic_gold(self):
        assert contrast_text_for("#FFD100") == "#000000"

    def test_contrast_ratio_symmetric(self):
        assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=0.01)
        assert contrast_ratio("#FFFFFF", "#000000") == pytest.approx(21.0, abs=0.01)

    def test_entry_forms(self):
        entry = ColorEntry.from_hex("#022C66")
        assert entry.rgb_255 == (2, 44, 102)
        assert entry.rgb_01 == (pytest.approx(0.0078, abs=1e-3),
                                pytest.approx(0.1725, abs=1e-3),
                                pytest.approx(0.4, abs=1e-3))

    def test_flat_block_keys(self):
        flat = TeamColorBlock.from_hexes("#022C66", "#FFFFFF").flat("home")
        assert flat["home_primary_hex"] == "#022C66"
        assert flat["home_secondary_hex"] == "#FFFFFF"
        assert {k for k in flat if k.startswith("home_primary_")} >= {
            "home_primary_hex", "home_primary_r", "home_primary_r01", "home_primary_text",
        }

    def test_mascot_failure_returns_none_never_raises(self, tmp_path):
        def boom(url):
            raise RuntimeError("no network")

        assert cache_mascot("https://x/y.gif", tmp_path, fetch_bytes=boom) is None

    def test_mascot_gif_converts_to_png(self, tmp_path):
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("P", (4, 4)).save(buffer, format="GIF")
        gif_bytes = buffer.getvalue()
        path = cache_mascot("https://x/mascot.gif?w=1", tmp_path, fetch_bytes=lambda u: gif_bytes)
        assert path is not None and path.suffix == ".png"
        with Image.open(path) as img:
            assert img.format == "PNG"


class TestBroadcastStrings:
    def test_fit_budget_word_boundary(self):
        assert fit_budget("Brea Olinda Wildcats", 12) == "Brea Olinda"

    def test_fit_budget_hard_cut_when_no_space(self):
        assert fit_budget("Supercalifragilistic", 7) == "Superca"

    def test_fit_budget_short_passthrough(self):
        assert fit_budget("NW", 12) == "NW"

    def test_score_line_pregame_dashes(self):
        assert score_line("NW", None, "IRV", None) == "NW – — IRV –"

    def test_score_line_live(self):
        assert score_line("NW", 21, "IRV", 14) == "NW 21 — IRV 14"

    def test_kickoff_display(self):
        moment = datetime(2026, 8, 21, 19, 0)
        assert kickoff_display(moment) == "FRI AUG 21 · 7:00 PM"
        assert kickoff_display(moment, time_tba=True) == "FRI AUG 21 · TIME TBA"
        assert kickoff_display(None) == "DATE TBA"


class TestMgjson:
    def _builder(self) -> MgjsonBuilder:
        builder = MgjsonBuilder()
        builder.add_static("home_abbr", "Home Abbr", "NW")
        builder.add_static("home_score", "Home Score", 21)
        builder.add_series(DynamicSeries("home_score_t", "Home Score", [
            (datetime(2026, 10, 16, 19, 0, tzinfo=UTC), 0.0),
            (datetime(2026, 10, 16, 19, 30, tzinfo=UTC), 7.0),
        ]))
        return builder

    def test_golden_document_validates(self):
        assert validate_mgjson(self._builder().build()) == []

    def test_time_format_exact(self):
        assert format_utc(datetime(2026, 10, 16, 19, 0, 0, 123456, tzinfo=UTC)) == \
            "2026-10-16T19:00:00.123Z"

    def test_scores_use_hold_interpolation(self):
        doc = self._builder().build()
        dynamic = next(e for e in doc["dataOutline"] if e["objectType"] == "dataDynamic")
        assert dynamic["interpolation"] == "hold"
        assert dynamic["hasExpectedFrequecyB"] is False  # (sic) schema spelling

    def test_sample_values_are_strings(self):
        doc = self._builder().build()
        sample = doc["dataDynamicSamples"][0]["samples"][0]
        assert isinstance(sample["value"], str)

    @pytest.mark.parametrize("mutate,fragment", [
        (lambda d: d.pop("version"), "version"),
        (lambda d: d["dataOutline"][0].pop("matchName"), "matchName"),
        (lambda d: d["dataOutline"][2].pop("sampleSetID"), "sampleSetID"),
        (lambda d: d["dataDynamicSamples"][0]["samples"].pop(), "sampleCount"),
        (lambda d: d["dataDynamicSamples"][0]["samples"][0].__setitem__("value", 7), "string"),
        (lambda d: d["dataDynamicSamples"][0]["samples"][0].__setitem__("time", "2026-10-16"), "ISO"),
        (lambda d: d.__setitem__("dynamicSamplesPresentB", False), "inconsistent"),
    ])
    def test_corruptions_rejected(self, mutate, fragment):
        doc = self._builder().build()
        mutate(doc)
        problems = validate_mgjson(doc)
        assert problems, f"expected a problem mentioning {fragment!r}"
        assert any(fragment.lower() in p.lower() for p in problems)

    def test_write_refuses_invalid(self, tmp_path):
        builder = self._builder()
        with mock.patch("maxpreps_broadcast.export.mgjson.validate_mgjson",
                        return_value=["boom"]):
            with pytest.raises(ValueError):
                write_mgjson(tmp_path / "x.mgjson", builder)
        assert not (tmp_path / "x.mgjson").exists()


class TestCsvXml:
    def test_csv_union_of_columns(self):
        text = csv_out.rows_to_delimited([{"a": 1}, {"a": 2, "b": "x,y"}])
        lines = text.strip().splitlines()
        assert lines[0] == "a,b"
        assert lines[2] == '2,"x,y"'          # quoting handled by csv module

    def test_xml_escapes_and_nests(self, tmp_path):
        path = xml_out.write_xml(tmp_path / "x.xml", {
            "name": 'A & B <"quoted">',
            "games": [{"opp": "Irvine"}, {"opp": "Portola"}],
        })
        root = ET.parse(path).getroot()
        assert root.find("field[@name='name']").text == 'A & B <"quoted">'
        assert [r.find("field[@name='opp']").text for r in root.findall("row")] == \
            ["Irvine", "Portola"]


class TestMapping:
    def test_resolve_paths_including_lists(self):
        view = {"home_score": "21", "games": [{"opponent": "Irvine"}, {"opponent": "Portola"}]}
        assert mapping.resolve_path(view, "home_score") == "21"
        assert mapping.resolve_path(view, "games.1.opponent") == "Portola"
        assert mapping.resolve_path(view, "games.9.opponent") is None
        assert mapping.resolve_path(view, "nope.deep") is None

    def test_render_with_defaults_and_missing(self, tmp_path):
        m = mapping.TemplateMapping(
            template="t", source="live",
            layers={"SCORE": "home_score", "LOGO": "home_logo_path"},
            defaults={"LOGO": "placeholder.png"},
        )
        rendered = mapping.render_mapping(m, {"home_score": "21"})
        assert rendered["layers"] == {"SCORE": "21", "LOGO": "placeholder.png"}
        assert rendered["missing_fields"] == ["home_logo_path"]

    def test_repo_scorebug_template_loads_and_renders(self, tmp_path):
        template_path = Path(__file__).parent.parent.parent / "templates" / "scorebug.mapping.yaml"
        m = mapping.TemplateMapping.load(template_path)
        assert m.source == "live"
        rendered = mapping.render_mapping(m, {"home_abbr": "NW", "home_score": "21"})
        assert rendered["layers"]["HOME ABBR"] == "NW"
