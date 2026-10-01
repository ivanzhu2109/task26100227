"""wp-config.php 安全改写库。

只处理 WP_DEBUG、WP_MEMORY_LIMIT 与 $table_prefix；
输入输出均为原始字节，文件读写由调用方负责。
"""

from .api import check, rewrite
from .types import (
    CheckResult,
    Issue,
    IssueCode,
    RewriteError,
    TargetName,
    TargetReport,
    TargetValue,
)

__all__ = [
    "check",
    "rewrite",
    "CheckResult",
    "Issue",
    "IssueCode",
    "RewriteError",
    "TargetName",
    "TargetReport",
    "TargetValue",
]
