"""
gs232_parser.py — Stateless GS-232 command parser.

Accepts a raw command string, returns a ParseResult dict or raises
ParseError. No I/O, no side effects — easy to unit test in isolation.

Supported commands (subset used by telescope controllers):
  C        — report current az + el
  C2       — report current az + el (alias, some controllers use this)
  W aaa eee — go to azimuth aaa, elevation eee (0-padded 3-digit integers)
  M aaa    — go to azimuth aaa only
  S        — stop all movement
  B        — report azimuth only
  A        — report elevation only
"""

import re
from typing import TypedDict, Literal, Optional


class ParseError(ValueError):
    """Raised when a command string cannot be parsed."""
    pass


class ParseResult(TypedDict):
    cmd:  Literal['C', 'W', 'M', 'S', 'B', 'A']
    az:   Optional[float]   # target azimuth,  W and M only
    el:   Optional[float]   # target elevation, W only


# Compiled patterns — module-level so they are only built once
_RE_W = re.compile(r'^W\s*(\d{1,3})\s+(\d{1,3})\s*$')
_RE_M = re.compile(r'^M\s*(\d{1,3})\s*$')
_SINGLE = {'C', 'C2', 'S', 'B', 'A'}


def parse(raw: str) -> ParseResult:
    """
    Parse a single GS-232 command string.

    Args:
        raw: Raw command string, may include trailing \\r\\n.

    Returns:
        ParseResult dict with cmd and optional az/el fields.

    Raises:
        ParseError: If the command is unrecognised or malformed.
    """
    cmd = raw.strip().upper()

    if not cmd:
        raise ParseError("empty command")

    # Single-letter / no-argument commands
    if cmd in _SINGLE:
        # Normalise C2 → C so the controller only needs to handle 'C'
        return ParseResult(cmd='C' if cmd == 'C2' else cmd, az=None, el=None)

    # W aaa eee
    m = _RE_W.match(cmd)
    if m:
        az = float(m.group(1))
        el = float(m.group(2))
        _validate_az(az)
        _validate_el(el)
        return ParseResult(cmd='W', az=az, el=el)

    # M aaa
    m = _RE_M.match(cmd)
    if m:
        az = float(m.group(1))
        _validate_az(az)
        return ParseResult(cmd='M', az=az, el=None)

    raise ParseError(f"unrecognised command: {raw!r}")


def format_position(az: float, el: float) -> bytes:
    """
    Format a GS-232 position response.

    Returns bytes ready to write to the PTY, e.g. b'AZ=180 EL=045\\r\\n'
    """
    return f"AZ={round(az):03d} EL={round(el):03d}\r\n".encode()


def format_az(az: float) -> bytes:
    """Format azimuth-only response."""
    return f"AZ={round(az):03d}\r\n".encode()


def format_el(el: float) -> bytes:
    """Format elevation-only response."""
    return f"EL={round(el):03d}\r\n".encode()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_az(az: float) -> None:
    if not (0 <= az <= 450):
        raise ParseError(f"azimuth out of range: {az}")


def _validate_el(el: float) -> None:
    if not (0 <= el <= 180):
        raise ParseError(f"elevation out of range: {el}")
