import pytest

from wpconfig import (
    Changes,
    UnsafeRewriteError,
    check,
    rewrite,
)


def decode_php_single_quoted(literal: str) -> str:
    """Independent decoder for the single-quoted literals we emit."""
    assert literal.startswith("'") and literal.endswith("'")
    body = literal[1:-1]
    out = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            nxt = body[i + 1]
            assert nxt in ("'", "\\"), f"unexpected escape \\{nxt}"
            out.append(nxt)
            i += 2
        else:
            assert c != "'"
            out.append(c)
            i += 1
    return "".join(out)


def extract_new_literal(original: bytes, result: bytes, old_literal: bytes) -> str:
    """The result must differ from the original only by one replaced span."""
    assert old_literal in original
    start = original.index(old_literal)
    end = start + len(old_literal)
    assert original[:start] == result[:start]
    assert original[end:] == result[start + (len(result) - len(original)) + (end - start):] \
        or original[end:] == result[len(result) - (len(original) - end):]
    tail = original[end:]
    new_end = len(result) - len(tail)
    assert result[new_end:] == tail
    return result[start:new_end].decode("utf-8")


SAMPLE = """﻿# frozen comment: define('WP_DEBUG', true);
<html>define('WP_DEBUG', true); $table_prefix = 'fake_';</html>
<?php
// define('WP_DEBUG', true);
/* $table_prefix = 'no_'; */
# define("WP_MEMORY_LIMIT", '8M');
define('WP_DEBUG', false);

\\define( 'WP_MEMORY_LIMIT', (( '64M' )) );

$heredoc = <<<EOT
    define('WP_DEBUG', true);
    $table_prefix = 'hd_';
    EOT;
$nowdoc = <<<'NOW'
	define('WP_MEMORY_LIMIT', '1G');
	NOW;
$sq = 'it\\'s a $table_prefix = \\'no_\\' trap';
$dq = "define('WP_DEBUG', true)";
$dq2 = "esc \\" quote";
$table_prefix = 'wp_';
?>
trailing define('WP_DEBUG', true);
"""


def test_rewrite_only_real_targets():
    original = SAMPLE.encode()
    result = rewrite(original, Changes(
        wp_debug=True,
        wp_memory_limit="256M",
        table_prefix="site_",
    ))
    expected = original.replace(
        b"define('WP_DEBUG', false)", b"define('WP_DEBUG', true)"
    ).replace(
        b"'64M'", b"'256M'"
    ).replace(
        b"$table_prefix = 'wp_';", b"$table_prefix = 'site_';"
    )
    assert result == expected
    # Pseudo targets inside comments / strings / text stay untouched.
    for fake in (b"define('WP_DEBUG', true);", b"$table_prefix = 'fake_';",
                 b"$table_prefix = 'no_';", b"$table_prefix = 'hd_';"):
        assert fake in result
    assert result.count(b"define('WP_DEBUG', true);") == 6  # 5 fakes + real
    assert b"'256M'" in result
    assert b"$table_prefix = 'site_';" in result
    # BOM and layout preserved.
    assert result.startswith(b"\xef\xbb\xbf# frozen comment")
    assert result.endswith(b"trailing define('WP_DEBUG', true);\n")


def test_rewrite_bool_and_parens_preserved():
    src = b"<?php\ndefine('WP_DEBUG', ((false)));\n"
    out = rewrite(src, Changes(wp_debug=True))
    assert out == b"<?php\ndefine('WP_DEBUG', ((true)));\n"


def test_new_string_with_special_chars_decodes_exactly():
    nasty = "qu'ote \\ back $dollar {$x} \n new\tline \r\n"
    src = b"<?php\n$table_prefix = 'wp_';\n"
    out = rewrite(src, Changes(table_prefix=nasty))
    literal = extract_new_literal(src, out, b"'wp_'")
    assert decode_php_single_quoted(literal) == nasty
    # Only quotes and backslashes are escaped; $ and newlines stay literal.
    assert literal.count("'") == 2 + nasty.count("'")


def test_memory_limit_special_chars():
    nasty = "128M'; drop /* $x"
    src = b"<?php\ndefine('WP_MEMORY_LIMIT', '64M');\n"
    out = rewrite(src, Changes(wp_memory_limit=nasty))
    literal = extract_new_literal(src, out, b"'64M'")
    assert decode_php_single_quoted(literal) == nasty


def test_idempotent():
    changes = Changes(wp_debug=True, wp_memory_limit="256M",
                      table_prefix="ab_")
    once = rewrite(SAMPLE.encode(), changes)
    twice = rewrite(once, changes)
    assert once == twice


def test_check_reports_ok():
    report = check(SAMPLE.encode(), Changes(wp_debug=True))
    assert report.ok
    assert report.for_target("WP_DEBUG").ok


def test_missing_target_rejected():
    src = b"<?php\n$x = 1;\n"
    report = check(src, Changes(wp_debug=True))
    assert not report.ok
    assert "no setting found" in report.for_target("WP_DEBUG").reason
    with pytest.raises(UnsafeRewriteError):
        rewrite(src, Changes(wp_debug=True))


def test_duplicate_in_mutually_exclusive_branches_rejected():
    src = b"""<?php
if (getenv('ENV') === 'dev') {
    define('WP_DEBUG', true);
} else {
    define('WP_DEBUG', false);
}
"""
    report = check(src, Changes(wp_debug=True))
    assert not report.ok
    assert "multiple" in report.for_target("WP_DEBUG").reason
    with pytest.raises(UnsafeRewriteError):
        rewrite(src, Changes(wp_debug=True))


def test_one_bad_target_blocks_whole_request():
    src = b"""<?php
define('WP_DEBUG', false);
define('WP_MEMORY_LIMIT', wp_get_memory_limit());
"""
    changes = Changes(wp_debug=True, wp_memory_limit="256M")
    report = check(src, changes)
    assert report.for_target("WP_DEBUG").ok
    assert not report.for_target("WP_MEMORY_LIMIT").ok
    with pytest.raises(UnsafeRewriteError):
        rewrite(src, changes)


def test_unspecified_duplicate_target_does_not_block():
    src = b"""<?php
define('WP_MEMORY_LIMIT', '64M');
define('WP_MEMORY_LIMIT', '128M');
if (a()) { $table_prefix = 'a_'; } else { $table_prefix = 'b_'; }
define('WP_DEBUG', false);
"""
    out = rewrite(src, Changes(wp_debug=True))
    assert out == src.replace(b"define('WP_DEBUG', false);",
                              b"define('WP_DEBUG', true);")


def test_wrong_arg_count_rejected():
    src = b"<?php\ndefine('WP_DEBUG', true, false);\n"
    assert not check(src, Changes(wp_debug=True)).ok
    src2 = b"<?php\ndefine('WP_DEBUG');\n"
    assert not check(src2, Changes(wp_debug=True)).ok


def test_trailing_comma_allowed():
    src = b"<?php\ndefine('WP_DEBUG', false,);\n"
    out = rewrite(src, Changes(wp_debug=True))
    assert out == b"<?php\ndefine('WP_DEBUG', true,);\n"


def test_method_and_qualified_calls_ignored():
    src = b"""<?php
$obj->define('WP_DEBUG', true);
Foo::define('WP_DEBUG', true);
Foo\\define('WP_DEBUG', true);
define('WP_DEBUG', false);
"""
    out = rewrite(src, Changes(wp_debug=True))
    assert out == src.replace(b"define('WP_DEBUG', false);",
                              b"define('WP_DEBUG', true);")


def test_interpolated_old_value_rejected():
    src = b'<?php\n$table_prefix = "wp_{$suffix}_";\n'
    assert not check(src, Changes(table_prefix="x_")).ok
    src2 = b'<?php\ndefine("WP_MEMORY_LIMIT", "64$unit");\n'
    assert not check(src2, Changes(wp_memory_limit="1G")).ok


def test_dynamic_name_not_matched():
    src = b"<?php\ndefine($name, true);\ndefine('WP_DEBUG', false);\n"
    out = rewrite(src, Changes(wp_debug=True))
    assert b"define($name, true);" in out


def test_mixed_line_endings_preserved():
    src = b"<?php\r\ndefine('WP_DEBUG', false);\r\n$table_prefix = 'wp_';\n"
    out = rewrite(src, Changes(wp_debug=True, table_prefix="zz_"))
    assert b"\r\n" in out and out.count(b"\r\n") == 2
    assert out == src.replace(b"false", b"true").replace(b"'wp_'", b"'zz_'")


def test_heredoc_old_value_rejected():
    src = b"""<?php
$table_prefix = <<<EOT
wp_
EOT;
"""
    assert not check(src, Changes(table_prefix="x_")).ok


def test_case_insensitive_function_name():
    src = b"<?php\nDEFINE('WP_DEBUG', false);\n"
    out = rewrite(src, Changes(wp_debug=True))
    assert out == b"<?php\nDEFINE('WP_DEBUG', true);\n"


def test_nothing_specified_returns_source():
    src = SAMPLE.encode()
    assert rewrite(src, Changes()) == src
    assert check(src, Changes()).ok
