from __future__ import annotations

import pytest

from atticus.testing.bad_fixtures import all_bad_fixtures, bad_worker_packet_fixtures
from atticus.workers.result_parser import ResultPacketError, parse_result


def test_bad_fixture_catalog_documents_expected_outcomes() -> None:
    fixtures = all_bad_fixtures()

    assert len(fixtures) >= 15
    assert {fixture.expected_outcome for fixture in fixtures} == {"reject", "repair", "operator_attention"}
    for fixture in fixtures:
        assert fixture.fixture_id
        assert fixture.category
        assert fixture.reason
        assert fixture.payload


@pytest.mark.parametrize("fixture", bad_worker_packet_fixtures(), ids=lambda fixture: fixture.fixture_id)
def test_worker_packet_bad_fixtures_are_rejected_or_flagged(fixture) -> None:
    if fixture.expected_outcome != "reject":
        return

    with pytest.raises(ResultPacketError):
        _ = parse_result(
            fixture.payload,
            allowed_citation_targets=fixture.allowed_citation_targets,
            proof_citation_targets=fixture.proof_citation_targets,
        )


def test_fixture_catalog_keeps_live_failure_categories_visible() -> None:
    categories = {fixture.category for fixture in all_bad_fixtures()}

    assert {
        "provider_control_plane",
        "provider_transient",
        "worker_contract",
        "context_budget",
        "reuse_staleness",
        "reducer_review",
        "worker_packet",
        "proposed_task",
    }.issubset(categories)
