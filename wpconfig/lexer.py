"""PHP 词法层：把字节流切成 token，识别注释、字符串与 PHP 片段边界。

只覆盖题设需要的 PHP 8.2 子集：
- 片段由 `<?php` 开启、`?>` 关闭（可省略），片段外为 TEXT；
- 三种注释：//、# 到行尾，/* ... */；
- 字符串：单/双引号、heredoc、nowdoc（结束标识允许缩进）；
- 标识符可带前导反斜杠；变量为 $name。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class TokenKind(str, Enum):
    PHP_OPEN = "php_open"        # <?php
    PHP_CLOSE = "php_close"      # ?>
    TEXT = "text"                # PHP 片段外的原始文本
    COMMENT = "comment"          # //、#、/* */ 三种注释
    STRING = "string"            # 单/双引号字符串、heredoc、nowdoc
    IDENT = "ident"              # 标识符（可带前导反斜杠）
    VARIABLE = "variable"        # $name
    SYMBOL = "symbol"            # 其他单字符符号


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    start: int          # 在原始字节中的起始偏移
    end: int            # 结束偏移（不含）
    value: bytes        # 原始字节切片
    quote: bytes = b""  # 仅 STRING：b"'" / b'"' / b"<<<"（heredoc）/ b"<<<'"（nowdoc）


# 字节类别表：避免逐字符调用 chr()/isalpha()
_CAT_SYMBOL = 0
_CAT_WS = 1
_CAT_IDENT = 2
_CAT_SQUOTE = 3
_CAT_DQUOTE = 4
_CAT_DOLLAR = 5
_CAT_HASH = 6
_CAT_SLASH = 7
_CAT_QUESTION = 8
_CAT_LT = 9
_CAT_BACKSLASH = 10

_CAT = bytearray(256)
for _c in b" \t\n\r\x0b\x0c":
    _CAT[_c] = _CAT_WS
for _c in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_":
    _CAT[_c] = _CAT_IDENT
for _c in range(0x80, 0x100):
    _CAT[_c] = _CAT_IDENT
_CAT[0x27] = _CAT_SQUOTE
_CAT[0x22] = _CAT_DQUOTE
_CAT[0x24] = _CAT_DOLLAR
_CAT[0x23] = _CAT_HASH
_CAT[0x2F] = _CAT_SLASH
_CAT[0x3F] = _CAT_QUESTION
_CAT[0x3C] = _CAT_LT
_CAT[0x5C] = _CAT_BACKSLASH

# 标识符字符表（含数字，用于首字符之后的扫描）
_IS_IDENT = bytearray(256)
for _c in b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789":
    _IS_IDENT[_c] = 1
for _c in range(0x80, 0x100):
    _IS_IDENT[_c] = 1

# 字符串扫描：转义序列或结束引号（DOTALL 让 \\. 覆盖反斜杠+换行）
_QUOTE_RES = {
    0x27: re.compile(rb"\\.|'", re.DOTALL),
    0x22: re.compile(rb'\\.|"', re.DOTALL),
}


def _scan_quoted(source: bytes, pos: int, quote: int) -> int:
    """从引号之后扫描到配对的结束引号，返回结束引号后一位。"""
    for m in _QUOTE_RES[quote].finditer(source, pos):
        if m.start() != -1 and source[m.start()] == quote:
            return m.end()
    return len(source)  # 输入保证合法 PHP，不会到这里


def _match_heredoc_end(source: bytes, line_start: int, label: bytes) -> int | None:
    """若 line_start 处（允许缩进）是结束标识行，返回标识结束后一位，否则 None。"""
    n = len(source)
    i = line_start
    while i < n and source[i] in (0x20, 0x09):  # 空格/制表符缩进
        i += 1
    if source[i:i + len(label)] != label:
        return None
    j = i + len(label)
    if j < n and _IS_IDENT[source[j]]:
        return None
    return j


def _scan_heredoc(source: bytes, pos: int, label: bytes) -> int:
    """从 heredoc 内容首行开始扫描到结束标识，返回整个 heredoc 的结束位置。"""
    n = len(source)
    line_start = pos
    while line_start < n:
        end = _match_heredoc_end(source, line_start, label)
        if end is not None:
            # 结束标识后可跟 ; , ) 等，heredoc token 到标识字符为止
            return end
        nl = source.find(b"\n", line_start)
        if nl == -1:
            return n
        line_start = nl + 1
    return n


def tokenize(source: bytes) -> list[Token]:
    """把源字节切成 token 序列。"""
    tokens: list[Token] = []
    append = tokens.append
    n = len(source)
    pos = 0
    in_php = False

    while pos < n:
        if not in_php:
            nxt = source.find(b"<?php", pos)
            if nxt == -1:
                if pos < n:
                    append(Token(TokenKind.TEXT, pos, n, source[pos:]))
                break
            if nxt > pos:
                append(Token(TokenKind.TEXT, pos, nxt, source[pos:nxt]))
            end = nxt + 5
            # `<?php` 后必须跟非标识符字符（合法 PHP 保证）
            append(Token(TokenKind.PHP_OPEN, nxt, end, source[nxt:end]))
            pos = end
            in_php = True
            continue

        ch = source[pos]
        cat = _CAT[ch]

        if cat == _CAT_WS:
            pos += 1
            continue
        if cat == _CAT_SYMBOL:
            append(Token(TokenKind.SYMBOL, pos, pos + 1, source[pos:pos + 1]))
            pos += 1
            continue
        if cat == _CAT_QUESTION:
            if source.startswith(b"?>", pos):
                append(Token(TokenKind.PHP_CLOSE, pos, pos + 2, b"?>"))
                pos += 2
                in_php = False
            else:
                append(Token(TokenKind.SYMBOL, pos, pos + 1, source[pos:pos + 1]))
                pos += 1
            continue
        if cat == _CAT_SLASH:
            if source.startswith(b"//", pos):
                nl = source.find(b"\n", pos)
                end = n if nl == -1 else nl
                close = source.find(b"?>", pos, end)
                if close != -1:
                    end = close
                append(Token(TokenKind.COMMENT, pos, end, source[pos:end]))
                pos = end
            elif source.startswith(b"/*", pos):
                close = source.find(b"*/", pos + 2)
                end = n if close == -1 else close + 2
                append(Token(TokenKind.COMMENT, pos, end, source[pos:end]))
                pos = end
            else:
                append(Token(TokenKind.SYMBOL, pos, pos + 1, source[pos:pos + 1]))
                pos += 1
            continue
        if cat == _CAT_HASH:
            nl = source.find(b"\n", pos)
            end = n if nl == -1 else nl
            close = source.find(b"?>", pos, end)
            if close != -1:
                end = close
            append(Token(TokenKind.COMMENT, pos, end, source[pos:end]))
            pos = end
            continue
        if cat == _CAT_SQUOTE or cat == _CAT_DQUOTE:
            quote = 0x27 if cat == _CAT_SQUOTE else 0x22
            end = _scan_quoted(source, pos + 1, quote)
            append(Token(TokenKind.STRING, pos, end, source[pos:end],
                         b"'" if quote == 0x27 else b'"'))
            pos = end
            continue
        if cat == _CAT_LT:
            if source.startswith(b"<<<", pos):
                j = pos + 3
                if j < n and source[j] == 0x27:  # nowdoc: <<<'LABEL'
                    close = source.find(b"'", j + 1)
                    label = source[j + 1:close]
                    line_end = source.find(b"\n", close)
                    body_start = n if line_end == -1 else line_end + 1
                    end = _scan_heredoc(source, body_start, label)
                    append(Token(TokenKind.STRING, pos, end, source[pos:end], b"<<<'"))
                    pos = end
                    continue
                if j < n and source[j] == 0x22:  # heredoc: <<<"LABEL"
                    close = source.find(b'"', j + 1)
                    label = source[j + 1:close]
                else:  # heredoc: <<<LABEL
                    k = j
                    while k < n and _IS_IDENT[source[k]]:
                        k += 1
                    label = source[j:k]
                line_end = source.find(b"\n", pos)
                body_start = n if line_end == -1 else line_end + 1
                end = _scan_heredoc(source, body_start, label)
                append(Token(TokenKind.STRING, pos, end, source[pos:end], b"<<<"))
                pos = end
            else:
                append(Token(TokenKind.SYMBOL, pos, pos + 1, source[pos:pos + 1]))
                pos += 1
            continue
        if cat == _CAT_DOLLAR:
            if pos + 1 < n and _CAT[source[pos + 1]] == _CAT_IDENT:
                j = pos + 2
                while j < n and _IS_IDENT[source[j]]:
                    j += 1
                append(Token(TokenKind.VARIABLE, pos, j, source[pos:j]))
                pos = j
            else:
                append(Token(TokenKind.SYMBOL, pos, pos + 1, source[pos:pos + 1]))
                pos += 1
            continue
        if cat == _CAT_BACKSLASH:
            if pos + 1 < n and _CAT[source[pos + 1]] == _CAT_IDENT:
                j = pos + 2
                while j < n and _IS_IDENT[source[j]]:
                    j += 1
                append(Token(TokenKind.IDENT, pos, j, source[pos:j]))
                pos = j
            else:
                append(Token(TokenKind.SYMBOL, pos, pos + 1, source[pos:pos + 1]))
                pos += 1
            continue
        # _CAT_IDENT
        j = pos + 1
        while j < n and _IS_IDENT[source[j]]:
            j += 1
        append(Token(TokenKind.IDENT, pos, j, source[pos:j]))
        pos = j

    return tokens
