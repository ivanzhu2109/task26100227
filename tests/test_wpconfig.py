"""wpconfig 库的验收测试。"""

from __future__ import annotations

import unittest

from wpconfig import (
    CheckResult,
    IssueCode,
    RewriteError,
    TargetName,
    check,
    rewrite,
)


def decode_php_single_quoted(literal: bytes) -> bytes:
    """独立解码器：把库产出的单引号字符串字面量还原为原始字节。"""
    assert literal.startswith(b"'") and literal.endswith(b"'")
    inner = literal[1:-1]
    out = bytearray()
    i = 0
    while i < len(inner):
        if inner[i:i + 1] == b"\\" and i + 1 < len(inner):
            assert inner[i + 1:i + 2] in (b"'", b"\\"), "不应出现其他转义"
            out.append(inner[i + 1])
            i += 2
        else:
            assert inner[i:i + 1] not in (b"'",), "未转义的引号会截断字符串"
            out.append(inner[i])
            i += 1
    return bytes(out)


def extract_literal_after(config: bytes, marker: bytes) -> bytes:
    """取出 marker 之后、下一个分号之前的字面量（测试辅助）。"""
    start = config.index(marker) + len(marker)
    end = config.index(b";", start)
    return config[start:end].strip()


class FakeTargetIsolationTest(unittest.TestCase):
    """注释、字符串、heredoc/nowdoc、PHP 外文本中的伪目标不得被匹配。"""

    SRC = (
        b"\xef\xbb\xbf"  # BOM
        b"<?php\r\n"
        b"// define('WP_DEBUG', true); fake in comment\n"
        b"# $table_prefix = 'fake_';\r\n"
        b"/* define(\"WP_MEMORY_LIMIT\", '999M'); */\n"
        b"$doc = <<<EOT\n"
        b"  define('WP_DEBUG', true);\n"
        b"  $table_prefix = 'hd_';\n"
        b"    EOT;\n"
        b"$now = <<<'NOW'\n"
        b"  define('WP_MEMORY_LIMIT', '1G');\n"
        b"\tNOW;\n"
        b"$s = 'define(\\'WP_DEBUG\\', true);';\n"
        b"$d = \"\\$table_prefix = 'fake_';\";\n"
        b"define('WP_DEBUG', false);\n"
        b"define('WP_MEMORY_LIMIT', '64M');\n"
        b"$table_prefix = 'wp_';\n"
        b"?>\n"
        b"<?php // footer ?> define('WP_DEBUG', true); fake in plain text\n"
    )

    def test_only_real_targets_change(self):
        targets = {
            TargetName.WP_DEBUG: True,
            TargetName.WP_MEMORY_LIMIT: "256M",
            TargetName.TABLE_PREFIX: "ab_",
        }
        result = check(self.SRC, targets)
        self.assertTrue(result.ok, result)
        out = rewrite(self.SRC, targets)
        # 逐字节对比：只有三处值字面量变化
        expected = (
            self.SRC
            .replace(b"define('WP_DEBUG', false);", b"define('WP_DEBUG', true);")
            .replace(b"'WP_MEMORY_LIMIT', '64M'", b"'WP_MEMORY_LIMIT', '256M'")
            .replace(b"$table_prefix = 'wp_';", b"$table_prefix = 'ab_';")
        )
        self.assertEqual(out, expected)
        # 伪目标原样保留
        self.assertIn(b"// define('WP_DEBUG', true);", out)
        self.assertIn(b"define('WP_DEBUG', true);\n  $table_prefix = 'hd_';", out)
        self.assertTrue(out.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", out)

    def test_idempotent(self):
        targets = {TargetName.WP_DEBUG: True, TargetName.WP_MEMORY_LIMIT: "256M",
                   TargetName.TABLE_PREFIX: "ab_"}
        once = rewrite(self.SRC, targets)
        self.assertEqual(rewrite(once, targets), once)


class NewValueEncodingTest(unittest.TestCase):
    """新值含引号、反斜杠、美元符号与换行时，解码后必须与请求一致。"""

    SRC = b"<?php\ndefine('WP_MEMORY_LIMIT', '64M');\n$table_prefix = 'wp_';\n"

    def test_tricky_string_roundtrip(self):
        tricky = "a'b\\c$d\n{$e}\r\n\"f\" 中文 \x00end"
        out = rewrite(self.SRC, {TargetName.WP_MEMORY_LIMIT: tricky})
        raw = extract_literal_after(out, b"'WP_MEMORY_LIMIT', ")
        self.assertTrue(raw.endswith(b")"))  # define 调用的右括号
        literal = raw[:-1]
        # 产出为单引号字符串：PHP 中单引号内无插值，内容即字面量
        self.assertTrue(literal.startswith(b"'") and literal.endswith(b"'"))
        decoded = decode_php_single_quoted(literal)
        self.assertEqual(decoded.decode("utf-8"), tricky)

    def test_table_prefix_roundtrip(self):
        value = "w'p\\$x\n_"
        out = rewrite(self.SRC, {TargetName.TABLE_PREFIX: value})
        literal = extract_literal_after(out, b"$table_prefix = ")
        self.assertEqual(decode_php_single_quoted(literal).decode("utf-8"), value)

    def test_bool_encoding(self):
        src = b"<?php\ndefine('WP_DEBUG', false);\n"
        self.assertIn(b"define('WP_DEBUG', true);",
                      rewrite(src, {TargetName.WP_DEBUG: True}))
        src2 = b"<?php\ndefine('WP_DEBUG', (true));\n"
        self.assertIn(b"define('WP_DEBUG', (false));",
                      rewrite(src2, {TargetName.WP_DEBUG: False}))


class RejectionTest(unittest.TestCase):
    """不安全情形必须整体拒绝，不返回部分结果。"""

    def test_duplicate_in_mutually_exclusive_branches(self):
        src = (
            b"<?php\n"
            b"if (getenv('APP_ENV') === 'prod') {\n"
            b"    define('WP_DEBUG', false);\n"
            b"} else {\n"
            b"    define('WP_DEBUG', true);\n"
            b"}\n"
        )
        result = check(src, {TargetName.WP_DEBUG: True})
        self.assertFalse(result.ok)
        codes = [i.code for i in result.targets[TargetName.WP_DEBUG].issues]
        self.assertIn(IssueCode.DUPLICATE, codes)
        with self.assertRaises(RewriteError):
            rewrite(src, {TargetName.WP_DEBUG: True})

    def test_partial_request_rejected_atomically(self):
        # WP_MEMORY_LIMIT 可改，但 WP_DEBUG 旧值是函数调用 → 整次拒绝
        src = (
            b"<?php\n"
            b"define('WP_DEBUG', app_debug_enabled());\n"
            b"define('WP_MEMORY_LIMIT', '64M');\n"
        )
        targets = {TargetName.WP_DEBUG: False, TargetName.WP_MEMORY_LIMIT: "128M"}
        result = check(src, targets)
        self.assertFalse(result.ok)
        self.assertTrue(result.targets[TargetName.WP_MEMORY_LIMIT].ok)
        debug_issues = result.targets[TargetName.WP_DEBUG].issues
        self.assertIn(IssueCode.UNSAFE_OLD_VALUE, [i.code for i in debug_issues])
        with self.assertRaises(RewriteError):
            rewrite(src, targets)

    def test_missing_and_bad_arity(self):
        src = b"<?php\ndefine('WP_MEMORY_LIMIT', '64M', true);\n"
        result = check(src, {TargetName.WP_DEBUG: True,
                             TargetName.WP_MEMORY_LIMIT: "128M"})
        self.assertFalse(result.ok)
        self.assertIn(IssueCode.MISSING,
                      [i.code for i in result.targets[TargetName.WP_DEBUG].issues])
        self.assertIn(IssueCode.BAD_ARITY,
                      [i.code for i in result.targets[TargetName.WP_MEMORY_LIMIT].issues])
        with self.assertRaises(RewriteError):
            rewrite(src, {TargetName.WP_MEMORY_LIMIT: "128M"})

    def test_interpolated_old_value_rejected(self):
        src = b'<?php\n$env = "prod";\ndefine("WP_MEMORY_LIMIT", "64M-$env");\n'
        result = check(src, {TargetName.WP_MEMORY_LIMIT: "128M"})
        self.assertFalse(result.ok)
        with self.assertRaises(RewriteError):
            rewrite(src, {TargetName.WP_MEMORY_LIMIT: "128M"})

    def test_unspecified_duplicate_does_not_block(self):
        # 未指定目标存在重复定义，不影响其余目标的安全修改
        src = (
            b"<?php\n"
            b"define('WP_DEBUG', false);\n"
            b"define('WP_DEBUG', true);\n"
            b"define('WP_MEMORY_LIMIT', '64M');\n"
            b"$table_prefix = 'wp_';\n"
        )
        targets = {TargetName.WP_MEMORY_LIMIT: "128M", TargetName.TABLE_PREFIX: "zz_"}
        result = check(src, targets)
        self.assertTrue(result.ok, result)
        out = rewrite(src, targets)
        self.assertEqual(
            out,
            src.replace(b"'WP_MEMORY_LIMIT', '64M'", b"'WP_MEMORY_LIMIT', '128M'")
               .replace(b"$table_prefix = 'wp_';", b"$table_prefix = 'zz_';"),
        )


class DefineFormTest(unittest.TestCase):
    """define 的各种合法与排除形式。"""

    def test_leading_backslash_and_case_insensitive(self):
        src = (
            b"<?php\n"
            b"\\DEFINE(\"WP_DEBUG\", false);\n"
            b"Define('WP_MEMORY_LIMIT', '64M',);\n"  # 末尾逗号
        )
        targets = {TargetName.WP_DEBUG: True, TargetName.WP_MEMORY_LIMIT: "128M"}
        self.assertTrue(check(src, targets).ok)
        out = rewrite(src, targets)
        self.assertIn(b'\\DEFINE("WP_DEBUG", true);', out)
        self.assertIn(b"Define('WP_MEMORY_LIMIT', '128M',);", out)

    def test_method_and_namespaced_calls_ignored(self):
        src = (
            b"<?php\n"
            b"$obj->define('WP_DEBUG', true);\n"
            b"Cls::define('WP_DEBUG', true);\n"
            b"Foo\\define('WP_DEBUG', true);\n"
            b"define('WP_DEBUG', false);\n"
        )
        result = check(src, {TargetName.WP_DEBUG: True})
        self.assertTrue(result.ok, result)
        out = rewrite(src, {TargetName.WP_DEBUG: True})
        self.assertEqual(out, src.replace(b"define('WP_DEBUG', false);",
                                          b"define('WP_DEBUG', true);"))

    def test_escaped_name_not_a_target(self):
        # 名称含转义序列的字符串不算目标设置
        src = b"<?php\ndefine('WP_\\x44EBUG', false);\n"
        result = check(src, {TargetName.WP_DEBUG: True})
        self.assertFalse(result.ok)
        self.assertIn(IssueCode.MISSING,
                      [i.code for i in result.targets[TargetName.WP_DEBUG].issues])

    def test_old_value_with_parens(self):
        src = b"<?php\ndefine('WP_MEMORY_LIMIT', ((('64M'))));\n"
        out = rewrite(src, {TargetName.WP_MEMORY_LIMIT: "128M"})
        self.assertEqual(out, b"<?php\ndefine('WP_MEMORY_LIMIT', ((('128M'))));\n")

    def test_table_prefix_double_quoted_old_value(self):
        src = b'<?php\n$table_prefix = "wp_";\n'
        out = rewrite(src, {TargetName.TABLE_PREFIX: "ab_"})
        self.assertEqual(out, b"<?php\n$table_prefix = 'ab_';\n")


class InputValidationTest(unittest.TestCase):
    def test_type_validation(self):
        src = b"<?php\ndefine('WP_DEBUG', false);\n"
        with self.assertRaises(TypeError):
            check(src, {TargetName.WP_DEBUG: "yes"})  # type: ignore[dict-item]
        with self.assertRaises(TypeError):
            rewrite(src, {TargetName.WP_MEMORY_LIMIT: 64})  # type: ignore[dict-item]
        with self.assertRaises(TypeError):
            check("not bytes", {TargetName.WP_DEBUG: True})  # type: ignore[arg-type]


class HeredocEdgeTest(unittest.TestCase):
    def test_empty_heredoc_then_real_target(self):
        src = b"<?php\n$h = <<<EOT\nEOT;\ndefine('WP_DEBUG', false);\n"
        out = rewrite(src, {TargetName.WP_DEBUG: True})
        self.assertEqual(out, src.replace(b"false", b"true"))

    def test_heredoc_end_at_eof_without_newline(self):
        src = b"<?php\n$h = <<<EOT\nbody\nEOT"
        # 无目标可改：检查缺失而非崩溃
        result = check(src, {TargetName.WP_DEBUG: True})
        self.assertFalse(result.ok)


class PerformanceTest(unittest.TestCase):
    def test_large_config_within_budget(self):
        import time
        body = []
        body.append(b"<?php\n")
        # 64 层嵌套条件块
        for i in range(64):
            body.append(b"if (cond_%d) {\n" % i)
        body.append(b"define('WP_DEBUG', false);\n")
        body.append(b"define('WP_MEMORY_LIMIT', '64M');\n")
        body.append(b"$table_prefix = 'wp_';\n")
        for i in range(64):
            body.append(b"}\n")
        # 填充注释与字符串到 2 MiB
        filler = (b"$s = 'define(\'WP_DEBUG\', true); " + b"x" * 60 + b"';\n"
                  b"// $table_prefix = 'fake_'; " + b"y" * 60 + b"\n")
        chunk = filler * (2 * 1024 * 1024 // len(filler) + 1)
        src = b"".join(body) + chunk
        self.assertGreaterEqual(len(src), 2 * 1024 * 1024)
        targets = {TargetName.WP_DEBUG: True, TargetName.WP_MEMORY_LIMIT: "256M",
                   TargetName.TABLE_PREFIX: "ab_"}
        start = time.monotonic()
        out = rewrite(src, targets)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, f"took {elapsed:.2f}s")
        self.assertIn(b"define('WP_DEBUG', true);", out)


if __name__ == "__main__":
    unittest.main()
