# task26100227

`wpconfig`：WordPress `wp-config.php` 安全改写库（纯内存、纯 Python，无第三方依赖）。

## 能力

- `check(config: bytes, targets) -> CheckResult`：检查指定目标能否安全修改。
- `rewrite(config: bytes, targets) -> bytes`：生成改写结果；任一指定目标不安全时抛
  `RewriteError`（携带完整 `CheckResult`），不返回部分结果。

`targets` 的键为 `TargetName` 枚举，值的类型：

| 目标 | 键 | 新值类型 |
| --- | --- | --- |
| `WP_DEBUG` | `TargetName.WP_DEBUG` | `bool` |
| `WP_MEMORY_LIMIT` | `TargetName.WP_MEMORY_LIMIT` | `str` |
| `$table_prefix` | `TargetName.TABLE_PREFIX` | `str` |

每次可指定其中若干项；未指定目标的状态（缺失、重复等）不影响本次操作。

## 规则摘要

- 常量设置是首参数为目标名的全局 `define` 调用（允许前导反斜杠、函数名不区分大小写）；
  对象方法、静态方法、其他命名空间的同名调用不计入。`$table_prefix` 只认直接等号赋值。
- 被指定的每个目标在全部 PHP 片段中必须恰有一处设置（条件块、函数体内也计数）；
  `define` 须恰有两个参数（允许末尾逗号）；旧值只接受布尔字面量或无插值的单/双引号
  字符串（可被多层圆括号包裹）。缺失、重复、参数数量不符或旧值为其他表达式时整次拒绝。
- 成功时只替换值字面量，其余字节（包裹圆括号、BOM、混合换行、缩进、注释、其他配置）
  完全保留；相同变更再次作用于结果时逐字节不变。
- 新字符串编码为单引号 PHP 字符串（只转义 `\` 与 `'`），不会引入插值或可执行内容。

## 用法

```python
from wpconfig import check, rewrite, TargetName, RewriteError

config = open("wp-config.php", "rb").read()  # 文件读写由调用方负责
targets = {TargetName.WP_DEBUG: False, TargetName.WP_MEMORY_LIMIT: "256M"}

result = check(config, targets)
if result.ok:
    new_config = rewrite(config, targets)
else:
    for name, report in result.targets.items():
        for issue in report.issues:
            print(name.value, issue.code.value)
```

## 测试

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
```
