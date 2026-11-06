"""Semantic review payloads for synthetic, offline reader fixtures only.

These are saved review inputs, not a substitute for exercising the real gates.
Each call returns fresh nested dictionaries so adversarial mutations stay local.
"""
from __future__ import annotations

import hashlib
import io
import itertools
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


@contextmanager
def offline_plain_transport(plain, transport=None, *, responder=None):
    """Exercise the real shared client with synthetic HTTP and no secret lookup.

    Guard the legacy urlopen path as well as injecting the shared transport. The
    response sentinels are offline test evidence, never live provider receipts.
    """
    original = plain.model_client.chat_completion
    sequence = itertools.count(1)

    def deny(*args, **kwargs):
        raise AssertionError("legacy network forbidden in offline reader tests")

    def completion(model, system, user, **kwargs):
        if responder is not None:
            respond = responder

            def selected_transport(request, timeout):
                payload = json.loads(request.data)
                text = respond(kwargs["role"], system, user)
                return io.BytesIO(json.dumps({
                    "synthetic": True,
                    "id": f"gen-SYNTHETIC-{next(sequence):04d}",
                    "model": payload["model"],
                    "choices": [{"finish_reason": "stop", "message": {"content": text}}],
                    "usage": {"cost": 0},
                }).encode())
        elif transport is not None:
            selected_transport = transport
        else:
            raise AssertionError("offline tests require an explicit synthetic transport")
        return original(model, system, user, **kwargs, transport=selected_transport, sleep=lambda _: None)

    with patch.object(plain.model_client, "_api_key", return_value="synthetic-offline-only"), \
            patch.object(plain.urllib.request, "urlopen", side_effect=deny), \
            patch.object(plain.model_client, "chat_completion", completion):
        yield


def offline_plain_client(plain, responder):
    """Return text for each routed role through real wrapper/identity handling."""
    return offline_plain_transport(plain, responder=responder)


def refresh_fixture_acceptance_bindings(root: Path) -> None:
    """Bind existing synthetic QA records after deliberate fixture setup changes.

    Pytest fixture construction only: this is not a live review or approval.
    Keep verdicts, findings, saved editor reader hashes and supporting evidence
    unchanged. Missing records stay missing, so negative fixtures remain blocked.
    """
    from research_qa_repair import semantic_manifest_snapshot

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    snapshot = semantic_manifest_snapshot(manifest)
    pack_rel = ("internal/reference-pack.md"
                if manifest.get("output_layout") == "brief-and-reference-v2"
                else "internal/full-research-pack.md")
    receipt_rel = ((manifest.get("qa") or {}).get("receipts") or {}).get("evidence_privacy") or "qa/evidence-privacy-review.json"
    receipt_path = root / receipt_rel
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["full_pack_sha256"] = hashlib.sha256((root / pack_rel).read_bytes()).hexdigest()
        receipt["semantic_manifest"] = snapshot
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    decisions_path = root / "qa/editor-note-decisions.json"
    if decisions_path.exists():
        decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
        decisions["semantic_manifest"] = snapshot
        decisions_path.write_text(json.dumps(decisions), encoding="utf-8")


def semantic_review_fields() -> dict:
    return {
        "checks": {
            "factual": {
                "verdict": "PASS",
                "basis": "Compared the fixture's claims, source links and stated limitations with its saved reference content.",
            },
            "reasoning": {
                "verdict": "PASS",
                "basis": "The conclusion follows the fixture evidence and preserves the gap in independent long-term evidence.",
            },
            "editorial": {
                "verdict": "PASS",
                "basis": "The answer, explanation and remaining gaps are connected and use the declared report purpose.",
            },
            "presentation": {
                "verdict": "PASS",
                "basis": "Reviewed headings, compact tables and source links; process records remain in sidecars.",
            },
        },
        "important_quantity_qualification_review": {
            "verdict": "PASS",
            "basis": "Compared material quantities with their subjects, units, periods, scope and uncertainty against the saved fixture evidence.",
        },
    }


def editor_pass() -> dict:
    return {"verdict": "PASS", "blocking": [], "minor": [], **semantic_review_fields()}
