#!/usr/bin/env python3
"""Tests for model_client.py and research_settings.py (DR-897)."""
from __future__ import annotations

import importlib
import io
import json
import urllib.error
from pathlib import Path

import pytest

import model_client as mc
import research_settings as rs


class Reply(io.BytesIO):
    pass


def response(model="anthropic/claude-opus-5.5", text="saved answer", reason="stop", cost=0.2):
    return {
        "id": "gen-unit-test",
        "model": model,
        "choices": [{"finish_reason": reason, "message": {"content": text}}],
        "usage": {"cost": cost, "prompt_tokens": 100, "completion_tokens": 20},
    }


def transport_for(*items, seen=None):
    pending = iter(items)

    def transport(request, timeout):
        if seen is not None:
            seen.append((json.loads(request.data), timeout, request.full_url))
        item = next(pending)
        if isinstance(item, Exception):
            raise item
        return Reply(item if isinstance(item, bytes) else json.dumps(item).encode())

    return transport


def settings(**execution):
    return rs.load_settings(environ={}, overrides={"execution": execution})


def call(*items, role="writer", conf=None, writers=(), **kwargs):
    return mc.chat_completion(
        None, "system", "question",
        role=role, settings=conf or settings(), writers=writers,
        transport=transport_for(*items), api_key="unit-test-key", sleep=lambda _: None, **kwargs,
    )


def test_settings_quality_profile_and_precedence(tmp_path):
    conf = rs.load_settings(
        environ={"DEEP_RESEARCH_WRITER_MAX_TOKENS": "18000", "OPENROUTER_API_KEY": "do-not-freeze"},
        overrides={"roles": {"writer": {"max_tokens": 20000}}},
    )
    writer = conf.resolve("writer")
    reviewer = conf.resolve("brief_reviewer")
    assert writer["model"] == "anthropic/claude-opus-5.5"
    assert reviewer["model"] == "openai/gpt-6.1-sol-pro"
    assert writer["max_tokens"] == 20000
    assert conf.resolve("writer", {"max_tokens": 22000})["max_tokens"] == 22000
    path = tmp_path / "effective.json"
    conf.freeze(path)
    assert "do-not-freeze" not in path.read_text()


def test_settings_reject_unsafe_overrides_and_same_developer_reviewer():
    for bad in (
        {"acceptance": False},
        {"execution": {"endpoint": "https://evil.invalid"}},
        {"roles": {"writer": {"model": "unknown/model"}}},
        {"roles": {"writer": {"reasoning_effort": "none"}}},
    ):
        with pytest.raises(ValueError):
            rs.load_settings(environ={}, overrides=bad)
    conf = rs.load_settings(environ={})
    with pytest.raises(ValueError, match="independence"):
        conf.resolve("brief_reviewer", writer_developers=["openai"])


def test_success_uses_role_settings_and_preserves_usage():
    seen = []
    raw = response()
    res = mc.chat_completion(
        None, "system", "question", role="writer", settings=settings(),
        transport=transport_for(raw, seen=seen), api_key="unit-test-key",
    )
    assert res.content == "saved answer" and res.cost_usd == 0.2
    assert res.calls[0]["identity_verified"] is True
    body, timeout, url = seen[0]
    assert body["reasoning"] == {"effort": "high"}
    assert body["provider"]["allow_fallbacks"] is False
    assert url == "https://openrouter.ai/api/v1/chat/completions"


def test_truncated_output_expands_once_and_fails_at_ceiling():
    seen = []
    raw = response(text="truncated", reason="length")
    res = mc.chat_completion(
        None, "s", "u", role="writer", settings=settings(),
        transport=transport_for(raw, response(), seen=seen), api_key="unit-test-key", sleep=lambda _: None,
    )
    assert res.content == "saved answer"
    assert [c["status"] for c in res.calls] == ["truncated", "success"]
    assert [x[0]["max_tokens"] for x in seen] == [16000, 32000]
    with pytest.raises(mc.ModelCallError, match="truncated"):
        call(response(reason="length"), response(reason="length"))


def test_auth_and_invalid_requests_never_retry_or_leak_secrets():
    err = urllib.error.HTTPError("https://openrouter.ai", 401, "Bearer unit-test-key", {}, None)
    with pytest.raises(mc.ModelCallError) as caught:
        call(err)
    assert len(caught.value.calls) == 1
    assert "unit-test-key" not in str(caught.value)
    assert caught.value.calls[0]["status"] == "authentication"


def test_rate_limit_retries_with_backoff_and_fallback():
    seen, pauses = [], []
    conf = settings(backoff_seconds=2, max_backoff_seconds=9, backoff_multiplier=3)
    err = urllib.error.HTTPError("https://openrouter.ai", 429, "rate limit", {}, None)
    res = mc.chat_completion(
        None, "s", "u", role="brief_reviewer", settings=conf,
        writers=[{"role": "writer", "status": "success", "served_model": "anthropic/claude-opus-5.5",
                  "generation_id": "gen-w", "identity_verified": True}],
        transport=transport_for(err, err, response("openai/gpt-6.1-terra-pro"), seen=seen),
        api_key="unit-test-key", sleep=pauses.append,
    )
    assert pauses == [2, 6]
    assert res.calls[1]["fallback_from"] == "openai/gpt-6.1-sol-pro"
    assert res.acceptance_eligible is True


def test_same_developer_or_missing_writer_evidence_is_not_acceptance_eligible():
    res = call(
        response("anthropic/claude-opus-5.5"), role="brief_reviewer",
        writers=[{"status": "success", "served_model": "anthropic/claude-opus-5.5",
                  "generation_id": "gen-w", "identity_verified": True}],
    )
    assert res.acceptance_eligible is False
    assert call(response("openai/gpt-6.1-sol-pro"), role="brief_reviewer").acceptance_eligible is False


def test_no_redirect_handler_blocks_redirect():
    handler = mc._NoRedirect()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(None, None, 307, "redirect", {}, "https://unapproved.invalid")
