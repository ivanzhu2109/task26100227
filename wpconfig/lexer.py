"""PHP source lexer: splits source into tokens, honouring text outside
<?php segments, the three PHP comment styles, and single/double-quoted,
heredoc and nowdoc strings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Token:
    kind: str          # 'comment' | 'ident' | 'variable' | 'number' | 'string' | 'punct'
    start: int         # char offset in the decoded source
    end: int
    # String-literal metadata (kind == 'string' only):
    quote: str = ""    # "'", '"', 'heredoc' or 'nowdoc'
    value: str = ""    # decoded value
    has_escape: bool = False
    has_interpolation: bool = False


def _is_label_char(ch: str) -> bool:
    return ch == "_" or ch.isalnum() or ord(ch) >= 0x80


_OPEN_RE = re.compile(r"<\?php(?![A-Za-z0-9_\x80-\xff])", re.IGNORECASE)

_SQ_DECODE_RE = re.compile(r"\\(['\\])")

_DQ_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "v": "\v",
    "e": "\x1b", "f": "\f", "\\": "\\", '"': '"', "$": "$",
}

_HEREDOC_RE = re.compile(
    r"<<<[ \t]*(?:([A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)"
    r"|\"([^\"\r\n]+)\"|'([^'\r\n]+)')"
)

_MASTER = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<close>\?>)
    | (?P<lcomment>//[^\n]*|\#[^\n]*)
    | (?P<bcomment>/\*.*?\*/)
    | (?P<squote>'(?:\\.|[^'\\])*'?)
    | (?P<dquote>"(?:\\.|[^"\\])*"?)
    | (?P<heredoc><<<)
    | (?P<variable>\$[A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)
    | (?P<ident>[A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)
    | (?P<number>[0-9][0-9A-Za-z_.]*)
    | (?P<op>===|!==|\*\*=|<<=|>>=|\?\?=
          |==|!=|<>|<=|>=|->|::|=>|&&|\|\|
          |\+=|-=|\.=|\*=|/=|%=|\*\*|<<|>>|\?\?|\+\+|--|&=|\|=|\^=)
    | (?P<punct>.)
    """,
    re.VERBOSE | re.DOTALL,
)


def _decode_double_quoted(body: str) -> Tuple[str, bool, bool]:
    """Best-effort decode of a double-quoted/heredoc body.

    Returns (value, has_escape, has_interpolation).
    """
    out: List[str] = []
    has_escape = False
    has_interp = False
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            has_escape = True
            nxt = body[i + 1] if i + 1 < n else ""
            if nxt in _DQ_ESCAPES:
                out.append(_DQ_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "x":
                m = re.match(r"[0-9A-Fa-f]{1,2}", body[i + 2:])
                if m:
                    out.append(chr(int(m.group(0), 16)))
                    i += 2 + len(m.group(0))
                    continue
            if nxt == "u" and i + 2 < n and body[i + 2] == "{":
                close = body.find("}", i + 3)
                if close > 0:
                    try:
                        out.append(chr(int(body[i + 3:close], 16)))
                        i = close + 1
                        continue
                    except ValueError:
                        pass
            if nxt and nxt in "01234567":
                m = re.match(r"[0-7]{1,3}", body[i + 1:])
                out.append(chr(int(m.group(0), 8) & 0xFF))
                i += 1 + len(m.group(0))
                continue
            out.append("\\")
            if nxt:
                out.append(nxt)
                i += 2
            else:
                i += 1
            continue
        if c == "$" and i + 1 < n and (
            body[i + 1] == "_" or body[i + 1].isalpha()
            or ord(body[i + 1]) >= 0x80
        ):
            has_interp = True
        if c == "{" and i + 1 < n and body[i + 1] == "$":
            has_interp = True
        out.append(c)
        i += 1
    return "".join(out), has_escape, has_interp


def _lex_heredoc(src: str, i: int) -> Optional[Tuple[Token, int]]:
    n = len(src)
    m = _HEREDOC_RE.match(src, i)
    if not m:
        return None
    nowdoc = m.group(3) is not None
    label = m.group(1) or m.group(2) or m.group(3) or ""
    nl = src.find("\n", m.end())
    if nl < 0:
        return (
            Token("string", i, n, quote="nowdoc" if nowdoc else "heredoc"),
            n,
        )
    pos = nl + 1
    lines: List[str] = []
    while pos <= n:
        line_end = src.find("\n", pos)
        if line_end < 0:
            line_end = n
        line = src[pos:line_end]
        stripped = line.lstrip(" \t")
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith(label) and (
            len(stripped) == len(label)
            or not _is_label_char(stripped[len(label)])
        ):
            close_at = pos + len(indent) + len(label)
            if indent:
                lines = [
                    ln[len(indent):] if ln.startswith(indent) else ln
                    for ln in lines
                ]
            body = "\n".join(lines)
            if nowdoc:
                value, has_escape, has_interp = body, False, False
            else:
                value, has_escape, has_interp = _decode_double_quoted(body)
            return (
                Token("string", i, close_at,
                      quote="nowdoc" if nowdoc else "heredoc",
                      value=value, has_escape=has_escape,
                      has_interpolation=has_interp),
                close_at,
            )
        if line_end == n:
            break
        lines.append(line)
        pos = line_end + 1
    return (
        Token("string", i, n, quote="nowdoc" if nowdoc else "heredoc"),
        n,
    )


def tokenize(source: str) -> List[Token]:
    tokens: List[Token] = []
    n = len(source)
    pos = 0
    in_php = False
    match = _MASTER.match
    while pos < n:
        if not in_php:
            m = _OPEN_RE.search(source, pos)
            if not m:
                break
            pos = m.end()
            in_php = True
            continue
        m = match(source, pos)
        kind = m.lastgroup
        start = pos
        if kind == "ws":
            pos = m.end()
            continue
        if kind == "close":
            in_php = False
            pos = m.end()
            continue
        if kind == "lcomment":
            text = m.group()
            q = text.find("?>")
            if q >= 0:
                tokens.append(Token("comment", start, start + q))
                pos = start + q
            else:
                tokens.append(Token("comment", start, m.end()))
                pos = m.end()
            continue
        if kind == "bcomment":
            tokens.append(Token("comment", start, m.end()))
            pos = m.end()
            continue
        if kind == "squote":
            raw = m.group()
            body = raw[1:-1] if len(raw) > 1 and raw[-1] == "'" else raw[1:]
            tokens.append(Token(
                "string", start, m.end(), quote="'",
                value=_SQ_DECODE_RE.sub(r"\1", body),
                has_escape="\\" in body,
            ))
            pos = m.end()
            continue
        if kind == "dquote":
            raw = m.group()
            body = raw[1:-1] if len(raw) > 1 and raw[-1] == '"' else raw[1:]
            value, has_escape, has_interp = _decode_double_quoted(body)
            tokens.append(Token(
                "string", start, m.end(), quote='"', value=value,
                has_escape=has_escape, has_interpolation=has_interp,
            ))
            pos = m.end()
            continue
        if kind == "heredoc":
            res = _lex_heredoc(source, start)
            if res is None:
                tokens.append(Token("punct", start, start + 2))
                pos = start + 2
            else:
                tok, pos = res
                tokens.append(tok)
            continue
        if kind in ("variable", "ident", "number"):
            tokens.append(Token(kind, start, m.end()))
            pos = m.end()
            continue
        tokens.append(Token("punct", start, m.end()))
        pos = m.end()
    return tokens
