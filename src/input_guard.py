"""Input guard rails for untrusted DXF intake.

The parse itself is currently synchronous in this CLI process, but these
limits give the read entrypoint an explicit quota boundary so it can later be
executed in a worker/container without changing callers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class DXFInputError(ValueError):
    """Raised when a DXF input exceeds an intake quota."""


@dataclass
class DXFInputLimits:
    max_bytes: int = 50 * 1024 * 1024
    max_entities: int = 200_000
    max_parse_seconds: float = 120.0


def check_dxf_file(path: str | Path, limits: DXFInputLimits) -> list[str]:
    """Cheap pre-parse checks that do not require opening the document."""
    path = Path(path)
    violations: list[str] = []
    if not path.exists():
        violations.append(f"file not found: {path}")
        return violations
    if path.suffix.lower() != ".dxf":
        violations.append(f"unsupported extension: {path.suffix or '<none>'}")
    size = path.stat().st_size
    if size > limits.max_bytes:
        violations.append(
            f"file size {size} bytes exceeds {limits.max_bytes} bytes"
        )
    return violations


def guarded_read(
    filepath: str | Path,
    read_func: Callable,
    limits: DXFInputLimits,
) -> tuple:
    """Run ``read_func(filepath)`` under file/entity/time quotas.

    ``read_func`` must return ``(parts_data, doc)`` like ``dxf_reader.read_dxf``
    and ``drawing_profile.read_dxf_with_profile``.
    """
    violations = check_dxf_file(filepath, limits)
    if violations:
        raise DXFInputError("; ".join(violations))

    started = time.monotonic()
    parts_data, doc = read_func(filepath)
    elapsed = time.monotonic() - started
    if elapsed > limits.max_parse_seconds:
        raise DXFInputError(
            f"parse time {elapsed:.1f}s exceeds {limits.max_parse_seconds}s"
        )

    entity_count = len(doc.modelspace()) if doc is not None else 0
    if entity_count > limits.max_entities:
        raise DXFInputError(
            f"entity count {entity_count} exceeds {limits.max_entities}"
        )
    return parts_data, doc
