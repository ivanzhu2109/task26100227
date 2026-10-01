"""分析层：在 token 流中定位目标设置点并解析旧值。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from .lexer import Token, TokenKind
from .types import IssueCode


@dataclass(frozen=True)
class SettingSite:
    """一处目标设置：值字面量在源字节中的范围及解码后的旧值。"""

    value_start: int   # 值字面量起始偏移（不含包裹圆括号）
    value_end: int     # 值字面量结束偏移（不含）
    old_value: Union[bool, str]


@dataclass
class SearchOutcome:
    """某目标的搜索结果：合法设置点与发现的问题。"""

    sites: list[SettingSite] = field(default_factory=list)
    errors: list[IssueCode] = field(default_factory=list)


def _is_ident_start(ch: int) -> bool:
    return ch >= 0x80 or chr(ch).isalpha() or ch == 0x5F


def _decode_single_quoted(inner: bytes) -> bytes:
    """解码单引号字符串内容：仅 \\' 与 \\\\ 是转义。"""
    out = bytearray()
    i = 0
    while i < len(inner):
        if inner[i] == 0x5C and i + 1 < len(inner) and inner[i + 1] in (0x27, 0x5C):
            out.append(inner[i + 1])
            i += 2
        else:
            out.append(inner[i])
            i += 1
    return bytes(out)


def _decode_double_quoted(inner: bytes) -> bytes | None:
    """解码双引号字符串内容；含 $name 或 {$name} 插值时返回 None。"""
    out = bytearray()
    i = 0
    n = len(inner)
    simple = {0x6E: 0x0A, 0x74: 0x09, 0x72: 0x0D, 0x76: 0x0B,
              0x65: 0x1B, 0x66: 0x0C}  # \n \t \r \v \e \f
    while i < n:
        ch = inner[i]
        if ch == 0x5C and i + 1 < n:  # 反斜杠
            nxt = inner[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
            elif nxt == 0x78:  # \xHH
                j = i + 2
                hexs = b""
                while j < n and len(hexs) < 2 and chr(inner[j]) in "0123456789abcdefABCDEF":
                    hexs += bytes([inner[j]])
                    j += 1
                out.append(int(hexs, 16) if hexs else 0x78)
                i = j
            elif nxt == 0x75 and i + 2 < n and inner[i + 2] == 0x7B:  # \u{...}
                close = inner.find(b"}", i + 3)
                out.extend(chr(int(inner[i + 3:close], 16)).encode("utf-8"))
                i = close + 1
            elif 0x30 <= nxt <= 0x37:  # 八进制
                j = i + 1
                octs = b""
                while j < n and len(octs) < 3 and 0x30 <= inner[j] <= 0x37:
                    octs += bytes([inner[j]])
                    j += 1
                out.append(int(octs, 8) & 0xFF)
                i = j
            elif nxt in (0x5C, 0x22, 0x24):  # \\ \" \$
                out.append(nxt)
                i += 2
            else:  # 其他反斜杠序列原样保留
                out.append(ch)
                i += 1
        elif ch == 0x24 and i + 1 < n and _is_ident_start(inner[i + 1]):  # $name
            return None
        elif ch == 0x7B and i + 1 < n and inner[i + 1] == 0x24:  # {$name}
            return None
        else:
            out.append(ch)
            i += 1
    return bytes(out)


def _parse_simple_value(arg: list[Token]) -> tuple[Token, Union[bool, str]] | None:
    """把一段参数 token 解析为 (字面量 token, 解码值)；失败返回 None。

    只接受：布尔字面量或无插值的单/双引号字符串，可被多层圆括号包裹。
    """
    toks = [t for t in arg if t.kind != TokenKind.COMMENT]
    # 剥离成对的包裹圆括号
    while len(toks) >= 2 and toks[0].value == b"(" and toks[-1].value == b")":
        depth = 0
        balanced = True
        for idx, t in enumerate(toks):
            if t.value == b"(":
                depth += 1
            elif t.value == b")":
                depth -= 1
                if depth == 0 and idx != len(toks) - 1:
                    balanced = False
                    break
        if not balanced or depth != 0:
            break
        toks = toks[1:-1]
    if len(toks) != 1:
        return None
    tok = toks[0]
    if tok.kind == TokenKind.IDENT and tok.value.lower() in (b"true", b"false"):
        return tok, tok.value.lower() == b"true"
    if tok.kind == TokenKind.STRING and tok.quote == b"'":
        inner = tok.value[1:-1]
        return tok, _decode_single_quoted(inner).decode("utf-8")
    if tok.kind == TokenKind.STRING and tok.quote == b'"':
        decoded = _decode_double_quoted(tok.value[1:-1])
        if decoded is None:
            return None
        return tok, decoded.decode("utf-8")
    return None


def _significant(tokens: list[Token]) -> list[Token]:
    return [t for t in tokens if t.kind not in (TokenKind.COMMENT, TokenKind.TEXT,
                                                TokenKind.PHP_OPEN, TokenKind.PHP_CLOSE)]


def _split_args(tokens: list[Token]) -> list[list[Token]] | None:
    """把括号内的 token 按顶层逗号切成参数；允许末尾逗号。结构不合法返回 None。"""
    args: list[list[Token]] = [[]]
    depth = 0
    for t in tokens:
        if t.value in (b"(", b"[", b"{"):
            depth += 1
        elif t.value in (b")", b"]", b"}"):
            depth -= 1
            if depth < 0:
                return None
        if depth == 0 and t.value == b",":
            args.append([])
        else:
            args[-1].append(t)
    if depth != 0:
        return None
    # 允许末尾逗号：去掉最后一个空参数
    if len(args) > 1 and not _significant(args[-1]):
        args.pop()
    return args


def find_define_sites(tokens: list[Token], name: str) -> SearchOutcome:
    """查找对指定常量的全局 define 设置点。tokens 须已经 _significant 过滤。"""
    outcome = SearchOutcome()
    sig = tokens
    name_bytes = name.encode()
    for idx, tok in enumerate(sig):
        if tok.kind != TokenKind.IDENT:
            continue
        bare = tok.value.lstrip(b"\\")
        if bare.lower() != b"define":
            continue
        # 带前导反斜杠时，若前面紧跟命名空间片段（如 Foo\define）则不是全局调用
        if tok.value.startswith(b"\\") and idx > 0 and sig[idx - 1].kind == TokenKind.IDENT:
            continue
        # 排除对象方法 ->define 与静态方法 ::define
        if idx > 0 and sig[idx - 1].value in (b">", b":"):
            continue
        # 下一有效 token 必须是 (
        if idx + 1 >= len(sig) or sig[idx + 1].value != b"(":
            continue
        # 收集到配对的 )
        depth = 0
        inner: list[Token] = []
        j = idx + 1
        closed = False
        while j < len(sig):
            t = sig[j]
            if t.value == b"(":
                depth += 1
                if depth > 1:
                    inner.append(t)
            elif t.value == b")":
                depth -= 1
                if depth == 0:
                    closed = True
                    break
                inner.append(t)
            else:
                inner.append(t)
            j += 1
        if not closed:
            continue
        args = _split_args(inner)
        if args is None or not args:
            continue
        # 首参数须为无转义/插值的字符串且内容等于目标名
        first = _significant(args[0])
        if len(first) != 1 or first[0].kind != TokenKind.STRING \
                or first[0].quote not in (b"'", b'"'):
            continue
        if first[0].value[1:-1] != name_bytes:
            continue
        # 是目标调用：检查参数数量（恰两个，允许末尾逗号）
        if len(args) != 2:
            outcome.errors.append(IssueCode.BAD_ARITY)
            continue
        parsed = _parse_simple_value(args[1])
        if parsed is None:
            outcome.errors.append(IssueCode.UNSAFE_OLD_VALUE)
            continue
        lit_tok, old_value = parsed
        outcome.sites.append(SettingSite(lit_tok.start, lit_tok.end, old_value))
    return outcome


def find_table_prefix_sites(tokens: list[Token]) -> SearchOutcome:
    """查找 $table_prefix 的直接等号赋值点。tokens 须已经 _significant 过滤。"""
    outcome = SearchOutcome()
    sig = tokens
    for idx, tok in enumerate(sig):
        if tok.kind != TokenKind.VARIABLE or tok.value != b"$table_prefix":
            continue
        if idx + 1 >= len(sig) or sig[idx + 1].value != b"=":
            continue
        # 排除 == 与 =>
        if idx + 2 < len(sig) and sig[idx + 2].value in (b"=", b">"):
            continue
        # 收集到顶层 ; 为止的表达式
        depth = 0
        expr: list[Token] = []
        j = idx + 2
        while j < len(sig):
            t = sig[j]
            if t.value in (b"(", b"[", b"{"):
                depth += 1
            elif t.value in (b")", b"]", b"}"):
                if depth == 0:
                    break
                depth -= 1
            if depth == 0 and t.value == b";":
                break
            expr.append(t)
            j += 1
        parsed = _parse_simple_value(expr)
        if parsed is None:
            outcome.errors.append(IssueCode.UNSAFE_OLD_VALUE)
            continue
        lit_tok, old_value = parsed
        outcome.sites.append(SettingSite(lit_tok.start, lit_tok.end, old_value))
    return outcome
