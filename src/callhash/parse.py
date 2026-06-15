"""Shared WSJT-X message-field parser.

FT8 / FT4 / MSK144 decoders (``decode_ft8``, ``jt9``) emit a freeform
message body whose grammar is identical across the modes.  Every
recorder consumer — psk-recorder, meteor-scatter, wspr-recorder — needs
the same best-effort extraction of tx/rx call, grid, and report, *and*
the same compound-callsign hash substitution via
:class:`~callhash.table.CallHashTable`.

Before this module the extraction was copy-pasted byte-for-byte into
each recorder's ``ch_tailer.py`` and the table was built but never read,
so hashed spots lost their call.  Centralising it here means one
implementation that resolves hashes the same way everywhere — the whole
point of a shared callhash library.

``parse_message`` is intentionally lossy: the raw (hash-resolved)
message string is always returned in ``message`` so the caller can
preserve it verbatim; the parsed call/grid/report fields are best-effort.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Standard callsigns + WSJT-X compound forms:
#   * standard ITU call:                        K1ABC, AC0G, JA1AAA
#   * suffix-form compound:                     K1ABC/QRP, K1ABC/MM
#   * prefix-form compound (region/portable):   VE3/K1ABC, G/K1ABC, KH6/AC0G
_CALL_RE = re.compile(
    r"^"
    r"(?:[A-Z0-9]{1,3}/)?"               # optional prefix (e.g. "VE3/", "G/")
    r"[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}"    # standard call body (XX[X][D][YY[Y][Y]])
    r"(?:/[A-Z0-9]{1,4})?"                # optional suffix (e.g. "/QRP", "/MM")
    r"$"
)
# Maidenhead 6-char form has uppercase field+square but lowercase
# subsquare (per IARU convention).  Tolerate either case for robustness.
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}(?:[A-Xa-x]{2})?$")
_REPORT_RE = re.compile(r"^R?([+-]?\d+)$")


def _plain(token: str) -> Optional[str]:
    """Return a bare callsign token, or ``None`` if it's still bracketed.

    ``parse_message`` resolves the whole message first, so any token that
    *still* carries angle brackets here (``<...>`` or a numeric hash we've
    never seen announced) is genuinely unrecoverable — drop it from the
    call fields rather than emitting a placeholder.
    """
    if not token or token.startswith("<"):
        return None
    return token


def parse_message(message: str, table: Optional[Any] = None) -> Dict[str, Any]:
    """Parse a decoded FT8/FT4/MSK144 message body.

    When ``table`` (a :class:`CallHashTable`) is supplied, the message is
    first run through :meth:`CallHashTable.resolve_message` so bracketed
    hashes that we've learned are substituted with plaintext calls — this
    is what stops us shipping ``<...>`` spots whose call we actually know.

    Recognized shapes (all approximate; freeform messages return empties):
      "CQ <tx_call> [<grid>]"           — directed CQ
      "<rx_call> <tx_call> <grid>"      — first contact w/ grid
      "<rx_call> <tx_call> [R]<report>" — signal report
      "<rx_call> <tx_call> [73|RR73]"   — close

    Returns a dict with keys ``message`` (the hash-resolved text — callers
    should store THIS, not the raw input), ``tx_call``, ``rx_call``,
    ``grid`` (all ``""`` when absent) and ``report`` (``None`` when absent).
    """
    if table is not None:
        message = table.resolve_message(message)

    out: Dict[str, Any] = {
        "message": message,
        "tx_call": "",
        "rx_call": "",
        "grid": "",
        "report": None,
    }
    tokens = message.split()
    if not tokens:
        return out

    if tokens[0] == "CQ":
        # "CQ [target] <tx_call> [grid]" — `target` may be a region tag
        # like "DX", "EU", "POTA" that isn't a callsign.  Scan past
        # non-call tokens until the first call-shaped one (the sender),
        # then look for a grid in the remaining tokens.
        for i, tok in enumerate(tokens[1:], start=1):
            candidate = _plain(tok)
            if candidate is not None and _CALL_RE.match(candidate):
                out["tx_call"] = candidate
                for later in tokens[i + 1:]:
                    if _GRID_RE.match(later):
                        out["grid"] = later
                        break
                break
    else:
        # <rx_call> <tx_call> [grid|report|RR73|73]
        rx_candidate = _plain(tokens[0])
        if rx_candidate is not None and _CALL_RE.match(rx_candidate):
            out["rx_call"] = rx_candidate
        if len(tokens) >= 2:
            tx_candidate = _plain(tokens[1])
            if tx_candidate is not None and _CALL_RE.match(tx_candidate):
                out["tx_call"] = tx_candidate
        if len(tokens) >= 3:
            tail = tokens[2]
            if _GRID_RE.match(tail):
                out["grid"] = tail
            else:
                m = _REPORT_RE.match(tail)
                if m:
                    try:
                        out["report"] = int(m.group(1))
                    except ValueError:
                        pass
    return out
