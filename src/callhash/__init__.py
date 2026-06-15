"""WSJT-X callsign-hash mechanism — Bob Jenkins lookup3 (seed 146)
plus an accumulator that resolves hashes from ``<call>`` announcement
markers in decoded text.

Background.  WSJT-X's FT4/FT8/WSPR packet formats compress compound
callsigns (e.g. ``K1ABC/QRP``, ``VE3/K1ABC``) into 22-bit (FT8/FT4)
or 15-bit (WSPR) hashes; receivers maintain a session table that
maps the hash back to plaintext when the call has been observed in a
``<call>`` first-occurrence message.  Both ``jt9`` and ``wsprd`` use
this same hash function (``nhash`` / Bob Jenkins lookup3, seed 146).

Per-slot decoder invocations (psk-recorder, wsprdaemon-client) start
with an empty session table, so most hashed packets emerge as
``<...>`` placeholders.  This module reconstructs the table at the
consumer side from the same announcement markers.

Public API:

    from callhash import CallHashTable, hash22, hash15, nhash

    table = CallHashTable.load_or_new("/var/lib/<client>/hashtable.json")
    table.observe("260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> CQ FT8")
    table.by_hash22(hash22("K1ABC/QRP"))   # → "K1ABC/QRP"
    clean = table.normalise_brackets("<K1ABC/QRP>")  # → "K1ABC/QRP"
    clean = table.normalise_brackets("<...>")        # → None
    table.save()                                     # persists to JSON

The hash function is bit-exact against WSJT-X's canonical
``lib/wsprcode/nhash.c`` for all 23 reference vectors in
``tests/test_nhash.py`` (including 11/12/13-byte boundary cases that
exercise distinct final-block branches).
"""
from ._nhash import (
    MASK10,
    MASK12,
    MASK15,
    MASK22,
    WSJTX_INITVAL,
    hash10,
    hash12,
    hash15,
    hash22,
    nhash,
)
from .parse import parse_message
from .table import CallHashTable

__all__ = [
    "CallHashTable",
    "MASK10",
    "MASK12",
    "MASK15",
    "MASK22",
    "WSJTX_INITVAL",
    "hash10",
    "hash12",
    "hash15",
    "hash22",
    "nhash",
    "parse_message",
]
