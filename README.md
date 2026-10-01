# wpconfig — wp-config.php 安全改写库

纯内存、不执行 PHP、不依赖 PHP 运行环境的 `wp-config.php` 改写库。
输入输出均为原始字节；本地文件读写由调用方负责。

## 能力

- `check(source: bytes, changes: Changes) -> CheckReport`
  检查本次指定的目标能否安全修改，逐项给出 `TargetReport(ok, reason)`。
- `rewrite(source: bytes, changes: Changes) -> bytes`
  任一指定目标不安全时抛出 `UnsafeRewriteError`（不返回部分结果）；
  否则只替换目标值字面量，其余字节（BOM、换行风格、缩进、注释、
  包裹圆括号、其他配置）完全保留。相同变更重复应用逐字节不变。

## 支持的目标

| `Changes` 字段 | 目标 | 新值类型 |
| --- | --- | --- |
| `wp_debug` | `define('WP_DEBUG', ...)` | `bool` |
| `wp_memory_limit` | `define('WP_MEMORY_LIMIT', ...)` | `str` |
| `table_prefix` | `$table_prefix = ...` | `str` |

字段为 `None` 表示本次不指定；未指定目标的缺失/重复/异常不影响其余目标。

## 安全规则（不满足则整次拒绝）

- 每个被指定目标在全部 PHP 片段中恰有一处设置（条件块、函数体内也计数）。
- `define` 须恰两个参数（允许末尾逗号），名称是不含转义/插值的引号字符串；
  允许 `\define`，函数名不区分大小写；方法、静态方法、限定名调用不计入。
- `$table_prefix` 只认对该变量的直接 `=` 赋值。
- 旧值只接受布尔字面量或无插值的单/双引号字符串（可多层圆括号包裹）；
  其他表达式（函数调用、heredoc、插值字符串等）一律拒绝。
- 注释与字符串（含 heredoc/nowdoc）中的伪目标不会被匹配。

新字符串值编码为单引号字面量（仅转义 `'` 与 `\`），可安全包含引号、
反斜杠、美元符号与换行，解码后与请求值完全一致，不引入插值。

## 示例

```python
from wpconfig import Changes, UnsafeRewriteError, check, rewrite

source = open("wp-config.php", "rb").read()
changes = Changes(wp_debug=True, wp_memory_limit="256M", table_prefix="site_")

report = check(source, changes)
if report.ok:
    result = rewrite(source, changes)
    open("wp-config.php", "wb").write(result)
else:
    for r in report.results:
        if not r.ok:
            print(r.target, r.reason)
```

## 测试

```sh
python3 -m pytest tests/ -q
```
