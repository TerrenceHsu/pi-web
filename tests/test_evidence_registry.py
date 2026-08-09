"""P2-R4-B1 — EvidenceRegistry unit tests.

Covers directive §58 (Registry unit matrix) + §59 (concurrent isolation).
"""
from __future__ import annotations

from pi_agent_core_py.web.knowledge.evidence import EvidenceRegistry
from pi_agent_core_py.web.knowledge.search_models import KnowledgeEvidence


def _register_hit(
    registry: EvidenceRegistry,
    *,
    chunk_id: str = "chunk_a",
    document_id: str = "doc_a",
    rank: float = -1.0,
) -> KnowledgeEvidence:
    return registry.register(
        document_id=document_id,
        chunk_id=chunk_id,
        source_filename="test.pdf",
        heading_path=("Intro",),
        page_start=1,
        page_end=2,
        content="some content",
        rank=rank,
    )


class TestEvidenceRegistry:
    def test_initial_empty(self):
        r = EvidenceRegistry()
        assert len(r) == 0
        assert r.all_evidence == ()

    def test_first_evidence_is_E1(self):
        r = EvidenceRegistry()
        ev = _register_hit(r, chunk_id="c1")
        assert ev.evidence_id == "E1"
        assert len(r) == 1

    def test_second_evidence_is_E2(self):
        r = EvidenceRegistry()
        _register_hit(r, chunk_id="c1")
        ev2 = _register_hit(r, chunk_id="c2")
        assert ev2.evidence_id == "E2"

    def test_same_chunk_id_returns_same_evidence(self):
        r = EvidenceRegistry()
        ev1 = _register_hit(r, chunk_id="c1", rank=-1.0)
        ev2 = _register_hit(r, chunk_id="c1", rank=-0.5)
        assert ev1.evidence_id == ev2.evidence_id == "E1"
        assert len(r) == 1

    def test_first_seen_snapshot_wins(self):
        """Same chunk_id with different rank — first-seen rank preserved."""
        r = EvidenceRegistry()
        ev1 = _register_hit(r, chunk_id="c1", rank=-1.0)
        ev2 = _register_hit(r, chunk_id="c1", rank=-0.5)
        assert ev1.rank == -1.0
        assert ev2.rank == -1.0  # first-seen snapshot, not -0.5

    def test_lookup_existing(self):
        r = EvidenceRegistry()
        _register_hit(r, chunk_id="c1")
        ev = r.lookup("E1")
        assert ev is not None
        assert ev.chunk_id == "c1"

    def test_lookup_unknown_returns_none(self):
        r = EvidenceRegistry()
        assert r.lookup("E999") is None

    def test_all_evidence_in_registration_order(self):
        r = EvidenceRegistry()
        _register_hit(r, chunk_id="c3")
        _register_hit(r, chunk_id="c1")
        _register_hit(r, chunk_id="c2")
        ids = [e.evidence_id for e in r.all_evidence]
        assert ids == ["E1", "E2", "E3"]

    def test_many_ids(self):
        r = EvidenceRegistry()
        for i in range(10):
            _register_hit(r, chunk_id=f"c{i}")
        assert len(r) == 10
        assert r.all_evidence[-1].evidence_id == "E10"

    def test_new_registry_resets_to_E1(self):
        r1 = EvidenceRegistry()
        _register_hit(r1, chunk_id="c1")
        _register_hit(r1, chunk_id="c2")
        r2 = EvidenceRegistry()
        ev = _register_hit(r2, chunk_id="c1")
        assert ev.evidence_id == "E1"

    def test_concurrent_isolation(self):
        """Two independent registries — each starts from E1."""
        r_a = EvidenceRegistry()
        r_b = EvidenceRegistry()
        ev_a = _register_hit(r_a, chunk_id="cA")
        ev_b = _register_hit(r_b, chunk_id="cB")
        assert ev_a.evidence_id == "E1"
        assert ev_b.evidence_id == "E1"
        assert ev_a.chunk_id == "cA"
        assert ev_b.chunk_id == "cB"

    def test_dedupe_across_multiple_searches_same_turn(self):
        """Simulate: search #1 returns c1, search #2 returns c1 again."""
        r = EvidenceRegistry()
        _register_hit(r, chunk_id="c1")
        _register_hit(r, chunk_id="c2")
        _register_hit(r, chunk_id="c3")
        # Second search returns c2 (already seen) + c4 (new)
        ev2_again = _register_hit(r, chunk_id="c2")
        ev4 = _register_hit(r, chunk_id="c4")
        assert ev2_again.evidence_id == "E2"  # reuse
        assert ev4.evidence_id == "E4"  # new
        assert len(r) == 4
