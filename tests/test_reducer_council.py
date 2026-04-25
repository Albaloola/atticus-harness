from __future__ import annotations

from atticus.reducer.council import reduce_votes


def test_reduce_votes_accepts_unique_majority_independent_of_order():
    first = reduce_votes(
        [
            {"role": "hostile", "vote": "accept", "candidate_id": "cand-b"},
            {"role": "evidence", "vote": "accept", "candidate_id": "cand-a"},
            {"role": "draft", "vote": "accept", "candidate_id": "cand-a"},
        ]
    )
    second = reduce_votes(
        [
            {"role": "draft", "vote": "accept", "candidate_id": "cand-a"},
            {"role": "hostile", "vote": "accept", "candidate_id": "cand-b"},
            {"role": "evidence", "vote": "accept", "candidate_id": "cand-a"},
        ]
    )

    assert first.decision == "accept"
    assert first.selected_candidate_id == "cand-a"
    assert second.selected_candidate_id == "cand-a"


def test_reduce_votes_needs_human_attention_on_tie():
    decision = reduce_votes(
        [
            {"role": "evidence", "vote": "accept", "candidate_id": "cand-a"},
            {"role": "authority", "vote": "accept", "candidate_id": "cand-b"},
        ]
    )

    assert decision.decision == "needs_human_attention"
    assert decision.selected_candidate_id is None
    assert "tie" in decision.rationale


def test_reduce_votes_blocks_on_explicit_reject():
    decision = reduce_votes(
        [
            {"role": "evidence", "vote": "accept", "candidate_id": "cand-a"},
            {"role": "hostile", "vote": "reject", "candidate_id": "cand-a"},
        ]
    )

    assert decision.decision == "blocked"
    assert decision.selected_candidate_id is None
    assert "rejected" in decision.rationale
