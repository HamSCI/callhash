# callhash — Requirements Specification

**Status:** v0.1 baseline (retroactive). **Owner:** Michael Hauan (AC0G).
**Last reconciled against code:** callhash `1.1.0` (2026-06-25).
**Prefix:** `CLH`.

> Application of [sigmond/docs/REQUIREMENTS-TEMPLATE.md](https://github.com/HamSCI/sigmond/blob/main/docs/REQUIREMENTS-TEMPLATE.md)
> to a small, **mature** shared **library** (not a sigmond client). callhash
> has no systemd units, no config, no sink and no contract surface — so the
> [client contract](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md)
> **does not apply**; its "interface" is its public Python API (§8.3). Provenance
> tags: `[DOC]` documented · `[CODE]` implicit-in-code · `[NEW]` surfaced by this
> review. Status: ✅ implemented/verified · 🟡 partial/unverified · ⬜ planned.

## 1. Context & problem statement

WSJT-X's FT8/FT4/MSK144/FST4W and WSPR-2 packet formats are too small to carry a
full compound callsign (`K1ABC/QRP`, `VE3/K1ABC`), so they transmit a **hash** of
it — 22-bit for FT8/FT4/MSK144/FST4W, 15-bit for WSPR-2 Type 3 — and rely on the
receiver having seen the call announced in plaintext (`<K1ABC/QRP>`) earlier in
the session to map the hash back. The HamSCI recorders drive their decoders
**one cycle at a time**, so each invocation starts with an *empty* session table
and most hashed packets surface as the bare placeholder `<...>` — or, with the
patched decoders, as a numeric `<NNNNNNN>` that no one resolves. The compound
call is lost from the spot.

callhash is the **single source of truth for WSJT-X hash handling** across all
three recorders. It (a) ports WSJT-X's hash function — Bob Jenkins lookup3, seed
146 — bit-exactly, and (b) provides a persistent, per-station accumulator that
reconstructs the announcement table on the consumer side, so a compound call
learned on any mode/band resolves hashes everywhere. It is pure stdlib, no
runtime dependencies, ~200 lines — small enough to live cleanly in its own repo
and be reused by any FT8/FT4/WSPR consumer, not just sigmond.

## 2. Goals & objectives

- Reproduce WSJT-X's `nhash` (lookup3, seed 146) **bit-exactly** so library
  hashes match decoder-emitted hashes.
- Resolve hashed callsigns (`<NNNNNNN>`/`<...>`) back to plaintext from the same
  `<call>` announcement markers WSJT-X uses.
- Be the **one** shared build/lookup/substitute mechanism for psk-recorder,
  meteor-scatter, and wspr-recorder so all three behave identically.
- **Never emit a wrong call:** a collided (ambiguous) hash resolves to nothing,
  not to a guess.
- Survive process restarts (persistent table) and operator/corruption mishaps
  without refusing to start.
- Stay dependency-free and embeddable (no import graph, no service).

## 3. Non-goals / out of scope

- **Decoding RF / running jt9 or wsprd** — callhash only consumes already-decoded
  text and seeds decoder files. (Owner: the recorders + their decoders.)
- **Being a sigmond client** — no systemd unit, no `deploy.toml`, no
  `inventory`/`validate`, no contract conformance. It is a `[tool.uv.sources]`
  source-only dependency in the catalog, never installed as a unit.
- **Sink / network I/O** — it writes only its own JSON table and decoder-seed
  files; uploading spots is the recorder's job.
- **Storing per-call metadata** (grids, bands, wsprnet negative-cache policy) —
  callers own that; the library exposes an `exclude=` hook and stays oblivious.
- **Callsign validation as a service** — `_looks_like_callsign` is a loose sanity
  filter, not an authoritative ITU validator.

## 4. Stakeholders & actors

The **consumers are the stakeholders** (this is a library): `psk-recorder`
(FT8/FT4, via `ch_tailer.py` → `observe` + `parse_message`), `meteor-scatter`
(MSK144, same path, `jt9 -Y`), and `wspr-recorder` (WSPR-2 + FST4W, via
`callsign_db.py` wrapper, adds grids + the wsprnet negative-cache `exclude=`
filter). Upstream reference: **WSJT-X** `lib/wsprcode/nhash.c` (the hash
contract). Indirect: the decoders themselves (`decode_ft8` fork emitting
`<NNNNNNN>`, `jt9 -Y`, `wsprd`) and the operator (owns the on-disk JSON table).

## 5. Assumptions & constraints

- `CLH-C-001` `[DOC]` ✅ The library SHALL be **pure stdlib** with no runtime
  dependencies (`pyproject` `dependencies = []`); dev-only `pytest`.
- `CLH-C-002` `[CODE]` ✅ Python ≥3.10 (`requires-python`), `src/` layout,
  setuptools build; consumers pin `callhash>=1.0.0` via `[tool.uv.sources]`
  editable path (HamSCI single-source-tree pattern).
- `CLH-C-003` `[DOC]` ✅ The 22/15/12/10-bit values SHALL all derive from the
  identical unmasked `nhash`; only the truncation mask differs.
- `CLH-C-004` `[CODE]` ✅ Numeric bracket tokens (`<NNNNNNN>`) SHALL be treated as
  **22-bit** hashes only — no decoder driven by the suite emits a narrower width
  as a number.
- `CLH-C-005` `[CODE]` ✅ The table SHALL stay embeddable: in-memory maps only, no
  import graph beyond stdlib, safe to construct inside either consumer.

## 6. Functional requirements

### 6.1 Hash function (`_nhash`)
- `CLH-F-001` `[DOC]` ✅ SHALL compute Bob Jenkins lookup3 with `WSJTX_INITVAL=146`,
  returning a 32-bit unsigned int, **bit-exact** against WSJT-X's canonical
  `lib/wsprcode/nhash.c` (the unmasked variant) for ASCII inputs.
- `CLH-F-002` `[CODE]` ✅ SHALL correctly emulate the C final-block `switch`
  branches (1–4 / 5–8 / 9–11 byte tails) without reading past the buffer end;
  zero-length input returns the seeded `c`.
- `CLH-F-003` `[DOC]` ✅ SHALL expose `hash22`/`hash15`/`hash12`/`hash10` as
  `nhash(call) & MASK{22,15,12,10}` and re-export the masks + `WSJTX_INITVAL`.
- `CLH-F-004` `[CODE]` ✅ SHALL accept `bytes` or `str` keys (str encoded ASCII).

### 6.2 Observation / build (`CallHashTable`)
- `CLH-F-010` `[DOC]` ✅ `observe(text)` SHALL extract every `<call>` /
  `<call/suffix>` marker from decoded text, callsign-filter it, index it into all
  three width-keyed maps, and return the count of NEW calls.
- `CLH-F-011` `[CODE]` ✅ SHALL ignore the literal `<...>` placeholder and
  non-callsign-shaped bracket content during observation.
- `CLH-F-012` `[DOC]` ✅ SHALL offer `add(call)` and `ingest_calls(calls)` for
  consumers that discover bare plaintext calls (e.g. wspr per-cycle spot ingest).
- `CLH-F-013` `[CODE]` ✅ SHALL record a UTC `first_seen` timestamp per new call.

### 6.3 Lookup & collision guard
- `CLH-F-020` `[DOC]` ✅ `by_hash22/15/12(h)` SHALL mask the input to width and
  return the plaintext call, or `None` if unknown.
- `CLH-F-021` `[DOC]` ✅ **Collision guard:** a hash slot claimed by 2+ *distinct*
  calls SHALL resolve to `None` (never a guessed call); the ambiguous-slot sets
  SHALL be recomputed on load (idempotent, order-independent), not persisted.

### 6.4 Token / message resolution & parsing
- `CLH-F-030` `[DOC]` ✅ `resolve_token` SHALL map `<NNNNNNN>`→`by_hash22`,
  `<...>`→`None`, `<CALL>`→strip-brackets-and-self-seed, bare token→passthrough,
  bracketed garbage→`None`.
- `CLH-F-031` `[DOC]` ✅ `resolve_message` SHALL substitute every resolvable token
  and leave unresolvable ones verbatim (normalising whitespace to single spaces).
- `CLH-F-032` `[DOC]` ✅ `parse_message(msg, table=)` SHALL be the single shared
  WSJT-X field parser: resolve hashes first (when a table is given), then
  best-effort extract `tx_call`/`rx_call`/`grid`/`report`, always returning the
  hash-resolved `message` string for verbatim storage.
- `CLH-F-033` `[CODE]` ✅ `normalise_brackets` SHALL canonicalise a single
  possibly-bracketed token (`<CALL>`→`CALL`, `<...>`→`None`, else as-is).

### 6.5 Persistence
- `CLH-F-040` `[DOC]` ✅ `load_or_new(path)` SHALL return a populated table from
  JSON when present and schema-matching, else a fresh table — **never** deleting
  the operator's file, logging a warning on unreadable/corrupt/schema-mismatch.
- `CLH-F-041` `[DOC]` ✅ `save()` SHALL persist **atomically** (write `.tmp` then
  `os.replace`) and SHALL no-op when the table is unchanged (`_dirty`).
- `CLH-F-042` `[CODE]` ✅ The on-disk JSON SHALL carry `schema_version` (currently
  1), `saved_at`, `observations`, and a `{call: first_seen}` map.

### 6.6 Decoder-seed export
- `CLH-F-050` `[DOC]` ✅ `write_wsprd_hashtable(path, *, exclude=)` SHALL emit
  wsprd's `hashtable.txt` (`"%5d %s"`, 15-bit index → call) so wsprd can resolve
  Type-3 hashes it would otherwise drop.
- `CLH-F-051` `[DOC]` ✅ `write_jt9_calls(path, grids=, *, exclude=)` SHALL emit
  jt9's `fst4w_calls.txt` (call + grid, blank grid = four spaces).
- `CLH-F-052` `[DOC]` ✅ Both exporters SHALL honour an optional `exclude(call)`
  predicate (the wsprnet negative-cache hook) while the library itself stays
  policy-free; each returns the count written.

## 7. Quality / non-functional requirements

- `CLH-Q-001` `[DOC]` ✅ **Correctness-critical:** `nhash` bit-exactness is the
  contract between library and decoder; any `_nhash` change SHALL keep the
  reference vectors (23 vectors, incl. 11/12/13-byte boundaries) green.
- `CLH-Q-002` `[DOC]` ✅ **No-wrong-call invariant:** the library SHALL prefer a
  missing call over a wrong one (collision guard, `CLH-F-021`) — a wrong call
  pollutes wsprnet/pskreporter; a missing call is benign.
- `CLH-Q-003` `[DOC]` ✅ Concurrent `observe()` / `by_hash*()` from multiple
  threads SHALL be safe (single internal `threading.Lock`).
- `CLH-Q-004` `[DOC]` ✅ Persistence SHALL be crash-safe (atomic replace) and
  fault-tolerant: corrupt/half-written JSON degrades to a fresh table, never a
  startup failure.
- `CLH-Q-005` `[CODE]` ✅ Hash resolution SHALL be O(1) per lookup (width-keyed
  dict + collided-set membership), trivial for the observed 10²–10³ table sizes.

## 8. External interfaces

### 8.1 Inputs
- Decoded WSJT-X text lines (from the consumer's `ch_tailer.py` / decoder output)
  containing `<call>` / `<NNNNNNN>` / `<...>` tokens.
- A per-station JSON table file path (consumer-chosen, e.g.
  `/var/lib/wspr-recorder/callhash/wspr-callhash.json`).
- Optional `{call: grid}` map and `exclude(call)` predicate from the consumer.

### 8.2 Outputs
- Reverse-resolved plaintext calls / hash-resolved message strings (return
  values).
- Parsed field dict: `message`, `tx_call`, `rx_call`, `grid`, `report`.
- Persisted JSON table (`schema_version`, `saved_at`, `observations`, `calls`).
- Decoder-seed files: wsprd `hashtable.txt`, jt9 `fst4w_calls.txt`.

### 8.3 Interface = public Python API (no client contract)
- `CLH-I-001` `[DOC]` ✅ The **public API IS the interface** — there is no sigmond
  client contract surface (no `inventory`/`validate`/units/sink). Exported from
  `callhash/__init__.py`: `nhash`, `hash22`/`hash15`/`hash12`/`hash10`, the four
  masks + `WSJTX_INITVAL`, `CallHashTable`, `parse_message`.
- `CLH-I-002` `[CODE]` ✅ `CallHashTable` public surface: `observe`, `add`,
  `ingest_calls`, `by_hash22/15/12`, `resolve_token`, `resolve_message`,
  `normalise_brackets`, `load_or_new`/`save`, `write_wsprd_hashtable`/
  `write_jt9_calls`, `stats`/`calls`/`observations`, `__contains__`/`__len__`.
- `CLH-I-003` `[DOC]` ✅ Consumed as a `[tool.uv.sources]` editable sibling
  (`callhash>=1.0.0`); declared in sigmond's catalog as a **source-only**
  dependency, never a managed unit.

## 9. Data requirements

In-memory: three width-keyed `{hash: call}` maps + three collided-slot sets +
`{call: first_seen}` + observation counter. On-disk JSON (`schema_version=1`):
`{schema_version, saved_at, observations, calls:{call: ISO-8601-first_seen}}` —
hashes and collided sets are **derived on load**, not stored. Volume is small
(10²–10³ calls per station); retention is operator-managed (the file is never
auto-evicted; bounded eviction noted as a future use of `first_seen`).

## 10. Dependencies & development sequence

**Deps:** none at runtime (pure stdlib); `pytest` for dev only. Upstream
reference: WSJT-X `nhash.c` (the bit-exactness anchor). Downstream: the three
recorders pin it editable.

**Development sequence (intended, recovered):** (1) port `nhash` bit-exactly and
lock it with cross-validated C vectors → (2) `CallHashTable` observe/lookup +
atomic persistence → (3) collision guard (forced by the *persistent* table
accumulating far more calls than WSJT-X's per-session one, esp. in 15-bit space)
→ (4) extract the copy-pasted per-recorder field parser into shared
`parse_message` + decoder-seed exporters, making the library the single source of
truth. Maturity is reflected by the near-total `✅`/`[DOC]` mix.

## 11. Acceptance criteria & verification

- Hash correctness → `tests/test_nhash.py` (23 reference vectors, boundary
  cases) — the bit-exactness gate.
- Table behaviour → `tests/test_table.py`: observe extraction, width lookups,
  collision guard returns `None`, token/message resolution, `parse_message`
  fields, atomic save, corrupt-JSON / schema-mismatch recovery, concurrent
  observe/lookup. (~89 checks documented across the two files / 58 test
  functions incl. parametrisation; runner: `uv run pytest tests/`.)
- Integration → the three recorders resolve compound calls identically through
  `observe` + `parse_message` (cross-mode pollination verified at the consumer).

## 12. Risks & open questions

- `CLH-D-001` `[NEW]` 🟡 **Test-count doc drift:** README/CLAUDE.md state "~89
  tests" while the two files define 58 `def test_*` functions (the rest are
  parametrised vectors). Harmless, but the wording SHALL be reconciled to one
  honest figure. *(candidate low-priority issue.)*
- `CLH-Q-006` `[NEW]` ⬜ **Unbounded growth:** the persistent table never evicts;
  `first_seen` is recorded "for future bounded eviction" but no policy exists. At
  multi-year station scale the 15-bit collision rate approaches saturation
  (~1000 calls already makes 15-bit collisions effectively certain). SHALL define
  an eviction or split-by-station policy before the table degrades 15-bit
  resolution materially.
- Version note: `pyproject` is at `1.1.0` while consumers pin `>=1.0.0` — intended
  floor, no action.

## 13. Traceability

| Requirement | #18 issue | Verification | PSWS #6 |
|---|---|---|---|
| CLH-F-001 (nhash bit-exact) | — | test_nhash.py (23 vectors) | — |
| CLH-F-021 (collision guard) | — | test_table.py collision cases | — |
| CLH-F-032 (shared parse_message) | Clients: psk/meteor/wspr hash resolve | recorder integration | #6:31 (sensor integ.) |
| CLH-F-040/041 (atomic persistence) | — | corrupt-JSON / atomic-save tests | — |
| CLH-D-001 (test-count drift) | *(new — file)* | doc reconcile | — |
| CLH-Q-006 (unbounded growth) | *(new — file)* | eviction policy + test | — |

*New rows (CLH-D-001, CLH-Q-006) are this review's surfaced gaps; both are
low-priority for a mature small library.*
