"""Public data model for the wp-config rewrite library."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


WP_DEBUG = "WP_DEBUG"
WP_MEMORY_LIMIT = "WP_MEMORY_LIMIT"
TABLE_PREFIX = "table_prefix"


@dataclass(frozen=True)
class Changes:
    """Requested changes. A field left as None means "not specified"."""

    wp_debug: Optional[bool] = None
    wp_memory_limit: Optional[str] = None
    table_prefix: Optional[str] = None

    def specified(self) -> Tuple[Tuple[str, object], ...]:
        out = []
        if self.wp_debug is not None:
            out.append((WP_DEBUG, self.wp_debug))
        if self.wp_memory_limit is not None:
            out.append((WP_MEMORY_LIMIT, self.wp_memory_limit))
        if self.table_prefix is not None:
            out.append((TABLE_PREFIX, self.table_prefix))
        return tuple(out)


@dataclass(frozen=True)
class TargetReport:
    target: str
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class CheckReport:
    results: Tuple[TargetReport, ...]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def for_target(self, target: str) -> Optional[TargetReport]:
        for r in self.results:
            if r.target == target:
                return r
        return None


class UnsafeRewriteError(Exception):
    """Raised when a requested change cannot be applied safely."""

    def __init__(self, report: CheckReport):
        self.report = report
        bad = "; ".join(
            f"{r.target}: {r.reason}" for r in report.results if not r.ok
        )
        super().__init__(bad or "unsafe rewrite")
