"""公共类型定义：目标描述、检查结果与异常。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union


class TargetName(str, Enum):
    WP_DEBUG = "WP_DEBUG"
    WP_MEMORY_LIMIT = "WP_MEMORY_LIMIT"
    TABLE_PREFIX = "table_prefix"


# 请求中 targets 映射的值类型：WP_DEBUG 为 bool，其余为 str
TargetValue = Union[bool, str]


class IssueCode(str, Enum):
    MISSING = "missing"                # 目标未设置
    DUPLICATE = "duplicate"            # 目标被设置多次
    BAD_ARITY = "bad_arity"            # define 参数数量不符
    UNSAFE_OLD_VALUE = "unsafe_old_value"  # 旧值不是布尔字面量或普通字符串
    INVALID_NEW_VALUE = "invalid_new_value"  # 新值类型不符


@dataclass(frozen=True)
class Issue:
    code: IssueCode
    message: str


@dataclass
class TargetReport:
    """单个目标的检查结论。"""

    name: TargetName
    ok: bool
    issues: list[Issue] = field(default_factory=list)


@dataclass
class CheckResult:
    """整次检查结论：只有全部指定目标都可安全修改时 ok 为 True。"""

    ok: bool
    targets: dict[TargetName, TargetReport] = field(default_factory=dict)


class RewriteError(Exception):
    """改写被拒绝时抛出，携带完整检查结果。"""

    def __init__(self, result: CheckResult):
        self.result = result
        super().__init__("unsafe to rewrite: " + "; ".join(
            f"{t.name.value}: {', '.join(i.code.value for i in t.issues)}"
            for t in result.targets.values() if not t.ok
        ))
