"""initialisms and short abbreviations do not end a sentence."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fact_grades as fg  # noqa: E402


@pytest.mark.parametrize("text", [
    "ChatGPT Instant Checkout started with U.S. Etsy sellers [1].",
    "The case is Amazon v. Perplexity on remand [1].",
    "Acme Pty. Ltd. sells it in Adelaide [1].",
    "The U.K. regulator said so in 2026 [1].",
    "Dr. Smith wrote the report [1].",
])
def test_abbreviation_is_one_sentence(text):
    rows = fg.split_sentences(text)
    assert [r["text"] for r in rows] == [text]


def test_real_sentence_ends_still_split():
    rows = fg.split_sentences("It runs on Linux.[1] It needs Python 3.11 [2]. The U.S. price is $10.[3] Done.")
    assert [r["text"] for r in rows] == ["It runs on Linux.[1]", "It needs Python 3.11 [2].",
                                         "The U.S. price is $10.[3]", "Done."]
