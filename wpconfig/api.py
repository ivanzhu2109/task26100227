"""Public entry points: check() and rewrite()."""

from __future__ import annotations

from typing import Dict, List, Tuple

from .analyze import Setting, find_settings
from .lexer import tokenize
from .model import (
    Changes,
    CheckReport,
    TargetReport,
    UnsafeRewriteError,
    WP_DEBUG,
)


def _validate_changes(changes: Changes) -> None:
    if changes.wp_debug is not None and not isinstance(changes.wp_debug, bool):
        raise TypeError("wp_debug must be a bool or None")
    for value in (changes.wp_memory_limit, changes.table_prefix):
        if value is not None and not isinstance(value, str):
            raise TypeError("wp_memory_limit/table_prefix must be str or None")


def _settings_by_target(text: str) -> Dict[str, List[Setting]]:
    grouped: Dict[str, List[Setting]] = {}
    for s in find_settings(text, tokenize(text)):
        grouped.setdefault(s.target, []).append(s)
    return grouped


def _evaluate(grouped: Dict[str, List[Setting]],
              changes: Changes) -> CheckReport:
    results: List[TargetReport] = []
    for target, _value in changes.specified():
        found = grouped.get(target, [])
        if len(found) == 0:
            results.append(TargetReport(target, False, "no setting found"))
        elif len(found) > 1:
            results.append(
                TargetReport(target, False,
                             f"multiple settings found ({len(found)})")
            )
        elif not found[0].valid:
            results.append(TargetReport(target, False, found[0].reason))
        else:
            results.append(TargetReport(target, True))
    return CheckReport(tuple(results))


def check(source: bytes, changes: Changes) -> CheckReport:
    """Report whether every specified target can be rewritten safely."""
    _validate_changes(changes)
    return _evaluate(_settings_by_target(source.decode("utf-8")), changes)


def _encode_string(value: str) -> str:
    """Single-quoted PHP literal: no interpolation, decodes back exactly."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _encode_value(target: str, value: object) -> str:
    if target == WP_DEBUG:
        return "true" if value else "false"
    return _encode_string(value)  # type: ignore[arg-type]


def rewrite(source: bytes, changes: Changes) -> bytes:
    """Return the source with the specified target values replaced.

    Raises UnsafeRewriteError if any specified target cannot be safely
    rewritten; in that case nothing is returned and no partial result
    is produced.
    """
    _validate_changes(changes)
    text = source.decode("utf-8")
    grouped = _settings_by_target(text)
    report = _evaluate(grouped, changes)
    if not report.ok:
        raise UnsafeRewriteError(report)
    replacements: List[Tuple[int, int, str]] = []
    for target, value in changes.specified():
        setting = grouped[target][0]
        replacements.append(
            (setting.value_start, setting.value_end,
             _encode_value(target, value))
        )
    replacements.sort(key=lambda r: r[0], reverse=True)
    for start, end, literal in replacements:
        text = text[:start] + literal + text[end:]
    return text.encode("utf-8")
