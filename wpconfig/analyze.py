"""Locates settings of the supported targets inside the token stream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .lexer import Token
from .model import WP_DEBUG, WP_MEMORY_LIMIT, TABLE_PREFIX

CONSTANT_TARGETS = (WP_DEBUG, WP_MEMORY_LIMIT)
TARGETS = CONSTANT_TARGETS + (TABLE_PREFIX,)


@dataclass(frozen=True)
class Setting:
    target: str
    value_start: int   # char offset of the old value literal
    value_end: int
    valid: bool        # False when the occurrence itself is malformed
    reason: str = ""


def _text(source: str, tok: Token) -> str:
    return source[tok.start:tok.end]


def _is_bool_literal(source: str, tok: Token) -> bool:
    return tok.kind == "ident" and _text(source, tok).lower() in (
        "true",
        "false",
    )


def _strip_parens(
    source: str, toks: List[Token], span: Tuple[int, int]
) -> Optional[Tuple[int, int]]:
    """Strip balanced paren pairs wrapping the whole token range."""
    lo, hi = span
    while hi - lo >= 2:
        if _text(source, toks[lo]) != "(" or _text(source, toks[hi - 1]) != ")":
            return (lo, hi)
        depth = 0
        balanced_wrap = True
        for k in range(lo, hi):
            t = _text(source, toks[k])
            if t == "(":
                depth += 1
            elif t == ")":
                depth -= 1
                if depth == 0 and k != hi - 1:
                    balanced_wrap = False
                    break
        if not balanced_wrap or depth != 0:
            return (lo, hi)
        lo, hi = lo + 1, hi - 1
    return (lo, hi)


def _validate_value(
    source: str, toks: List[Token], span: Tuple[int, int]
) -> Optional[Token]:
    """The value must reduce to a single bool literal or a plain
    single/double-quoted string without interpolation."""
    stripped = _strip_parens(source, toks, span)
    if stripped is None:
        return None
    lo, hi = stripped
    if hi - lo != 1:
        return None
    tok = toks[lo]
    if _is_bool_literal(source, tok):
        return tok
    if (
        tok.kind == "string"
        and tok.quote in ("'", '"')
        and not tok.has_interpolation
    ):
        return tok
    return None


def _split_args(
    source: str, toks: List[Token], open_idx: int
) -> Optional[Tuple[List[Tuple[int, int]], int]]:
    """Given index of '(' return (arg token-index spans, index after ')')."""
    assert _text(source, toks[open_idx]) == "("
    depth = 1
    args: List[Tuple[int, int]] = []
    cur_start: Optional[int] = None
    i = open_idx + 1
    while i < len(toks):
        t = _text(source, toks[i])
        if t in ("(", "[", "{"):
            depth += 1
        elif t in (")", "]", "}"):
            depth -= 1
            if depth == 0:
                if cur_start is not None:
                    args.append((cur_start, i))
                return args, i + 1
        elif t == "," and depth == 1:
            args.append((cur_start, i) if cur_start is not None else (i, i))
            cur_start = None
            i += 1
            continue
        if depth >= 1 and cur_start is None and t != ",":
            cur_start = i
        i += 1
    return None


def _match_define(
    source: str, toks: List[Token], i: int
) -> Optional[Setting]:
    name_tok = toks[i]
    if name_tok.kind != "ident" or _text(source, name_tok).lower() != "define":
        return None
    # Exclude method/static/qualified calls and declarations.
    j = i - 1
    if j >= 0:
        prev = _text(source, toks[j])
        if prev in ("->", "::"):
            return None
        if prev == "function":
            return None
        if prev == "\\":
            if j - 1 >= 0 and (
                toks[j - 1].kind == "ident"
                or _text(source, toks[j - 1]) in ("->", "::")
            ):
                return None  # Foo\define or ->\define: not the global function
    if i + 1 >= len(toks) or _text(source, toks[i + 1]) != "(":
        return None
    split = _split_args(source, toks, i + 1)
    if split is None:
        return None
    args, _end = split
    # Tolerate a single trailing comma.
    if args and args[-1][0] == args[-1][1]:
        args = args[:-1]
    if not args:
        return None
    # First argument must be a plain quoted string naming a target.
    first = args[0]
    target = None
    if first[1] - first[0] == 1:
        t = toks[first[0]]
        if (
            t.kind == "string"
            and t.quote in ("'", '"')
            and not t.has_escape
            and not t.has_interpolation
            and t.value in CONSTANT_TARGETS
        ):
            target = t.value
    if target is None:
        return None  # not one of our constants (or dynamic name)
    if len(args) != 2:
        return Setting(target, name_tok.start, name_tok.end, False,
                       "define() must have exactly 2 arguments")
    value_tok = _validate_value(source, toks, args[1])
    if value_tok is None:
        return Setting(target, name_tok.start, name_tok.end, False,
                       "unsupported value expression")
    return Setting(target, value_tok.start, value_tok.end, True)


def _match_assignment(
    source: str, toks: List[Token], i: int
) -> Optional[Setting]:
    tok = toks[i]
    if tok.kind != "variable" or _text(source, tok) != "$" + TABLE_PREFIX:
        return None
    if i > 0 and _text(source, toks[i - 1]) in ("->", "::"):
        return None
    if i + 1 >= len(toks) or _text(source, toks[i + 1]) != "=":
        return None
    # Value expression runs to the next top-level ';'.
    depth = 0
    j = i + 2
    end = j
    while end < len(toks):
        t = _text(source, toks[end])
        if t in ("(", "[", "{"):
            depth += 1
        elif t in (")", "]", "}"):
            depth -= 1
        elif t == ";" and depth == 0:
            break
        end += 1
    value_tok = _validate_value(source, toks, (j, end))
    if value_tok is None:
        return Setting(TABLE_PREFIX, tok.start, tok.end, False,
                       "unsupported value expression")
    return Setting(TABLE_PREFIX, value_tok.start, value_tok.end, True)


def find_settings(source: str, tokens: List[Token]) -> List[Setting]:
    toks = [t for t in tokens if t.kind != "comment"]
    settings: List[Setting] = []
    for i in range(len(toks)):
        s = _match_define(source, toks, i)
        if s is not None:
            settings.append(s)
            continue
        s = _match_assignment(source, toks, i)
        if s is not None:
            settings.append(s)
    return settings
