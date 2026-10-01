"""对外能力：配置检查与生成改写结果。只在内存中处理字节。"""

from __future__ import annotations

from .analyze import SearchOutcome, _significant, find_define_sites, find_table_prefix_sites
from .lexer import tokenize
from .types import (
    CheckResult,
    Issue,
    IssueCode,
    RewriteError,
    TargetName,
    TargetReport,
    TargetValue,
)

_EXPECTED_TYPES = {
    TargetName.WP_DEBUG: bool,
    TargetName.WP_MEMORY_LIMIT: str,
    TargetName.TABLE_PREFIX: str,
}


def _encode_string_literal(value: str) -> bytes:
    """把新值编码为单引号 PHP 字符串：只转义反斜杠与单引号。

    单引号字符串中 $ 与换行都是字面量，不可能引入插值或可执行内容。
    """
    raw = value.encode("utf-8")
    raw = raw.replace(b"\\", b"\\\\").replace(b"'", b"\\'")
    return b"'" + raw + b"'"


def _encode_new_value(name: TargetName, value: TargetValue) -> bytes:
    if name is TargetName.WP_DEBUG:
        return b"true" if value else b"false"
    return _encode_string_literal(value)  # type: ignore[arg-type]


def _validate_targets(targets: dict[TargetName, TargetValue]) -> None:
    for name, value in targets.items():
        if not isinstance(name, TargetName):
            raise TypeError(f"unknown target: {name!r}")
        expected = _EXPECTED_TYPES[name]
        # bool 是 int 的子类，但 WP_DEBUG 只接受 bool，其余只接受 str
        if type(value) is not expected:
            raise TypeError(
                f"{name.value} expects {expected.__name__}, got {type(value).__name__}"
            )


def _collect(config: bytes, targets: dict[TargetName, TargetValue]) -> tuple[CheckResult, dict[TargetName, SearchOutcome]]:
    tokens = _significant(tokenize(config))
    reports: dict[TargetName, TargetReport] = {}
    outcomes: dict[TargetName, SearchOutcome] = {}
    for name in targets:
        if name is TargetName.TABLE_PREFIX:
            outcome = find_table_prefix_sites(tokens)
        else:
            outcome = find_define_sites(tokens, name.value)
        outcomes[name] = outcome
        issues = [Issue(code, code.name) for code in outcome.errors]
        if not outcome.sites and not issues:
            issues.append(Issue(IssueCode.MISSING, "target is not set"))
        if len(outcome.sites) > 1:
            issues.append(Issue(IssueCode.DUPLICATE, "target is set more than once"))
        reports[name] = TargetReport(name, ok=not issues, issues=issues)
    return CheckResult(ok=all(r.ok for r in reports.values()), targets=reports), outcomes


def check(config: bytes, targets: dict[TargetName, TargetValue]) -> CheckResult:
    """检查指定目标能否安全修改（不改动任何内容）。"""
    if not isinstance(config, bytes):
        raise TypeError("config must be bytes")
    _validate_targets(targets)
    result, _ = _collect(config, targets)
    return result


def rewrite(config: bytes, targets: dict[TargetName, TargetValue]) -> bytes:
    """生成改写结果；任一指定目标不安全时抛 RewriteError，不返回部分结果。"""
    if not isinstance(config, bytes):
        raise TypeError("config must be bytes")
    _validate_targets(targets)
    result, outcomes = _collect(config, targets)
    if not result.ok:
        raise RewriteError(result)
    # 从后往前替换，保持偏移有效；只替换值字面量，其余字节原样保留
    edits: list[tuple[int, int, bytes]] = []
    for name, value in targets.items():
        site = outcomes[name].sites[0]
        edits.append((site.value_start, site.value_end, _encode_new_value(name, value)))
    edits.sort(key=lambda e: e[0], reverse=True)
    out = config
    for start, end, replacement in edits:
        out = out[:start] + replacement + out[end:]
    return out
