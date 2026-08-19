"""CSV/TSV export — for AE spreadsheet-style data imports and plain checking.

Column order is stable (first row's key order), values are stringified, and
the write is atomic like everything else.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from maxpreps_broadcast.export.atomic import atomic_write_text


def rows_to_delimited(rows: list[dict[str, Any]], *, delimiter: str = ",") -> str:
    if not rows:
        return ""
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, delimiter=delimiter, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if v is None else v for k, v in row.items()})
    return buffer.getvalue()


def write_csv(path: Path | str, rows: list[dict[str, Any]]) -> Path:
    return atomic_write_text(path, rows_to_delimited(rows, delimiter=","))


def write_tsv(path: Path | str, rows: list[dict[str, Any]]) -> Path:
    return atomic_write_text(path, rows_to_delimited(rows, delimiter="\t"))
