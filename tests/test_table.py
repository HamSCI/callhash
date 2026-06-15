"""Tests for ``callhash.CallHashTable``.

Covers:
  * `<call>` announcement parsing from decoded text — single, multi,
    bracketed-with-suffix, the literal `<...>` placeholder.
  * Hash-based lookups (h22 + h15) round-trip.
  * `normalise_brackets` static helper for token-level cleanup.
  * Persistence: load → observe → save → reload → state preserved.
  * Schema-version mismatch and corrupt-JSON cases start fresh
    without losing the operator's file.
  * Thread safety: concurrent observe + lookup doesn't corrupt.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from callhash import CallHashTable, hash12, hash15, hash22, parse_message


class TestObserve:

    def test_extracts_single_announcement(self):
        t = CallHashTable()
        added = t.observe("260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> CQ FT8")
        assert added == 1
        assert "K1ABC/QRP" in t
        assert t.by_hash22(hash22("K1ABC/QRP")) == "K1ABC/QRP"
        assert t.by_hash15(hash15("K1ABC/QRP")) == "K1ABC/QRP"

    def test_multiple_announcements_in_one_line(self):
        # Both ends of a QSO each in brackets — typical of an
        # exchange where both calls are non-standard.
        t = CallHashTable()
        added = t.observe(
            "260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> <VE3/W1XYZ> 73 FT8"
        )
        assert added == 2
        assert "K1ABC/QRP" in t and "VE3/W1XYZ" in t

    def test_duplicate_announcement_only_adds_once(self):
        t = CallHashTable()
        first  = t.observe("foo <K1ABC/QRP> bar")
        second = t.observe("foo <K1ABC/QRP> baz")
        assert first == 1
        assert second == 0          # already known
        assert len(t) == 1
        # observations counter still reflects both sightings:
        assert t.observations == 2

    def test_literal_unresolved_placeholder_is_skipped(self):
        t = CallHashTable()
        added = t.observe("260507 1234 -12 +0.45 1250 ` <...> 73 FT8")
        assert added == 0
        assert len(t) == 0

    def test_bracketed_standard_call_still_extracted(self):
        # Even a standard non-compound call may appear in brackets
        # when the decoder resolved it from a hash.  We treat that
        # exactly like an announcement and store it.
        t = CallHashTable()
        added = t.observe("260507 1234 -12 +0.45 1250 ` <K1JT> CQ FT8")
        assert added == 1
        assert "K1JT" in t

    def test_garbage_and_lowercase_not_treated_as_calls(self):
        t = CallHashTable()
        added = t.observe("foo <hello> <abc!@#> bar")
        assert added == 0
        assert len(t) == 0


class TestNormaliseBrackets:

    def test_strips_brackets_around_call(self):
        assert CallHashTable.normalise_brackets("<K1ABC>") == "K1ABC"

    def test_strips_brackets_around_compound_call(self):
        assert CallHashTable.normalise_brackets("<K1ABC/QRP>") == "K1ABC/QRP"

    def test_unresolved_returns_none(self):
        assert CallHashTable.normalise_brackets("<...>") is None

    def test_passthrough_for_non_bracketed(self):
        assert CallHashTable.normalise_brackets("K1ABC") == "K1ABC"
        assert CallHashTable.normalise_brackets("CQ") == "CQ"

    def test_handles_whitespace(self):
        assert CallHashTable.normalise_brackets("  <K1ABC>  ") == "K1ABC"


class TestLookup:

    def test_lookup_with_extraneous_high_bits_masks_correctly(self):
        """by_hash22 should mask its argument to 22 bits before lookup."""
        t = CallHashTable()
        t.add("K1ABC")
        h = hash22("K1ABC")
        # Same lookup works whether the caller passes a clean 22-bit
        # value or a 32-bit superset (e.g. the unmasked nhash output).
        assert t.by_hash22(h) == "K1ABC"
        assert t.by_hash22(h | 0xFFC00000) == "K1ABC"

    def test_unknown_hash_returns_none(self):
        t = CallHashTable()
        t.add("K1ABC")
        assert t.by_hash22(0xDEADBE & 0x3FFFFF) is None


class TestPersistence:

    def test_save_then_load_roundtrip(self, tmp_path):
        path = tmp_path / "hashtable.json"
        t = CallHashTable.load_or_new(path)
        t.observe("foo <K1ABC/QRP> bar")
        t.observe("foo <VE3/W1XYZ> bar")
        t.save()
        assert path.exists()

        # Fresh load — same content.
        t2 = CallHashTable.load_or_new(path)
        assert "K1ABC/QRP" in t2
        assert "VE3/W1XYZ" in t2
        assert t2.by_hash22(hash22("K1ABC/QRP")) == "K1ABC/QRP"
        assert t2.observations == 2

    def test_save_no_op_when_unchanged(self, tmp_path):
        """Saving a clean table should be a no-op (no file write)."""
        path = tmp_path / "hashtable.json"
        # Initial save with one call.
        t = CallHashTable.load_or_new(path)
        t.observe("<K1ABC/QRP>")
        t.save()
        mtime_initial = path.stat().st_mtime_ns

        # Reload (clean) and save again — file shouldn't be rewritten.
        t2 = CallHashTable.load_or_new(path)
        t2.save()
        assert path.stat().st_mtime_ns == mtime_initial

    def test_atomic_write_uses_tempfile(self, tmp_path):
        """save() writes via .tmp + replace; no half-written file."""
        path = tmp_path / "hashtable.json"
        t = CallHashTable.load_or_new(path)
        t.observe("<K1ABC/QRP>")
        t.save()
        # After save, the .tmp file should NOT exist (renamed away).
        assert not (tmp_path / "hashtable.json.tmp").exists()
        assert path.exists()
        # And the JSON parses.
        data = json.loads(path.read_text())
        assert data["schema_version"] == 1
        assert "K1ABC/QRP" in data["calls"]

    def test_corrupt_json_starts_fresh_without_deleting(self, tmp_path):
        path = tmp_path / "hashtable.json"
        path.write_text("{not valid json")
        t = CallHashTable.load_or_new(path)
        assert len(t) == 0
        # Operator's file is left alone (not auto-deleted).
        assert path.read_text() == "{not valid json"

    def test_schema_mismatch_starts_fresh(self, tmp_path):
        path = tmp_path / "hashtable.json"
        path.write_text(json.dumps({
            "schema_version": 99,
            "calls": {"K1ABC/QRP": "2025-01-01T00:00:00+00:00"},
            "observations": 1,
        }))
        t = CallHashTable.load_or_new(path)
        assert len(t) == 0
        # Saving doesn't clobber yet — would only fire if dirty.

    def test_save_without_path_raises(self):
        t = CallHashTable()
        with pytest.raises(ValueError, match="no path supplied"):
            t.save()


class TestThreadSafety:

    def test_concurrent_observe_and_lookup(self):
        """Hammer the table from multiple threads; assert no corruption."""
        t = CallHashTable()
        calls = [f"K{i:04d}AA" for i in range(50)]
        # Pre-add a couple so lookups have something to find.
        for c in calls[:5]:
            t.add(c)

        stop = threading.Event()
        errors: list[str] = []

        def writer():
            for c in calls:
                if stop.is_set():
                    return
                t.observe(f"<{c}>")

        def reader():
            while not stop.is_set():
                for c in calls[:5]:
                    if t.by_hash22(hash22(c)) != c:
                        errors.append(f"lookup miss: {c}")
                        return

        threads = [
            threading.Thread(target=writer, daemon=True),
            threading.Thread(target=writer, daemon=True),
            threading.Thread(target=reader, daemon=True),
        ]
        for th in threads:
            th.start()
        # Let them race.
        threads[0].join(timeout=2.0)
        threads[1].join(timeout=2.0)
        stop.set()
        threads[2].join(timeout=2.0)

        assert not errors, errors
        # All 50 calls should be in the table.
        assert len(t) == 50


class TestCollisionGuard:
    """A hash slot claimed by 2+ distinct calls is ambiguous and must
    resolve to None — never a guessed (wrong) call."""

    # Deterministic collision pairs (found by brute force over the real
    # nhash, seed 146):
    #   * KA0BL / KA0CS  share hash15 == 2570 but DIFFER in hash22.
    #   * KA05O / KA1XF  share hash22 == 2827543.
    H15_PAIR = ("KA0BL", "KA0CS")
    H22_PAIR = ("KA05O", "KA1XF")

    def test_15bit_collision_blocks_resolution(self):
        a, b = self.H15_PAIR
        assert hash15(a) == hash15(b)        # precondition
        t = CallHashTable()
        t.add(a)
        t.add(b)
        # Ambiguous 15-bit slot → neither resolves.
        assert t.by_hash15(hash15(a)) is None
        # But both calls are still KNOWN (we just can't reverse the hash).
        assert a in t and b in t

    def test_15bit_collision_does_not_poison_22bit(self):
        a, b = self.H15_PAIR
        assert hash22(a) != hash22(b)        # distinct in 22-bit
        t = CallHashTable()
        t.add(a)
        t.add(b)
        # 22-bit space is unaffected — each resolves cleanly.
        assert t.by_hash22(hash22(a)) == a
        assert t.by_hash22(hash22(b)) == b

    def test_22bit_collision_blocks_resolution(self):
        a, b = self.H22_PAIR
        assert hash22(a) == hash22(b)
        t = CallHashTable()
        t.add(a)
        t.add(b)
        assert t.by_hash22(hash22(a)) is None

    def test_non_colliding_call_still_resolves(self):
        t = CallHashTable()
        t.add("K1ABC")
        assert t.by_hash22(hash22("K1ABC")) == "K1ABC"

    def test_collision_recomputed_on_reload(self, tmp_path):
        a, b = self.H22_PAIR
        path = tmp_path / "hashtable.json"
        t = CallHashTable.load_or_new(path)
        t.add(a)
        t.add(b)
        t.save()
        # Fresh load must rebuild the ambiguous-slot set from disk.
        t2 = CallHashTable.load_or_new(path)
        assert t2.by_hash22(hash22(a)) is None
        assert a in t2 and b in t2

    def test_resolve_token_skips_collided_hash(self):
        a, b = self.H22_PAIR
        t = CallHashTable()
        t.add(a)
        t.add(b)
        # The numeric hash both share resolves to nothing → message keeps
        # the placeholder rather than fabricating a call.
        token = f"<{hash22(a):07d}>"
        assert t.resolve_token(token) is None
        assert t.resolve_message(f"AC0G {token} 73") == f"AC0G {token} 73"


class TestResolveToken:

    def test_numeric_hash_resolves_after_announcement(self):
        t = CallHashTable()
        t.observe("<PJ4/K1ABC> CQ")
        h = hash22("PJ4/K1ABC")
        assert t.resolve_token(f"<{h:07d}>") == "PJ4/K1ABC"

    def test_numeric_hash_unknown_returns_none(self):
        t = CallHashTable()
        # never announced → can't recover
        assert t.resolve_token("<1234567>") is None

    def test_literal_unresolved_returns_none(self):
        assert CallHashTable().resolve_token("<...>") is None

    def test_bracketed_call_is_stripped_and_seeded(self):
        t = CallHashTable()
        assert t.resolve_token("<VE3/W1XYZ>") == "VE3/W1XYZ"
        # the sighting seeds the table so the hash now resolves
        assert "VE3/W1XYZ" in t
        assert t.by_hash22(hash22("VE3/W1XYZ")) == "VE3/W1XYZ"

    def test_bare_token_passthrough(self):
        t = CallHashTable()
        assert t.resolve_token("K1ABC") == "K1ABC"
        assert t.resolve_token("EM38") == "EM38"

    def test_bracketed_garbage_returns_none(self):
        assert CallHashTable().resolve_token("<!!!>") is None


class TestResolveMessage:

    def test_substitutes_known_hash_leaves_rest(self):
        t = CallHashTable()
        t.observe("<PJ4/K1ABC> CQ")
        h = hash22("PJ4/K1ABC")
        msg = f"AC0G <{h:07d}> -12"
        assert t.resolve_message(msg) == f"AC0G PJ4/K1ABC -12"

    def test_unknown_hash_preserved_verbatim(self):
        t = CallHashTable()
        msg = "AC0G <1234567> -12"
        # nothing learned → leave the placeholder so we don't fabricate
        assert t.resolve_message(msg) == "AC0G <1234567> -12"

    def test_literal_placeholder_preserved(self):
        t = CallHashTable()
        assert t.resolve_message("CQ <...>") == "CQ <...>"

    def test_empty(self):
        assert CallHashTable().resolve_message("") == ""


class TestByHash12:

    def test_h12_lookup_roundtrip(self):
        t = CallHashTable()
        t.add("K1ABC/QRP")
        assert t.by_hash12(hash12("K1ABC/QRP")) == "K1ABC/QRP"
        assert t.by_hash12(0xABC & 0xFFF) in (None, "K1ABC/QRP")


class TestIngestCalls:

    def test_ingest_adds_new_and_counts(self):
        t = CallHashTable()
        added = t.ingest_calls(["K1ABC", "VE3/W1XYZ", "K1ABC", "garbage!!"])
        assert added == 2
        assert "K1ABC" in t and "VE3/W1XYZ" in t
        assert t.by_hash22(hash22("VE3/W1XYZ")) == "VE3/W1XYZ"


class TestDecoderExport:

    def test_write_wsprd_hashtable_format_and_exclude(self, tmp_path):
        t = CallHashTable()
        t.ingest_calls(["K1ABC", "W4UK/P"])
        path = tmp_path / "hashtable.txt"
        n = t.write_wsprd_hashtable(path, exclude=lambda c: c == "W4UK/P")
        assert n == 1
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        idx, call = lines[0].split()
        assert call == "K1ABC"
        assert int(idx) == hash15("K1ABC")

    def test_write_jt9_calls_with_grids_and_blank(self, tmp_path):
        t = CallHashTable()
        t.ingest_calls(["K1ABC", "AC0G"])
        path = tmp_path / "fst4w_calls.txt"
        n = t.write_jt9_calls(path, grids={"K1ABC": "FN42"})
        assert n == 2
        body = path.read_text()
        assert "K1ABC FN42\n" in body
        assert "AC0G     \n" in body          # blank grid → 4 spaces


class TestParseMessage:

    def test_cq_with_grid(self):
        out = parse_message("CQ K1ABC FN42")
        assert out["tx_call"] == "K1ABC"
        assert out["grid"] == "FN42"

    def test_exchange_with_report(self):
        out = parse_message("AC0G K1ABC R-12")
        assert out["rx_call"] == "AC0G"
        assert out["tx_call"] == "K1ABC"
        assert out["report"] == -12

    def test_resolves_hash_via_table(self):
        t = CallHashTable()
        t.observe("<PJ4/K1ABC> CQ")
        h = hash22("PJ4/K1ABC")
        out = parse_message(f"AC0G <{h:07d}> 73", table=t)
        assert out["rx_call"] == "AC0G"
        assert out["tx_call"] == "PJ4/K1ABC"
        assert out["message"] == "AC0G PJ4/K1ABC 73"

    def test_unresolved_hash_drops_call_keeps_message(self):
        t = CallHashTable()
        out = parse_message("AC0G <1234567> 73", table=t)
        assert out["rx_call"] == "AC0G"
        assert out["tx_call"] == ""          # unrecoverable → empty, not "<1234567>"
        assert out["message"] == "AC0G <1234567> 73"   # raw preserved

    def test_no_table_strips_resolved_brackets_path(self):
        # Without a table, a bracketed call stays bracketed → dropped
        # from call fields but message preserved.
        out = parse_message("CQ <...>")
        assert out["tx_call"] == ""
        assert out["message"] == "CQ <...>"


class TestStats:

    def test_stats_snapshot(self):
        t = CallHashTable()
        t.observe("<K1ABC/QRP> <VE3/W1XYZ>")
        t.observe("<K1ABC/QRP> 73")     # duplicate → adds an observation, not a call
        unique, observations, h22_entries = t.stats()
        assert unique == 2
        assert observations == 3
        assert h22_entries == 2

    def test_calls_returns_sorted_snapshot(self):
        t = CallHashTable()
        t.observe("<VE3/W1XYZ> <K1ABC/QRP> <AC0G>")
        assert t.calls() == ["AC0G", "K1ABC/QRP", "VE3/W1XYZ"]
