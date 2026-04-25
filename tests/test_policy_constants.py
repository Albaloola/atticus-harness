from __future__ import annotations

from atticus.core.policies import LegalStage


def test_authority_stage_uses_s6_code():
    assert LegalStage.S6_AUTHORITY_LAW_MAP == "S6"
