"""Recursive-descent parser producing the WHERE-clause AST (design section 5.2).

"Build a small parser for the supported query language rather than interpreting query text
with string matching": the discriminator between suppression and scoping is which field is
negated and where the predicate sits in the boolean tree, and only a real tree can answer
the second half. Reading ``severity = 'critical'`` as suppression would mark a team's
entire non-critical inventory as bad alerts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from alerts_bi_operations.suppression.lexer import SqlParseError, Token, tokenize

__all__ = [
    "Leaf",
    "Node",
    "Operand",
    "SqlParseError",
    "collect_leaves",
    "parse_panel_sql",
]

#: Clauses that end the WHERE expression.
_TERMINATORS: Final = frozenset({"GROUP", "ORDER", "HAVING", "LIMIT", "OFFSET", "UNION"})


@dataclass(frozen=True, slots=True)
class Operand:
    """A column, literal, template variable or function call."""

    kind: str
    """``field`` | ``literal`` | ``variable`` | ``call``"""
    name: str | None = None
    value: str | None = None
    raw: str | None = None
    args: tuple[Operand, ...] = ()
    macro: bool = False


@dataclass(frozen=True, slots=True)
class Node:
    """A boolean combinator: ``and``, ``or`` or ``not``."""

    type: str
    left: Any = None
    right: Any = None
    operand: Any = None


@dataclass(frozen=True, slots=True)
class Leaf:
    """A predicate the classifier may inspect."""

    type: str
    """``comparison`` | ``in`` | ``like`` | ``is_null`` | ``between`` | ``call``"""
    field: Operand | None = None
    operator: str | None = None
    value: Operand | None = None
    values: tuple[Operand, ...] = ()
    pattern: Operand | None = None
    low: Operand | None = None
    high: Operand | None = None
    negated: bool = False
    name: str | None = None
    macro: bool = False
    args: tuple[Operand, ...] = ()


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def next(self) -> Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def match_keyword(self, value: str) -> bool:
        token = self.peek()
        if token.type == "keyword" and token.value == value:
            self.pos += 1
            return True
        return False

    def match_punct(self, value: str) -> bool:
        token = self.peek()
        if token.type == "punct" and token.value == value:
            self.pos += 1
            return True
        return False

    def expect_punct(self, value: str) -> None:
        if not self.match_punct(value):
            raise SqlParseError(f"expected {value!r}", self.peek().start)

    def at_expression_end(self) -> bool:
        token = self.peek()
        if token.type == "eof":
            return True
        if token.type == "punct" and token.value in (")", ";"):
            return True
        return token.type == "keyword" and token.value in _TERMINATORS

    def parse_expression(self) -> Any:
        return self.parse_or()

    def parse_or(self) -> Any:
        left = self.parse_and()
        while self.peek().type == "keyword" and self.peek().value == "OR":
            self.next()
            left = Node("or", left=left, right=self.parse_and())
        return left

    def parse_and(self) -> Any:
        left = self.parse_not()
        while self.peek().type == "keyword" and self.peek().value == "AND":
            self.next()
            left = Node("and", left=left, right=self.parse_not())
        return left

    def parse_not(self) -> Any:
        if self.match_keyword("NOT"):
            return Node("not", operand=self.parse_not())
        return self.parse_primary()

    def parse_primary(self) -> Any:
        if self.match_punct("("):
            inner = self.parse_expression()
            self.expect_punct(")")
            return inner
        return self.parse_predicate()

    def parse_operand(self) -> Operand:
        token = self.next()

        if token.type == "identifier":
            if self.peek().type == "punct" and self.peek().value == "(":
                return Operand("call", name=token.value, args=self.parse_argument_list())
            # Strip a table qualifier: `a.node_name` identifies the same column.
            bare = token.value.split(".")[-1] if "." in token.value else token.value
            return Operand("field", name=bare, raw=token.value)

        if token.type == "variable":
            if self.peek().type == "punct" and self.peek().value == "(":
                return Operand(
                    "call", name=token.value, args=self.parse_argument_list(), macro=True
                )
            return Operand("variable", name=token.value, raw=token.raw)

        if token.type in ("string", "number"):
            return Operand("literal", value=token.value)
        if token.type == "keyword" and token.value in ("NULL", "TRUE", "FALSE"):
            return Operand("literal", value=token.value)

        raise SqlParseError(f"unexpected token {token.raw!r} in expression", token.start)

    def parse_argument_list(self) -> tuple[Operand, ...]:
        self.expect_punct("(")
        args: list[Operand] = []
        if self.match_punct(")"):
            return ()
        while True:
            args.append(self.parse_operand())
            if self.match_punct(","):
                continue
            self.expect_punct(")")
            return tuple(args)

    def parse_predicate(self) -> Leaf:
        left = self.parse_operand()

        # A bare macro such as $__timeFilter(@timestamp) is a complete predicate.
        if left.kind == "call":
            token = self.peek()
            if self.at_expression_end() or (
                token.type == "keyword"
                and (token.value in ("AND", "OR") or token.value in _TERMINATORS)
            ):
                return Leaf("call", name=left.name, args=left.args, macro=left.macro)

        if self.match_keyword("IS"):
            negated = self.match_keyword("NOT")
            if not self.match_keyword("NULL"):
                raise SqlParseError("expected NULL after IS", self.peek().start)
            return Leaf("is_null", field=left, negated=negated)

        negated = False
        if self.peek().type == "keyword" and self.peek().value == "NOT":
            after = self.peek(1)
            if after.type == "keyword" and after.value in ("IN", "LIKE", "BETWEEN"):
                self.next()
                negated = True

        if self.match_keyword("IN"):
            return Leaf("in", field=left, values=self.parse_argument_list(), negated=negated)

        if self.match_keyword("LIKE"):
            return Leaf("like", field=left, pattern=self.parse_operand(), negated=negated)

        if self.match_keyword("BETWEEN"):
            low = self.parse_operand()
            if not self.match_keyword("AND"):
                raise SqlParseError("expected AND in BETWEEN", self.peek().start)
            return Leaf("between", field=left, low=low, high=self.parse_operand(), negated=negated)

        if negated:
            raise SqlParseError(
                "NOT must be followed by IN, LIKE or BETWEEN here", self.peek().start
            )

        token = self.peek()
        if token.type == "operator":
            self.next()
            return Leaf("comparison", field=left, operator=token.value, value=self.parse_operand())

        raise SqlParseError(
            f"unsupported predicate near {(token.raw or token.value)!r}", token.start
        )


def parse_panel_sql(sql_text: str) -> Any | None:
    """Parse a panel query and return its WHERE-clause AST, or ``None`` if it has none.

    A query with no WHERE clause is valid and simply suppresses nothing.
    """
    tokens = tokenize(sql_text)

    # Find the top-level WHERE. Depth tracking keeps a subquery's WHERE from being taken
    # for the outer one.
    depth = 0
    where_index = -1
    for index, token in enumerate(tokens):
        if token.type == "punct" and token.value == "(":
            depth += 1
        elif token.type == "punct" and token.value == ")":
            depth -= 1
        elif token.type == "keyword" and token.value == "WHERE" and depth == 0:
            where_index = index
            break

    if where_index == -1:
        return None

    parser = _Parser(tokens[where_index + 1 :])
    where = parser.parse_expression()
    if not parser.at_expression_end():
        raise SqlParseError(
            f"unparsed input after the WHERE clause near {parser.peek().raw!r}",
            parser.peek().start,
        )
    return where


def collect_leaves(node: Any, nested: bool = False) -> list[tuple[Leaf, bool]]:
    """Yield every leaf together with whether it sits inside an ``OR`` or a ``NOT``.

    This is the rewrite-safety rule from design section 5.2: only suppression leaves that
    are AND-ed at the top level may be evaluated, because
    ``operator = 'x' AND (node_name != 'junk' OR severity = 'critical')`` changes meaning
    if the leaf is lifted out. A ``NOT`` wrapper flips the leaf's sense, so its subtree is
    treated the same way - nested, and therefore unmeasured if it looks like suppression.
    """
    if node is None:
        return []
    if isinstance(node, Node):
        if node.type == "and":
            return [*collect_leaves(node.left, nested), *collect_leaves(node.right, nested)]
        if node.type == "or":
            return [*collect_leaves(node.left, True), *collect_leaves(node.right, True)]
        if node.type == "not":
            return collect_leaves(node.operand, True)
    return [(node, nested)]
