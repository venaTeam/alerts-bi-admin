"""Represent team IDs as one unambiguous URL segment without changing their identity."""

from __future__ import annotations

from string import ascii_letters, digits

_PLAIN = frozenset(ascii_letters + digits + "-_")


def encode_team_id(team_id: str) -> str:
    """Keep legacy slugs; encode other IDs as a reserved-prefix UTF-8 hex token.

    A token survives URL decoding and dot-segment normalization, including IDs with
    slashes, percent escapes, or literal tildes. The registry ID is never normalized.
    """
    if team_id and all(char in _PLAIN for char in team_id):
        return team_id
    return "~" + team_id.encode("utf-8").hex()


def decode_team_id(segment: str) -> str:
    """Decode a canonical token, or accept a legacy unencoded single-segment ID."""
    if not segment.startswith("~"):
        return segment
    try:
        decoded = bytes.fromhex(segment[1:]).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid team URL") from exc
    if not decoded or encode_team_id(decoded) != segment:
        raise ValueError("Invalid team URL")
    return decoded
