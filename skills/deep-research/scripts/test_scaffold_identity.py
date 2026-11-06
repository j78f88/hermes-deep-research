"""New reader scaffolds use the current assistant's attribution."""
from __future__ import annotations

import pytest

from test_report_purpose_init import creation_args, load_init


@pytest.mark.parametrize(
    "generator",
    ["reference_pack", "internal_full_pack", "internal_executive_brief"],
)
@pytest.mark.parametrize("purpose", ["factual", "decision_assessment"])
def test_new_reader_scaffold_names_research_contributors(generator: str, purpose: str) -> None:
    text = getattr(load_init(), generator)(creation_args(purpose))
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    assert "author: Research contributors / Hermes internal synthesis" in frontmatter
    assert "author: Research agent" not in frontmatter
    assert "stage: import" in frontmatter
