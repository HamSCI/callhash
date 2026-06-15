"""``CallHashTable`` — accumulator that maps WSJT-X callsign hashes
back to plaintext from ``<call>`` announcement markers.

Two consumers (psk-recorder, wsprdaemon-client) and the same
mechanism: when a compound callsign is first transmitted, both ends
broadcast it in plaintext between angle brackets — ``<K1ABC/QRP>``;
subsequent packets use a 22-bit (FT8/FT4) or 15-bit (WSPR) hash, and
each side resolves the hash via its in-memory table.  Per-invocation
decoders (jt9, wsprd) start with an empty table, so most hashed
packets surface as the literal placeholder ``<...>``.

This class reconstructs the table at the consumer side by watching
decoded text for ``<call>`` markers — the announcement is itself a
plaintext sighting we can hash and store.  Persisting the table to
JSON lets the resolution survive daemon restarts so the cumulative
mapping grows over time.

The class deliberately stays small and stateless beyond the in-memory
maps so it can be embedded in either client without dragging in an
import graph.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

from ._nhash import hash12, hash15, hash22

log = logging.getLogger(__name__)


# Plaintext compound-callsign markers in decoded text:
#   - <K1ABC/QRP>   announcement (canonical case — two- or three-segment
#                   call separated by '/').
#   - <K1ABC>       a hashed call already resolved by the decoder
#                   from its session table; reading us the call back.
#   - <...>         literal "unresolved hash" placeholder (no useful
#                   content).
# The regex below matches the first two but not the literal ellipsis
# placeholder.
_BRACKET_CALL_RE = re.compile(r"<([A-Z0-9][A-Z0-9/]{1,15})>")
_LITERAL_UNRESOLVED = "<...>"
# A bracketed all-numeric token is a hash the decoder couldn't resolve
# from its own session table — jt9 ``-Y`` (FST4W / MSK144) and the
# patched ``decode_ft8`` (FT8 / FT4) both emit the 22-bit hash this way
# (``<NNNNNNN>``, up to 7 digits since MAX22 = 4194304).  No decoder we
# drive emits the narrower 12-bit form as a number, so a numeric bracket
# token is always a 22-bit hash.
_BRACKET_HASH_RE = re.compile(r"<(\d{1,7})>")


class CallHashTable:
    """Per-(client, radiod) cache mapping WSJT-X hashes → plaintext call.

    Thread-safe in the only way that matters here: ``observe`` and
    ``by_hash*`` may be called concurrently by a tailer thread + a
    parser thread without external locking.
    """

    SCHEMA_VERSION = 1     # bump when the on-disk JSON shape changes

    def __init__(self, source_path: Optional[Path] = None) -> None:
        self._source_path: Optional[Path] = (
            Path(source_path) if source_path is not None else None
        )
        self._lock = threading.Lock()
        # Two parallel maps (one per protocol).  Same plaintext call
        # is in both — the cost of double-storage is trivial for the
        # observed table sizes (10²-10³ entries) and avoids per-lookup
        # bit-mask choices on the hot path.
        self._by_h22: Dict[int, str] = {}
        self._by_h15: Dict[int, str] = {}
        # 12-bit map kept for API symmetry with the other widths.  No
        # decoder we drive emits a 12-bit hash as a resolvable number
        # (decode_ft8's 12-bit site stays ``<...>``), so this is rarely
        # hit in practice, but populating it is free.
        self._by_h12: Dict[int, str] = {}
        # Collision guard.  A hash slot claimed by 2+ DISTINCT calls is
        # ambiguous — a reverse lookup can't tell which call the hash
        # meant, so we refuse to resolve it rather than risk emitting the
        # wrong callsign into a spot (a wrong call pollutes wsprnet /
        # pskreporter; a missing call is benign).  This matters because
        # OUR table is persistent and accumulates 10²–10³+ calls, so it
        # sees far more collisions than WSJT-X's short-lived per-session
        # table — especially in the 15-bit space (32 768 slots).  These
        # sets are recomputed on load, not persisted.
        self._collided_h22: set[int] = set()
        self._collided_h15: set[int] = set()
        self._collided_h12: set[int] = set()
        # First-seen timestamp per call — useful for operator forensics
        # ("when did this compound call first appear?") and bounded
        # eviction in future versions.
        self._first_seen: Dict[str, str] = {}
        self._observations = 0       # total announcements seen (for stats)
        self._dirty = False          # save() can short-circuit when no change

    # ----- observation -----

    def observe(self, text: str) -> int:
        """Scan ``text`` for ``<call>`` markers and add each to the table.

        Returns the number of NEW entries inserted by this call.
        Multiple calls in one line are all extracted; existing entries
        are left unchanged (announcements are stable per call).
        """
        added = 0
        with self._lock:
            for match in _BRACKET_CALL_RE.findall(text):
                call = match.strip()
                if not call:
                    continue
                if not _looks_like_callsign(call):
                    continue
                self._observations += 1
                if self._add_locked(call):
                    added += 1
        if added:
            log.debug("callhash: +%d new call(s); table now %d (h22) / %d (h15)",
                      added, len(self._by_h22), len(self._by_h15))
        return added

    def add(self, call: str) -> bool:
        """Direct-add a single plaintext callsign.  Returns True if new."""
        with self._lock:
            return self._add_locked(call)

    def ingest_calls(self, calls: Iterable[str]) -> int:
        """Add a batch of plaintext callsigns.  Returns the count of NEW
        entries.

        Convenience for consumers that discover full calls in decoded
        output (rather than ``<call>`` brackets) — e.g. wspr-recorder's
        per-cycle spot ingest.  Non-callsign-shaped entries are skipped.
        """
        added = 0
        with self._lock:
            for call in calls:
                if not call:
                    continue
                call = call.strip()
                if not _looks_like_callsign(call):
                    continue
                if self._add_locked(call):
                    added += 1
        return added

    def _add_locked(self, call: str) -> bool:
        is_new = call not in self._first_seen
        self._index_call(call)
        if is_new:
            self._first_seen[call] = datetime.now(tz=timezone.utc).isoformat()
            self._dirty = True
        return is_new

    def _index_call(self, call: str) -> None:
        """Insert ``call`` into the three width-keyed maps, flagging any
        slot already owned by a *different* call as collided.

        The first call to claim a slot keeps it; a second, distinct call
        that hashes to the same slot makes it ambiguous (added to the
        collided set) so neither resolves.  Detection is idempotent and
        order-independent, so live observation and on-disk replay
        produce the same ambiguous-slot set.
        """
        for hfn, fwd, collided in (
            (hash22, self._by_h22, self._collided_h22),
            (hash15, self._by_h15, self._collided_h15),
            (hash12, self._by_h12, self._collided_h12),
        ):
            h = hfn(call)
            existing = fwd.get(h)
            if existing is None:
                fwd[h] = call
            elif existing != call:
                collided.add(h)

    # ----- lookup -----

    def by_hash22(self, h: int) -> Optional[str]:
        """Resolve a 22-bit hash (FT8/FT4) to plaintext, or None.

        Returns None for an ambiguous (collided) slot — see the collision
        guard in ``__init__`` — so we never return a guessed call.
        """
        with self._lock:
            h &= 0x3FFFFF
            if h in self._collided_h22:
                return None
            return self._by_h22.get(h)

    def by_hash15(self, h: int) -> Optional[str]:
        """Resolve a 15-bit hash (WSPR Type 3) to plaintext, or None.

        Returns None for an ambiguous (collided) slot.
        """
        with self._lock:
            h &= 0x7FFF
            if h in self._collided_h15:
                return None
            return self._by_h15.get(h)

    def by_hash12(self, h: int) -> Optional[str]:
        """Resolve a 12-bit hash to plaintext, or None.

        Returns None for an ambiguous (collided) slot.
        """
        with self._lock:
            h &= 0xFFF
            if h in self._collided_h12:
                return None
            return self._by_h12.get(h)

    # ----- token / message resolution -----

    def resolve_token(self, token: str) -> Optional[str]:
        """Resolve a single decoded-message token to a plaintext call.

        This is the canonical, shared substitution primitive used by
        every consumer (psk / meteor-scatter / wspr) so they behave
        identically.  Cases:

          * ``<NNNNNNN>`` (numeric hash) → :meth:`by_hash22` lookup;
            returns the plaintext call if we've seen it announced, else
            ``None`` (still genuinely unknown — keep the placeholder).
          * ``<...>``       → ``None`` (decoder discarded the hash number;
            nothing to look up).
          * ``<CALL>`` / ``<CALL/QRP>`` → the decoder resolved it from its
            own session table; strip the brackets, **seed our table** with
            the sighting, and return the bare call.
          * bare token (``K1ABC``, ``CQ``, ``EM38``) → returned unchanged.
          * anything else in brackets (garbage) → ``None``.
        """
        if not token:
            return token
        t = token.strip()
        if not (t.startswith("<") and t.endswith(">") and len(t) > 2):
            return t                                   # bare token
        if t == _LITERAL_UNRESOLVED:
            return None
        m = _BRACKET_HASH_RE.fullmatch(t)
        if m:
            return self.by_hash22(int(m.group(1)))
        m = _BRACKET_CALL_RE.fullmatch(t)
        if m:
            call = m.group(1)
            if _looks_like_callsign(call):
                self.add(call)                          # self-seeding sighting
                return call
        return None                                     # bracketed garbage

    def resolve_message(self, message: str) -> str:
        """Return ``message`` with every resolvable hash token replaced by
        its plaintext call.

        Unresolvable tokens (``<...>`` or a numeric hash we've never seen
        announced) are left exactly as-is, so the raw text is preserved
        when we genuinely can't recover the call.  Token spacing is
        normalised to single spaces.
        """
        if not message:
            return message
        out = []
        for tok in message.split():
            resolved = self.resolve_token(tok)
            out.append(resolved if resolved is not None else tok)
        return " ".join(out)

    def __contains__(self, call: str) -> bool:
        with self._lock:
            return call in self._first_seen

    def __len__(self) -> int:
        with self._lock:
            return len(self._first_seen)

    @property
    def observations(self) -> int:
        """Total ``<call>`` markers observed (incl. duplicates)."""
        with self._lock:
            return self._observations

    # ----- normalisation helper -----

    @staticmethod
    def normalise_brackets(token: str) -> Optional[str]:
        """Canonicalise a possibly-bracketed callsign token.

        Per the wsprd / jt9 output convention:
          * ``<K1ABC>`` or ``<K1ABC/QRP>`` → strip brackets, return
            the inner call (it has been resolved by the decoder).
          * ``<...>`` → return ``None`` (unresolved hash; no useful
            information at this point — caller may try
            :meth:`by_hash22` / :meth:`by_hash15` if it has a hash to
            look up).
          * Anything else → return as-is.
        """
        if not token:
            return token
        t = token.strip()
        if t == _LITERAL_UNRESOLVED:
            return None
        m = _BRACKET_CALL_RE.fullmatch(t)
        if m:
            return m.group(1)
        return t

    # ----- persistence -----

    @classmethod
    def load_or_new(cls, source_path: Path | str) -> "CallHashTable":
        """Load from JSON if it exists, else return a fresh table.

        The file may be absent, empty, or contain malformed JSON
        (cleared by an operator, half-written by a previous crashed
        save) — in any of those cases we start fresh and log a
        warning.  We never delete the operator's file on load failure.
        """
        path = Path(source_path)
        instance = cls(source_path=path)
        if not path.exists():
            return instance
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.warning("callhash: ignoring unreadable %s (%s); starting fresh",
                        path, e)
            return instance
        if not isinstance(data, dict):
            return instance
        if data.get("schema_version") != cls.SCHEMA_VERSION:
            log.warning("callhash: %s has schema_version=%r, expected %d; "
                        "starting fresh", path,
                        data.get("schema_version"), cls.SCHEMA_VERSION)
            return instance
        for call, first_seen in (data.get("calls") or {}).items():
            if not _looks_like_callsign(call):
                continue
            instance._first_seen[call] = first_seen
            instance._index_call(call)
        instance._observations = int(data.get("observations", 0))
        instance._dirty = False
        log.debug("callhash: loaded %d calls from %s", len(instance), path)
        return instance

    def save(self, path: Optional[Path | str] = None) -> None:
        """Atomically persist the table to disk.

        Writes to ``<path>.tmp`` then ``os.replace`` so a partial
        write can't corrupt an existing good file.  No-op when no
        observations have changed since the last save (or load).
        """
        target = Path(path) if path is not None else self._source_path
        if target is None:
            raise ValueError(
                "save(): no path supplied and no source_path set on table"
            )
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "schema_version": self.SCHEMA_VERSION,
                "saved_at": datetime.now(tz=timezone.utc).isoformat(),
                "observations": self._observations,
                "calls": dict(self._first_seen),
            }
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(target)
        with self._lock:
            self._dirty = False

    # ----- decoder-seed export -----

    def write_wsprd_hashtable(
        self,
        path: Path | str,
        *,
        exclude: Optional[Callable[[str], bool]] = None,
    ) -> int:
        """Write wsprd's ``hashtable.txt`` (15-bit index → call).

        Pre-populating this file lets ``wsprd`` resolve Type-3 hashes it
        would otherwise emit as ``<...>`` (wsprd discards the number, so
        the only way to recover those calls is to seed the decoder ahead
        of the run).  Format matches wsprd: ``"%5d %s\\n"``.

        ``exclude`` is an optional predicate — calls for which it returns
        True are omitted.  wsprd-recorder passes its wsprnet
        negative-cache filter here so consistently-rejected compounds
        stop being re-emitted; the library itself stays oblivious to that
        policy.  Returns the number of entries written.
        """
        path = Path(path)
        with self._lock:
            calls = list(self._first_seen)
        count = 0
        with open(path, "w") as f:
            for call in calls:
                if exclude is not None and exclude(call):
                    continue
                f.write(f"{hash15(call):5d} {call}\n")
                count += 1
        return count

    def write_jt9_calls(
        self,
        path: Path | str,
        grids: Optional[Dict[str, str]] = None,
        *,
        exclude: Optional[Callable[[str], bool]] = None,
    ) -> int:
        """Write jt9's ``fst4w_calls.txt`` (call + grid per line).

        Same role as :meth:`write_wsprd_hashtable` for the jt9 / FST4W
        side.  The library stores no grids, so the caller may pass a
        ``{call: grid}`` map; missing grids are rendered as four spaces
        (jt9's blank-grid convention).  ``exclude`` works as above.
        Returns the number of entries written.
        """
        path = Path(path)
        grids = grids or {}
        with self._lock:
            calls = list(self._first_seen)
        count = 0
        with open(path, "w") as f:
            for call in calls:
                if exclude is not None and exclude(call):
                    continue
                grid = grids.get(call) or "    "
                f.write(f"{call} {grid}\n")
                count += 1
        return count

    # ----- introspection -----

    def stats(self) -> Tuple[int, int, int]:
        """Return ``(unique_calls, observations, h22_entries)`` snapshot."""
        with self._lock:
            return len(self._first_seen), self._observations, len(self._by_h22)

    def calls(self) -> list[str]:
        """Return a snapshot list of known plaintext calls."""
        with self._lock:
            return sorted(self._first_seen)


# ── helpers ────────────────────────────────────────────────────────────────

# Loose callsign sanity check — both standard ITU calls and compound
# variants we care about.  We let through anything that's mostly
# alphanumerics with at most a few '/' separators; the decoder's own
# bracket markers already filter out garbage upstream.
_CALLSIGN_VALIDATOR = re.compile(
    r"^[A-Z0-9](?:[A-Z0-9/]{0,15})$"
)


def _looks_like_callsign(call: str) -> bool:
    return bool(_CALLSIGN_VALIDATOR.fullmatch(call))
