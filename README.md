# callhash

Pure-Python WSJT-X compound-callsign hash resolution.

WSJT-X's FT4/FT8/WSPR packet formats compress compound callsigns
(e.g. `K1ABC/QRP`, `VE3/K1ABC`) into 22-bit (FT8/FT4) or 15-bit
(WSPR Type 3) hashes. Receivers maintain a session table that maps
the hash back to plaintext when the call has been observed in a
`<call>` first-occurrence message. Both `jt9` and `wsprd` use the
same hash function — Bob Jenkins lookup3, seed 146.

When a per-slot decoder invocation starts with an empty session
table (the typical case for `psk-recorder`, `meteor-scatter`,
`wspr-recorder`, or any consumer that drives the decoder one cycle
at a time), most hashed packets surface as the literal `<...>`
placeholder — or, when the decoder is told to emit the number
(`jt9 -Y`, the patched `decode_ft8`), as `<NNNNNNN>`. This library
reconstructs the table on the consumer side from the same
announcement markers WSJT-X uses, and substitutes the numeric
hashes back to plaintext.

All three HamSCI recorders use this one library for build
(`observe`), lookup (`by_hashNN`), and substitution
(`resolve_token` / `resolve_message` / `parse_message`) — the same
mechanism everywhere, so a compound call learned on one mode/band
resolves hashes on another. Every mode hashes with the identical
`nhash`; only the width differs (22-bit FT8/FT4/MSK144/FST4W,
15-bit WSPR-2), and the table keys each width separately.

## Install

```
pip install callhash
```

Or from a sibling checkout (HamSCI deployment pattern):

```
pip install -e /opt/git/sigmond/callhash
```

## Usage

```python
from callhash import CallHashTable, hash22, hash15, nhash

# Persistent per-station cache.
table = CallHashTable.load_or_new("/var/lib/myclient/callhash.json")

# Feed any text containing <call> markers; the table extracts them.
table.observe("260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> CQ FT8")

# Look a hash up if you happen to have one.
table.by_hash22(hash22("K1ABC/QRP"))   # → "K1ABC/QRP"
table.by_hash15(hash15("K1ABC/QRP"))   # → "K1ABC/QRP"

# Resolve a decoded message: substitutes <NNNNNNN>/<CALL>, drops <...>.
table.resolve_message("AC0G <2288505> R-12")   # → "AC0G K9AN R-12" (if learned)

# Or parse a whole message into call fields, resolving hashes via the table.
from callhash import parse_message
parse_message("AC0G <2288505> R-12", table=table)
# → {"message": "AC0G K9AN R-12", "rx_call": "AC0G", "tx_call": "K9AN",
#    "grid": "", "report": -12}

# Persist for the next invocation.
table.save()
```

## Public API

| Symbol                | Purpose                                                                |
| --------------------- | ---------------------------------------------------------------------- |
| `nhash(key, initval)` | Bob Jenkins lookup3 32-bit hash. `initval=146` matches WSJT-X.         |
| `hash22(call)`        | Convenience: `nhash(call) & 0x3FFFFF` (FT8/FT4 compound-call width).   |
| `hash15(call)`        | Convenience: `nhash(call) & 0x7FFF` (WSPR Type 3 width).               |
| `hash12(call)`        | Convenience: 12-bit mask (some ARRL contest variants).                 |
| `hash10(call)`        | Convenience: 10-bit mask (narrowest WSJT-X variant).                   |
| `CallHashTable`       | Persistent accumulator + lookup + substitution + collision guard.      |
| `parse_message(msg, table=None)` | Shared WSJT-X message-field parser (tx/rx/grid/report) + hash substitution. |

`CallHashTable` resolves with `resolve_token` / `resolve_message`,
seeds decoders with `write_wsprd_hashtable` / `write_jt9_calls`
(both take an `exclude=` predicate), and **guards collisions**: a
hash slot claimed by two distinct calls is ambiguous, so
`by_hashNN` returns `None` rather than a guessed (wrong) call. This
matters because a persistent table accumulates far more calls than
WSJT-X's per-session table, so collisions — especially in the
15-bit space — are common.

## Correctness

`nhash` is bit-exact against WSJT-X's canonical
`lib/wsprcode/nhash.c` (the unmasked variant — note that
`lib/wsprd/nhash.c` has a 15-bit mask baked into its return). 23
reference vectors verified, including 11/12/13-byte boundary cases
that exercise distinct final-block branches in the C code.

The `CallHashTable` covers `<call>` announcement extraction,
hash lookups, token/message substitution, the collision guard,
atomic JSON persistence (write-tempfile + rename), corrupt-JSON /
schema-mismatch recovery, and concurrent-observe / concurrent-lookup
safety. 89 tests total; no live ClickHouse / WSJT-X server required
to run them.

## Why this lives in its own repo

The hash function and bracket-resolution logic are WSJT-X's, not
sigmond's. They're useful for any FT8/FT4/WSPR consumer — the
sigmond client suite is the primary user today, but a future
`hs-uploader` library or any independent log analyser will
benefit equally. Pure stdlib, no runtime deps, ~200 lines of
code; small enough to live cleanly on its own.

## License

MIT.
