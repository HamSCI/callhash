# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**callhash** is a pure-Python implementation of WSJT-X's
compound-callsign hash mechanism — Bob Jenkins lookup3 with seed 146 —
plus a persistent per-station accumulator that resolves the hashes from
`<call>` announcement markers in decoded `jt9` / `wsprd` output.

WSJT-X's FT4 / FT8 / WSPR packet formats compress compound callsigns
(e.g. `K1ABC/QRP`, `VE3/K1ABC`) into 22-bit (FT8/FT4/MSK144/FST4W) or
15-bit (WSPR-2 Type 3) hashes. Per-slot decoder invocations that start
with an empty session table — the typical case for `psk-recorder`,
`meteor-scatter`, and `wspr-recorder` — surface hashed packets as the
literal `<...>` placeholder. This library reconstructs the table on the
consumer side and resolves the hashes back to plaintext.

**This library is the single source of truth for hash handling across
all three recorders.** Build (`observe`), lookup (`by_hashNN`), and
substitution (`resolve_token` / `resolve_message` / `parse_message`)
all live here, so a compound call learned on any mode/band resolves the
same way everywhere. The recorders are thin callers — see "Consumers".

### Same function, different width — and one shared inventory

Every mode hashes with the **identical** `nhash` (Jenkins lookup3, seed
146); only the truncation width differs: **22-bit** for FT8 / FT4 /
MSK144 / FST4W-via-`jt9 -Y`, **15-bit** for WSPR-2-via-`wsprd`. So for a
given call the 15-bit value is exactly the low 15 bits of the 22-bit
value (`hash15 == hash22 & 0x7FFF`) — *related, not interchangeable*.
`CallHashTable` keeps **three width-keyed maps** (`_by_h22/_by_h15/
_by_h12`); a lookup only ever consults the map matching the decoder's
width. The shared part is the *call inventory* (cross-mode pollination),
not the numbers.

Part of the HamSCI sigmond suite — see `/opt/git/sigmond/sigmond/CLAUDE.md`
(orchestrator) and `/opt/git/sigmond/CLAUDE.md` (umbrella) for
cross-repo context. This is a pure library with **no runtime
dependencies** — see `pyproject.toml`'s empty `dependencies = []`.

## Authors

- Michael Hauan (AC0G, GitHub: mijahauan)
- Repo: https://github.com/HamSCI/callhash
- `nhash` is a port of WSJT-X's canonical `lib/wsprcode/nhash.c`
  (Bob Jenkins's lookup3, public domain).

## Commands

```bash
# Development
uv sync --extra dev
uv run pytest tests/                    # ~89 tests; no live ClickHouse / WSJT-X
uv run pytest tests/test_nhash.py -v    # one file
uv run pytest -k bracket -v             # by keyword

# Install (consumers typically resolve via [tool.uv.sources] editable path)
pip install callhash                    # PyPI
pip install -e /opt/git/sigmond/callhash  # sibling editable (HamSCI pattern)
```

## Public API

| Symbol | Purpose |
|---|---|
| `nhash(key, initval)` | Bob Jenkins lookup3 32-bit hash. `initval=146` matches WSJT-X. |
| `hash22(call)` | `nhash(call) & 0x3FFFFF` — FT8 / FT4 compound-call width. |
| `hash15(call)` | `nhash(call) & 0x7FFF` — WSPR Type 3 width. |
| `hash12(call)` | 12-bit mask (some ARRL contest variants). |
| `hash10(call)` | 10-bit mask (narrowest WSJT-X variant). |
| `CallHashTable` | Persistent accumulator + lookup + substitution. |
| `parse_message(message, table=None)` | Shared WSJT-X message-field parser (tx/rx/grid/report) **+ hash substitution** when a `table` is passed. The one entry point all recorders use. |

`CallHashTable` methods (the shared build / lookup / substitute surface):

| Method | Purpose |
|---|---|
| `observe(text)` | Scan decoded text for `<call>` markers; seed the table. |
| `add(call)` / `ingest_calls(calls)` | Seed from plaintext calls found in decodes. |
| `by_hash22/15/12(h)` | Reverse a hash → plaintext (None if unknown **or ambiguous**). |
| `resolve_token(tok)` | Resolve one token: `<NNNNNNN>`→call, `<...>`→None, `<CALL>`→strip+seed, bare→passthrough. |
| `resolve_message(msg)` | Substitute every resolvable hash token in a message, leave the rest verbatim. |
| `write_wsprd_hashtable(path, *, exclude=)` / `write_jt9_calls(path, grids=, *, exclude=)` | Seed wsprd / jt9 decoder files; `exclude` predicate lets wspr inject its wsprnet negative-cache filter. |

All public symbols are re-exported from `callhash/__init__.py`.

## Project structure

```
src/callhash/
  _nhash.py     # Bob Jenkins lookup3 port (final-block branches included).
  table.py      # CallHashTable: persistence, observe, lookup, substitution,
                #   collision guard, decoder-seed exporters.
  parse.py      # parse_message — shared WSJT-X message-field parser + resolve.
  __init__.py   # public API surface.
tests/          # ~89 tests; nhash bit-exactness + table observe/lookup/
                #   persistence/corruption recovery + collision guard +
                #   token/message resolution + parse_message.
```

## Correctness invariants

- `nhash` is **bit-exact** against WSJT-X's canonical
  `lib/wsprcode/nhash.c` (the unmasked variant — note that
  `lib/wsprd/nhash.c` has a 15-bit mask baked into its return). 23
  reference vectors verified, including 11 / 12 / 13-byte boundary
  cases that exercise distinct final-block branches in the C code.
- `CallHashTable` uses **atomic JSON persistence** (write tempfile +
  rename) and degrades safely on corrupt JSON / schema mismatch by
  rebuilding the table rather than refusing to start.
- Concurrent `observe()` / `by_hash22()` / `by_hash15()` from multiple
  threads is safe.
- **Collision guard.** A hash slot claimed by 2+ *distinct* calls is
  ambiguous, so `by_hashNN` returns `None` for it — we never return a
  guessed (wrong) call. This matters because our table is *persistent*
  and accumulates 10²–10³+ calls, so it sees far more collisions than
  WSJT-X's short-lived per-session table — especially in the 15-bit
  space (32 768 slots; ~1 000 calls ⇒ collisions effectively certain).
  A wrong call pollutes wsprnet / pskreporter; a missing call is benign.
  The ambiguous-slot set is **recomputed on load**, not persisted
  (`_index_call` is order-independent and idempotent).

When extending the library, the test suite expects:

1. Any change to `_nhash.py` keeps the bit-exactness vectors green.
   They are the contract between this library and decoded WSJT-X
   output — drift here means downstream consumers can't resolve
   hashes from announcement markers.
2. New `hashN` masks should follow the existing convention
   (`nhash(call) & mask`).

## Consumers

All three recorders depend on callhash (`callhash>=1.0.0`) via
`[tool.uv.sources]` editable path, and use the **same** build → lookup →
substitute path:

- `psk-recorder` (FT8/FT4) — `ch_tailer.py` feeds decoded text through
  `observe()` then `parse_message(line, table=...)`. Requires the
  patched `decode_ft8` that emits `<NNNNNNN>` (see ft8_lib fork);
  upstream `decode_ft8` discards the hash number as `<...>`, leaving
  nothing to resolve.
- `meteor-scatter` (MSK144) — same `ch_tailer.py` path; `jt9` is invoked
  with `-Y` so it emits `<NNNNNNN>`.
- `wspr-recorder` (WSPR-2 + FST4W) — `callsign_db.py` is a thin wrapper
  that composes a `CallHashTable` for all hashing/lookup, adds wspr-only
  grid/bands metadata + the wsprnet negative-cache filter (passed as the
  exporter `exclude=` predicate), and keeps its own JSON format at
  `/var/lib/wspr-recorder/callhash/wspr-callhash.json`.

## Library lockfile policy

`uv.lock` for libraries doesn't bind downstream consumers. Each
consumer pins callhash via its own `uv.lock` (and via
`[tool.uv.sources]` editable path during dev).
