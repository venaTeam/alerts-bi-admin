"""Tokenizer for the panel-query subset (design section 5.2).

A purpose-built lexer rather than a general SQL library: the queries carry Grafana template
variables (``$nodes``, ``${nodes}``, ``[[nodes]]``, ``$__timeFilter(...)``) that a standard
SQL grammar rejects outright, and the classification this step performs is a lookup over
field names and operators rather than full SQL semantics.

Anything the lexer cannot represent makes the parse fail, and a parse failure produces
``suppression_unmeasured`` - never an assumed suppression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

__all__ = ["KEYWORDS", "SqlParseError", "Token", "TokenType", "tokenize"]

TokenType = Literal[
    "identifier", "string", "number", "operator", "punct", "keyword", "variable", "eof"
]


class SqlParseError(Exception):
    """Raised when panel SQL cannot be represented; never an assumed suppression."""

    def __init__(self, message: str, position: int | None = None) -> None:
        self.position = position
        super().__init__(message if position is None else f"{message} (at offset {position})")


@dataclass(frozen=True, slots=True)
class Token:
    type: TokenType
    value: str
    """Normalized value: keywords uppercased, strings unquoted."""
    raw: str
    """Source text exactly as written."""
    start: int


#: Words that must not be read as column names.
KEYWORDS: Final = frozenset(
    {
        "SELECT",
        "FROM",
        "WHERE",
        "AND",
        "OR",
        "NOT",
        "IN",
        "LIKE",
        "IS",
        "NULL",
        "BETWEEN",
        "GROUP",
        "ORDER",
        "BY",
        "HAVING",
        "LIMIT",
        "OFFSET",
        "AS",
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "OUTER",
        "ON",
        "UNION",
        "DISTINCT",
        "TOP",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "ASC",
        "DESC",
        "TRUE",
        "FALSE",
    }
)

#: Multi-character operators, longest first so ``<=`` is not read as ``<`` then ``=``.
_OPERATORS: Final = ("<=", ">=", "<>", "!=", "!<", "!>", "=", "<", ">")

_NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")
_BARE_VARIABLE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_IDENTIFIER = re.compile(r"[A-Za-z_@#][A-Za-z0-9_@#$.]*")
_PUNCT: Final = "(),;*"


def tokenize(sql_text: str) -> list[Token]:
    """Tokenize panel SQL, or raise :class:`SqlParseError`."""
    tokens: list[Token] = []
    i = 0
    n = len(sql_text)

    while i < n:
        char = sql_text[i]

        if char.isspace():
            i += 1
            continue

        # line comment
        if char == "-" and sql_text.startswith("--", i):
            newline = sql_text.find("\n", i)
            i = n if newline == -1 else newline
            continue

        # block comment
        if char == "/" and sql_text.startswith("/*", i):
            end = sql_text.find("*/", i + 2)
            if end == -1:
                raise SqlParseError("unterminated block comment", i)
            i = end + 2
            continue

        # single-quoted string; '' is an escaped quote
        if char == "'":
            start = i
            i += 1
            value: list[str] = []
            while True:
                if i >= n:
                    raise SqlParseError("unterminated string literal", start)
                if sql_text[i] == "'":
                    if i + 1 < n and sql_text[i + 1] == "'":
                        value.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                value.append(sql_text[i])
                i += 1
            tokens.append(Token("string", "".join(value), sql_text[start:i], start))
            continue

        # Grafana variable: [[name]]
        if sql_text.startswith("[[", i):
            end = sql_text.find("]]", i + 2)
            if end == -1:
                raise SqlParseError("unterminated [[variable]]", i)
            tokens.append(
                Token("variable", sql_text[i + 2 : end].strip(), sql_text[i : end + 2], i)
            )
            i = end + 2
            continue

        # bracketed identifier
        if char == "[":
            end = sql_text.find("]", i + 1)
            if end == -1:
                raise SqlParseError("unterminated [identifier]", i)
            tokens.append(Token("identifier", sql_text[i + 1 : end], sql_text[i : end + 1], i))
            i = end + 1
            continue

        # double-quoted identifier
        if char == '"':
            end = sql_text.find('"', i + 1)
            if end == -1:
                raise SqlParseError('unterminated "identifier"', i)
            tokens.append(Token("identifier", sql_text[i + 1 : end], sql_text[i : end + 1], i))
            i = end + 1
            continue

        # Grafana variable: ${name} or ${name:format}
        if sql_text.startswith("${", i):
            end = sql_text.find("}", i + 2)
            if end == -1:
                raise SqlParseError("unterminated ${variable}", i)
            inner = sql_text[i + 2 : end].strip()
            # A format suffix (${nodes:csv}) selects rendering, not identity.
            tokens.append(Token("variable", inner.split(":")[0].strip(), sql_text[i : end + 1], i))
            i = end + 1
            continue

        # Grafana variable or macro: $name / $__timeFilter
        if char == "$":
            match = _BARE_VARIABLE.match(sql_text, i)
            if match is None:
                raise SqlParseError("stray $ in query", i)
            tokens.append(Token("variable", match.group(0)[1:], match.group(0), i))
            i = match.end()
            continue

        if char.isdigit():
            match = _NUMBER.match(sql_text, i)
            assert match is not None
            tokens.append(Token("number", match.group(0), match.group(0), i))
            i = match.end()
            continue

        if char.isalpha() or char in "_@#":
            match = _IDENTIFIER.match(sql_text, i)
            assert match is not None
            raw = match.group(0)
            upper = raw.upper()
            is_keyword = upper in KEYWORDS
            tokens.append(
                Token(
                    "keyword" if is_keyword else "identifier", upper if is_keyword else raw, raw, i
                )
            )
            i = match.end()
            continue

        operator = next((op for op in _OPERATORS if sql_text.startswith(op, i)), None)
        if operator is not None:
            tokens.append(Token("operator", operator, operator, i))
            i += len(operator)
            continue

        if char in _PUNCT:
            tokens.append(Token("punct", char, char, i))
            i += 1
            continue

        raise SqlParseError(f"unexpected character {char!r}", i)

    tokens.append(Token("eof", "", "", n))
    return tokens
