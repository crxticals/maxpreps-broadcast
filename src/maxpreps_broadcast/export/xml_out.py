"""XML export for AE workflows wired to pt_ImportSubtitles-style XML sources.

Flat dicts become <data><field name="...">value</field>...</data>; lists of
dicts become repeated <row> elements.  Everything is escaped via ElementTree —
no hand-built markup.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from maxpreps_broadcast.export.atomic import atomic_write_bytes


def _fill(parent: ET.Element, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                row = ET.SubElement(parent, "row", {"of": key})
                if isinstance(item, dict):
                    _fill(row, item)
                else:
                    row.text = "" if item is None else str(item)
        elif isinstance(value, dict):
            child = ET.SubElement(parent, key)
            _fill(child, value)
        else:
            field = ET.SubElement(parent, "field", {"name": key})
            field.text = "" if value is None else str(value)


def to_xml_bytes(data: dict[str, Any], *, root: str = "data") -> bytes:
    element = ET.Element(root)
    _fill(element, data)
    ET.indent(element)
    return bytes(ET.tostring(element, encoding="utf-8", xml_declaration=True))


def write_xml(path: Path | str, data: dict[str, Any], *, root: str = "data") -> Path:
    return atomic_write_bytes(path, to_xml_bytes(data, root=root))
