#!/usr/bin/env python3
"""Bounded OpenRouter chat completions with separate requested/served evidence.

Application JSON/schema parsing deliberately lives outside the paid-call loop. Keep
``result.raw_responses`` (or the same attributes on ModelCallError) in run sidecars.
Returned content and evidence redact exact resolved-credential echoes. Concurrency
limits are process-local buckets keyed by the numeric concurrency setting: equal
limits share a semaphore; differing limits do not impose a combined global cap.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import copy
import http.client
import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_settings import REVIEWERS, load_settings

CALLS = []
RAW_RESPONSES = []
_LOCK = threading.Lock()
_LIMITERS = {}


class ModelCallError(RuntimeError):
    def __init__(self, reason, calls, raw_responses):
        super().__init__(f'model call stopped: {reason}')
        self.calls = copy.deepcopy(calls)
        self.raw_responses = copy.deepcopy(raw_responses)


class CompletionResult:
    def __init__(self, content, response, calls, raw_responses, acceptance_eligible):
        self.content = content
        self.response = copy.deepcopy(response)
        self.calls = copy.deepcopy(calls)
        self.raw_responses = copy.deepcopy(raw_responses)
        self.acceptance_eligible = acceptance_eligible
        costs = [c.get('cost_usd') for c in calls]
        self.cost_usd = sum(costs) if all(c is not None for c in costs) else None


def _api_key():
    """Use the existing reference, once only; auth failures never invoke this again."""
    value = os.environ.get('OPENROUTER_API_KEY')
    if value:
        return value
    try:
        for line in (Path.home() / '.hermes/.env').read_text().splitlines():
            if line.startswith('OPENROUTER_API_KEY='):
                value = line.split('=', 1)[1].strip().strip('\"').strip("'")
                if value:
                    return value
    except OSError:
        pass
    raise RuntimeError('existing OpenRouter secret reference is unavailable')


def _redact(value, token) -> Any:
    """Remove the resolved secret from JSON-shaped evidence, including keys."""
    if isinstance(value, str):
        return value.replace(token, '[REDACTED]') if token else value
    if isinstance(value, dict):
        return {_redact(k, token): _redact(v, token) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, token) for v in value]
    return value


def _classify(status):
    if status in (401, 403):
        return 'authentication'
    if status == 429:
        return 'rate_limit'
    if status == 408:
        return 'timeout'
    if isinstance(status, int) and 500 <= status < 600:
        return 'transient'
    return 'invalid_request'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, 'unapproved endpoint redirect', headers, fp)


def _read_body(reply, chunks, deadline, timeout_seconds, clock):
    """Read one buffered/socket chunk per deadline check; retain failed-read bytes.

    Standard urllib sockets get the remaining worker budget before each read1.
    DNS and injected transports that ignore timeouts cannot be force-preempted.
    """
    stream = reply.fp if isinstance(reply, urllib.error.HTTPError) else reply
    sock = getattr(getattr(getattr(stream, 'fp', None), 'raw', None), '_sock', None)
    read = getattr(reply, 'read1', None) or reply.read
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError('worker body-read deadline exhausted')
        if sock is not None:
            sock.settimeout(min(timeout_seconds, remaining))
        try:
            chunk = read(65536)
        except http.client.IncompleteRead as exc:
            chunks.append(exc.partial)
            raise
        if not chunk:
            # read1(), unlike read(), does not reject a short Content-Length body.
            if isinstance(stream, http.client.HTTPResponse) and stream.length:
                raise http.client.IncompleteRead(b'', stream.length)
            return b''.join(chunks)
        chunks.append(chunk)
        # The final Content-Length chunk closes urllib's sole remaining socket file.
        if isinstance(stream, http.client.HTTPResponse) and stream.length == 0:
            return b''.join(chunks)


def _cost(response):
    usage = response.get('usage')
    value = usage.get('cost') if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def writer_provenance(settings, writers):
    """Require known served identity, literal True, and nonblank generation ID per writer.

    This structural check is not independent authentication of caller-supplied evidence.
    """
    if not isinstance(writers, (list, tuple)) or any(not isinstance(r, dict) for r in writers):
        return set(), False
    developers = []
    known = bool(writers)
    for record in writers:
        model = record.get('served_model')
        developer = settings.developer(model)
        if record.get('status') is None:
            known = False
            continue
        if record.get('status') != 'success':
            # Failed/truncated calls cannot have authored the returned reader text.
            if record.get('status') not in {'authentication', 'invalid_request', 'timeout',
                    'rate_limit', 'transient', 'empty', 'truncated', 'invalid_response', 'duration'}:
                known = False
            continue
        generation_id = record.get('generation_id')
        if (record.get('identity_verified') is not True or not developer or
                not isinstance(generation_id, str) or not generation_id.strip()):
            known = False
        else:
            developers.append(developer)
    return set(developers), known and bool(developers)


def chat_completion(model, system, user, max_tokens=None, role='model', settings=None, *,
                    writers=(), transport=None, sleep=None, clock=None, api_key=None):
    """Return content plus every attempt/raw response; never accept unknown identities.

    ``model``/``max_tokens`` are explicit run overrides. Prefer load_settings(overrides=...)
    then freeze the settings before dispatch. Call records also carry the effective route.
    ``writers`` must contain the actual served calls that authored the artifact, including
    rewriting editors, not model names selected before invocation.
    """
    settings = settings or load_settings()
    overrides = {}
    if model is not None:
        overrides['model'] = model
        # Explicit pins do not inherit a duplicate fallback from a different primary.
        baseline = settings.resolve(role)
        overrides['fallback_models'] = [m for m in baseline['fallback_models'] if m != model]
    if max_tokens is not None:
        overrides['max_tokens'] = max_tokens
    developers, writers_known = writer_provenance(settings, writers)
    route = settings.resolve(role, overrides, writer_developers=developers)
    if not isinstance(system, str) or not isinstance(user, (str, list)):
        raise ValueError('messages must be system text and user text/native parts')
    if isinstance(user, list):
        for part in user:
            if not isinstance(part, dict) or part.get('type') not in ('text', 'image_url'):
                raise ValueError('unsupported native message part')
            if part['type'] == 'image_url' and 'image' not in route['requirements']['input_modalities']:
                raise ValueError('image input requires an image-capable role')
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
    # UTF-8 byte length is a conservative text-token upper bound, not a bytes/4 guess.
    # Image URLs/base64 are not text tokens; their native token usage is unknown.
    has_images = isinstance(user, list) and any(part['type'] == 'image_url' for part in user)
    text_user = [part for part in user if part['type'] == 'text'] if isinstance(user, list) else user
    text_messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': text_user}]
    input_bound = len(json.dumps(text_messages, ensure_ascii=False).encode())
    chain = [route['model']] + route['fallback_models']
    primary_context = settings._routing['catalogue']['models'][route['model']]['context_length']
    for candidate in chain:
        row = settings._routing['catalogue']['models'][candidate]
        if has_images and row['context_length'] < primary_context:
            raise ValueError('native image fallback cannot reduce primary context')
        if input_bound + route['max_tokens'] > row['context_length']:
            raise ValueError('input/output exceeds catalogue context allowance')
    policy = route['execution']
    transport = transport or urllib.request.build_opener(_NoRedirect()).open
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    started = clock()
    deadline = started + policy['worker_duration_seconds']
    with _LOCK:
        limiter = _LIMITERS.setdefault(policy['concurrency'], threading.BoundedSemaphore(policy['concurrency']))
    calls, raw_responses = [], []
    if not limiter.acquire(timeout=max(0, deadline - clock())):
        raise ModelCallError('worker duration exhausted while waiting for concurrency', calls, raw_responses)
    try:
        token = api_key if api_key is not None else _api_key()
        current, tokens, last = 0, route['max_tokens'], 'no response'
        for attempt in range(policy['attempts']):
            remaining = deadline - clock()
            if remaining <= 0:
                raise ModelCallError('worker duration exhausted', calls, raw_responses)
            candidate = chain[current]
            try:
                row = settings._routing['catalogue']['models'][candidate]
                tokens = min(tokens, policy['max_call_tokens'],
                             row['top_provider']['max_completion_tokens'],
                             row['context_length'] - input_bound)
                if tokens < 1:
                    raise ValueError('no remaining text context for output')
                call_route = copy.deepcopy(route)
                call_route.update(model=candidate, max_tokens=tokens)
                call_route['catalogue']['model'] = copy.deepcopy(row)
                settings.validate_model(candidate, role, call_route)
            except (ValueError, TypeError, KeyError):
                if calls:
                    raise ModelCallError('local route validation failed', calls, raw_responses) from None
                raise
            body = {'model': candidate, 'max_tokens': tokens, 'usage': {'include': True},
                    'reasoning': {'effort': route['reasoning_effort']},
                    'provider': {'allow_fallbacks': False, 'require_parameters': True, 'data_collection': 'deny'},
                    'messages': messages, **route['parameters']}
            request = urllib.request.Request(route['endpoint'], data=json.dumps(body).encode(),
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
            rec = {'role': role, 'requested_model': route['model'], 'attempt_model': candidate,
                   'served_model': None, 'generation_id': None, 'attempt': attempt + 1,
                   'fallback_from': chain[current - 1] if current else None,
                   'invocation_time': datetime.now(timezone.utc).isoformat(),
                   'identity_verified': False, 'capability_verified': False, 'cost_usd': None,
                   'input_text_token_upper_bound': input_bound,
                   'input_image_tokens': None if has_images else 0,
                   'effective_route': call_route}
            response, content = None, None
            raw_start = len(raw_responses)
            chunks, body_complete = [], False
            try:
                with transport(request, timeout=min(policy['timeout_seconds'], remaining)) as reply:
                    raw = _read_body(reply, chunks, deadline, policy['timeout_seconds'], clock)
                body_complete = True
                try:
                    response = _redact(json.loads(raw), token)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Keep invalid raw bytes too; do not repay to repair a local parser/schema.
                    raw_responses.append(_redact(raw.decode('utf-8', errors='replace'), token))
                    rec['status'] = 'invalid_response'
                else:
                    raw_responses.append(response)
                    if not isinstance(response, dict):
                        rec['status'] = 'invalid_response'
                    elif response.get('error'):
                        error = response['error']
                        code = error.get('code') if isinstance(error, dict) else None
                        rec['status'] = _classify(code)
                    else:
                        rec.update(served_model=response.get('model'), generation_id=response.get('id'),
                                   usage=response.get('usage'), cost_usd=_cost(response))
                        rec['developer'] = settings.developer(rec['served_model'])
                        rec['identity_verified'] = bool(rec['developer'] and isinstance(rec['generation_id'], str) and rec['generation_id'].strip())
                        try:
                            settings.validate_model(rec['served_model'], role, call_route)
                            served_context = settings._routing['catalogue']['models'][rec['served_model']]['context_length']
                            if (input_bound + tokens > served_context or
                                    has_images and served_context < primary_context):
                                raise ValueError('served context does not admit the request')
                            rec['capability_verified'] = True
                        except (ValueError, TypeError):
                            pass
                        choices = response.get('choices')
                        choice = choices[0] if isinstance(choices, list) and choices else {}
                        if not isinstance(choice, dict):
                            choice = {}
                        message = choice.get('message')
                        content = message.get('content') if isinstance(message, dict) else None
                        if choice.get('finish_reason') in ('length', 'max_tokens'):
                            rec['status'] = 'truncated'
                        elif choice.get('finish_reason') != 'stop':
                            rec['status'] = 'invalid_response'
                        else:
                            rec['status'] = 'success' if isinstance(content, str) and content.strip() else 'empty'
            except urllib.error.HTTPError as exc:
                rec['status'] = _classify(exc.code)
                rec['http_status'] = exc.code
                try:
                    if not exc.closed:
                        with exc:
                            _read_body(exc, chunks, deadline, policy['timeout_seconds'], clock)
                        body_complete = True
                except (TimeoutError, socket.timeout):
                    rec['body_status'] = 'timeout'
                except (OSError, http.client.HTTPException):
                    rec['body_status'] = 'transient'
            except (TimeoutError, socket.timeout):
                rec['status'] = 'timeout'
            except urllib.error.URLError as exc:
                rec['status'] = 'timeout' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'transient'
            except (OSError, http.client.HTTPException):
                rec['status'] = 'transient'
            if len(raw_responses) == raw_start and chunks:
                raw = b''.join(chunks)
                saved = raw.decode('utf-8', errors='replace')
                if body_complete:
                    try:
                        saved = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                raw_responses.append(_redact(saved, token))
            rec['elapsed_seconds'] = max(0, clock() - started)
            if clock() >= deadline:
                rec['status'] = 'duration'
            rec = _redact(rec, token)
            calls.append(rec)
            with _LOCK:
                CALLS.append(copy.deepcopy(rec))
                RAW_RESPONSES.extend(copy.deepcopy(raw_responses[raw_start:]))
            last = rec['status']
            if last == 'success':
                eligible = bool(role in REVIEWERS and writers_known and rec['identity_verified'] and
                    rec['capability_verified'] and rec['developer'] not in developers)
                return CompletionResult(content, response, calls, raw_responses, eligible)
            if last not in policy['retry_classes'] or attempt + 1 >= policy['attempts']:
                break
            if last == 'truncated':
                ceiling = min(policy['max_call_tokens'],
                    settings._routing['catalogue']['models'][candidate]['top_provider']['max_completion_tokens'],
                    settings._routing['catalogue']['models'][candidate]['context_length'] - input_bound)
                if tokens >= ceiling:
                    break
                tokens = min(tokens * 2, ceiling)
            elif last in policy['fallback_classes'] and current + 1 < len(chain):
                current += 1
            delay = min(policy['backoff_seconds'] * policy['backoff_multiplier'] ** attempt,
                        policy['max_backoff_seconds'])
            if deadline - clock() <= delay:
                raise ModelCallError('worker duration exhausted before retry', calls, raw_responses)
            sleep(delay)
        raise ModelCallError(last, calls, raw_responses)
    finally:
        limiter.release()
