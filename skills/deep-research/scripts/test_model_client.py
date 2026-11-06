"""Safe model client tests. HTTP/clocks are doubles; generation IDs are unit sentinels."""
import importlib
import importlib.util
import io
import http.client
import json
import socket
from types import SimpleNamespace
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

from research_settings import load_settings


def module():
    assert importlib.util.find_spec('model_client'), 'shared safe model client is missing'
    return importlib.import_module('model_client')


class Reply(io.BytesIO):
    pass


def response(model='anthropic/claude-opus-5.5', text='saved answer', reason='stop', cost=0.2):
    return {'id': 'gen-unit-test', 'model': model,
            'choices': [{'finish_reason': reason, 'message': {'content': text}}],
            'usage': {'cost': cost, 'prompt_tokens': 100, 'completion_tokens': 20}}


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
    return load_settings(environ={}, overrides={'execution': execution})


def call(*items, role='writer', conf=None, writers=(), **kwargs):
    return module().chat_completion(None, 'system', 'question', role=role,
        settings=conf or settings(), writers=writers,
        transport=transport_for(*items), api_key='unit-test-key', sleep=lambda _: None, **kwargs)


def test_success_uses_role_settings_and_preserves_response_usage_identity():
    seen = []
    raw = response()
    result = module().chat_completion(None, 'system', 'question', role='writer', settings=settings(),
        transport=transport_for(raw, seen=seen), api_key='unit-test-key')
    assert result.content == 'saved answer'
    assert result.response == raw
    assert result.raw_responses == [raw]
    assert result.calls[0]['served_model'] == raw['model']
    assert result.calls[0]['requested_model'] == raw['model']
    assert result.calls[0]['identity_verified'] is True
    assert result.cost_usd == 0.2
    body, timeout, url = seen[0]
    assert body['reasoning'] == {'effort': 'high'}
    assert body['max_tokens'] == 16000
    assert 'temperature' not in body
    assert body['provider'] == {'allow_fallbacks': False, 'require_parameters': True, 'data_collection': 'deny'}
    assert url == 'https://openrouter.ai/api/v1/chat/completions'
    assert timeout <= 900


def test_truncated_output_is_not_returned_and_all_paid_responses_survive():
    seen = []
    raw = response(text='truncated', reason='length')
    result = module().chat_completion(None, 's', 'u', role='writer', settings=settings(),
        transport=transport_for(raw, response(), seen=seen), api_key='unit-test-key', sleep=lambda _: None)
    assert result.content == 'saved answer'
    assert [c['status'] for c in result.calls] == ['truncated', 'success']
    assert [x[0]['max_tokens'] for x in seen] == [16000, 32000]
    assert result.raw_responses[0] == raw
    assert result.cost_usd == pytest.approx(0.4)


def test_truncation_at_ceiling_fails_without_returning_partial_text():
    mod = module()
    with pytest.raises(mod.ModelCallError, match='truncated') as err:
        call(response(reason='length'), response(reason='length'))
    assert len(err.value.calls) == 2
    assert len(err.value.raw_responses) == 2


def test_local_schema_failure_does_not_make_another_call():
    result = call(response(text='valid text, not your JSON schema'))
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.content)
    assert len(result.calls) == 1
    assert result.raw_responses[0]['choices'][0]['message']['content'] == result.content


@pytest.mark.parametrize('status', [401, 403, 400, 404, 422])
def test_auth_permission_and_invalid_requests_never_retry_or_leak_secrets(status):
    mod = module()
    err = urllib.error.HTTPError('https://openrouter.ai', status, 'Bearer unit-test-key', {}, None)
    with pytest.raises(mod.ModelCallError) as caught:
        call(err)
    assert len(caught.value.calls) == 1
    assert 'unit-test-key' not in str(caught.value)
    assert 'unit-test-key' not in json.dumps(caught.value.calls)
    assert caught.value.calls[0]['status'] == ('authentication' if status in (401, 403) else 'invalid_request')


def test_rate_limit_retries_with_configured_backoff_and_records_fallback():
    seen, pauses = [], []
    mod = module()
    conf = settings(backoff_seconds=2, max_backoff_seconds=9, backoff_multiplier=3)
    err = urllib.error.HTTPError('https://openrouter.ai', 429, 'rate limit', {}, None)
    result = mod.chat_completion(None, 's', 'u', role='brief_reviewer', settings=conf,
        writers=[{'role': 'writer', 'status': 'success', 'served_model': 'anthropic/claude-opus-5.5', 'generation_id': 'gen-synthetic-writer', 'identity_verified': True}],
        transport=transport_for(err, err, response('openai/gpt-6.1-terra-pro'), seen=seen),
        api_key='unit-test-key', sleep=pauses.append)
    assert pauses == [2, 6]
    assert [x[0]['model'] for x in seen] == ['openai/gpt-6.1-sol-pro', 'openai/gpt-6.1-terra-pro', 'openai/gpt-6.1-terra-pro']
    assert result.calls[1]['fallback_from'] == 'openai/gpt-6.1-sol-pro'
    assert result.calls[1]['effective_route']['catalogue']['model']['id'] == result.calls[1]['attempt_model']
    assert result.acceptance_eligible is True
    assert result.cost_usd is None  # failed attempts have no known provider cost


def test_retry_classes_are_not_decorative():
    mod = module()
    with pytest.raises(mod.ModelCallError) as caught:
        call(TimeoutError(), conf=settings(retry_classes=[], fallback_classes=[]))
    assert len(caught.value.calls) == 1
    assert caught.value.calls[0]['status'] == 'timeout'


@pytest.mark.parametrize('served', [None, 'unknown/hosted-model', 'anthropic/claude-opus-5.5', 'google/gemini-3.8-flash'])
def test_unknown_same_developer_or_incapable_served_identity_cannot_accept(served):
    result = call(response(served), role='brief_reviewer',
        writers=[{'status': 'success', 'served_model': 'anthropic/claude-opus-5.5', 'generation_id': 'gen-synthetic-writer', 'identity_verified': True}])
    assert result.acceptance_eligible is False
    assert result.calls[-1]['served_model'] == served
    assert len(result.calls) == 1  # preserve a valid paid response, do not buy an identity retry


def test_missing_actual_writer_evidence_cannot_accept():
    result = call(response('openai/gpt-6.1-sol-pro'), role='brief_reviewer')
    assert result.acceptance_eligible is False


def test_unknown_writer_identity_cannot_accept():
    result = call(response('openai/gpt-6.1-sol-pro'), role='brief_reviewer',
        writers=[{'status': 'success', 'requested_model': 'anthropic/claude-opus-5.5'}])
    assert result.acceptance_eligible is False


def test_rewriting_editor_counts_as_actual_writer_and_blocks_same_developer_before_call():
    mod = module()
    with pytest.raises(ValueError, match='independence'):
        call(response('openai/gpt-6.1-sol-pro'), role='brief_reviewer', writers=[
            {'role': 'rewriting_editor', 'status': 'success', 'served_model': 'openai/gpt-6.1-terra-pro', 'generation_id': 'gen-synthetic-editor', 'identity_verified': True}])


def test_native_image_parts_are_preserved_and_text_only_role_rejects_images():
    seen = []
    parts = [{'type': 'text', 'text': 'inspect'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
    result = module().chat_completion(None, 'system', parts, role='visual_reviewer', settings=settings(),
        transport=transport_for(response('openai/gpt-6.1-sol-pro'), seen=seen), api_key='unit-test-key')
    assert seen[0][0]['messages'][1]['content'] == parts
    assert result.content
    with pytest.raises(ValueError, match='image'):
        module().chat_completion(None, 'system', parts, role='writer', settings=settings(),
            transport=transport_for(), api_key='unit-test-key')


def test_duration_bounds_http_timeout_and_stops_retries():
    clock = [100.0]
    def transport(request, timeout):
        assert timeout <= 5
        clock[0] += 5
        raise TimeoutError()
    mod = module()
    with pytest.raises(mod.ModelCallError, match='duration') as caught:
        mod.chat_completion(None, 's', 'u', role='writer',
            settings=settings(timeout_seconds=5, worker_duration_seconds=5),
            transport=transport, api_key='unit-test-key', clock=lambda: clock[0], sleep=lambda _: None)
    assert len(caught.value.calls) == 1


def test_concurrency_limits_transport_across_separately_loaded_settings():
    mod = module()
    active = [0, 0]
    lock = threading.Lock()
    def transport(request, timeout):
        with lock:
            active[0] += 1
            active[1] = max(active)
        time.sleep(0.01)
        with lock:
            active[0] -= 1
        return Reply(json.dumps(response()).encode())
    def run(_):
        return mod.chat_completion(None, 's', 'u', role='writer', settings=settings(concurrency=1),
            api_key='unit-test-key', transport=transport)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(run, range(4)))
    assert active[1] == 1
    assert all(len(r.calls) == 1 for r in results)


@pytest.mark.parametrize('raw', [b'not json', b'{"error": {"code": 401, "message": "unit-test-key"}}'])
def test_malformed_or_error_responses_are_preserved_without_local_retries(raw):
    mod = module()
    with pytest.raises(mod.ModelCallError) as caught:
        call(raw)
    assert len(caught.value.calls) == 1
    assert caught.value.raw_responses
    assert 'unit-test-key' not in str(caught.value)


def test_unknown_cost_stays_unknown():
    raw = response()
    raw['usage'].pop('cost')
    assert call(raw).cost_usd is None


def test_unsupported_parameters_fail_before_transport():
    with pytest.raises(ValueError, match='unsupported'):
        conf = load_settings(environ={}, overrides={'roles': {'writer': {'parameters': {'temperature': 0.3}}}})
        call(response(), conf=conf)


def test_missing_status_writer_cannot_hide_alongside_known_writer():
    result = call(response('openai/gpt-6.1-sol-pro'), role='brief_reviewer', writers=[
        {'status': 'success', 'served_model': 'anthropic/claude-opus-5.5', 'generation_id': 'gen-synthetic-writer', 'identity_verified': True},
        {'role': 'rewriting_editor', 'requested_model': 'openai/gpt-6.1-sol-pro'}])
    assert result.acceptance_eligible is False


@pytest.mark.parametrize('updates', [{'usage': [1]}, {'model': {'not': 'an identity'}}])
def test_malformed_metadata_preserves_valid_content_without_repaying(updates):
    raw = response()
    raw.update(updates)
    result = call(raw)
    assert result.content == 'saved answer'
    assert result.raw_responses == [raw]
    assert len(result.calls) == 1


def test_response_without_terminal_finish_reason_is_not_complete():
    raw = response()
    del raw['choices'][0]['finish_reason']
    with pytest.raises(module().ModelCallError) as caught:
        call(raw)
    assert caught.value.raw_responses == [raw]


def test_missing_provider_generation_identity_cannot_accept():
    raw = response('openai/gpt-6.1-sol-pro')
    raw.pop('id')
    result = call(raw, role='brief_reviewer', writers=[
        {'status': 'success', 'served_model': 'anthropic/claude-opus-5.5', 'generation_id': 'gen-synthetic-writer', 'identity_verified': True}])
    assert result.acceptance_eligible is False
    assert result.calls[-1]['identity_verified'] is False


def test_malformed_raw_response_also_survives_process_telemetry():
    mod = module()
    start = len(mod.RAW_RESPONSES)
    with pytest.raises(mod.ModelCallError):
        call(b'raw parse failure')
    assert mod.RAW_RESPONSES[start:] == ['raw parse failure']


def test_unknown_writer_status_cannot_hide_alongside_verified_writer():
    result = call(response('openai/gpt-6.1-sol-pro'), role='brief_reviewer', writers=[
        {'status': 'success', 'served_model': 'anthropic/claude-opus-5.5', 'generation_id': 'gen-synthetic-writer', 'identity_verified': True},
        {'status': 'unknown', 'served_model': 'openai/gpt-6.1-sol-pro'}])
    assert result.acceptance_eligible is False


def test_served_model_with_smaller_context_cannot_establish_acceptance():
    result = module().chat_completion(None, 's', 'x' * 1000000, role='brief_reviewer', settings=settings(),
        writers=[{'status': 'success', 'served_model': 'google/gemini-3.8-flash', 'generation_id': 'gen-synthetic-writer', 'identity_verified': True}],
        transport=transport_for(response('anthropic/claude-opus-5.5')), api_key='unit-test-key')
    assert result.acceptance_eligible is False
    assert result.calls[-1]['capability_verified'] is False


def test_nonboolean_writer_verification_flag_cannot_establish_acceptance():
    result = call(response('openai/gpt-6.1-sol-pro'), role='brief_reviewer', writers=[
        {'status': 'success', 'served_model': 'anthropic/claude-opus-5.5', 'generation_id': 'gen-synthetic-writer', 'identity_verified': 'false'}])
    assert result.acceptance_eligible is False


@pytest.mark.parametrize('kind', ['success', 'provider_error', 'malformed'])
def test_resolved_credential_echo_is_redacted_recursively_from_all_returned_evidence(kind):
    mod = module()
    call_start, raw_start = len(mod.CALLS), len(mod.RAW_RESPONSES)
    # Synthetic credential echoes, including dictionary keys and nested usage.
    raw = response(text='echo unit-test-key')
    raw['usage']['debug'] = [{'unit-test-key': 'Bearer unit-test-key'}]
    if kind == 'provider_error':
        raw = {'error': {'code': 401, 'message': 'Bearer unit-test-key',
                         'metadata': [{'unit-test-key': 'unit-test-key'}]}}
    elif kind == 'malformed':
        raw = b'not JSON: Bearer unit-test-key'
    try:
        result = call(raw)
    except mod.ModelCallError as exc:
        evidence = [str(exc), exc.calls, exc.raw_responses]
    else:
        evidence = [result.content, result.response, result.calls, result.raw_responses]
    evidence.extend([mod.CALLS[call_start:], mod.RAW_RESPONSES[raw_start:]])
    serialized = json.dumps(evidence)
    assert 'unit-test-key' not in serialized
    assert '[REDACTED]' in serialized


@pytest.mark.parametrize('as_http_error', [False, True])
def test_trickled_body_uses_chunk_reads_remaining_socket_timeout_and_keeps_partial(as_http_error):
    mod = module()
    clock, timeouts = [0.0], []
    class Trickle(Reply):
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append)))

        def read(self, size=-1):
            # The old full read spends far beyond the worker deadline.
            clock[0] += 50
            return b'old unbounded read'

        def read1(self, size=-1):
            assert 0 < size <= 65536
            clock[0] += 1
            return b'chunk '

    reply = Trickle()
    def transport(*a, **kw):
        if as_http_error:
            raise urllib.error.HTTPError('https://openrouter.ai', 401, 'synthetic', {}, reply)
        return reply

    start = len(mod.RAW_RESPONSES)
    with pytest.raises(mod.ModelCallError) as caught:
        mod.chat_completion(None, 's', 'u', role='writer',
            settings=settings(timeout_seconds=3, worker_duration_seconds=3),
            transport=transport, api_key='unit-test-key', clock=lambda: clock[0])
    assert clock[0] <= 3
    assert timeouts == [3, 2, 1]
    assert caught.value.raw_responses == ['chunk chunk chunk ']
    assert mod.RAW_RESPONSES[start:] == caught.value.raw_responses
    assert caught.value.calls[0]['status'] == 'duration'
    assert reply.closed


@pytest.mark.parametrize('failure', ['timeout', 'incomplete', 'protocol'])
@pytest.mark.parametrize('as_http_error', [False, True])
def test_body_read_failure_preserves_redacted_partial_bytes(failure, as_http_error):
    mod = module()
    class Interrupted(Reply):
        reads = 0

        def read(self, size=-1):
            return self.read1(size)

        def read1(self, size=-1):
            self.reads += 1
            if self.reads == 1:
                return b'first unit-test-key '
            if failure == 'incomplete':
                raise http.client.IncompleteRead(b'tail unit-test-key', 10)
            if failure == 'protocol':
                raise http.client.HTTPException('synthetic protocol failure')
            raise TimeoutError('synthetic timeout')

    def transport(*a, **kw):
        reply = Interrupted()
        if as_http_error:
            raise urllib.error.HTTPError('https://openrouter.ai', 401, 'synthetic', {}, reply)
        return reply

    with pytest.raises(mod.ModelCallError) as caught:
        mod.chat_completion(None, 's', 'u', role='writer', settings=settings(attempts=1),
            transport=transport, api_key='unit-test-key')
    expected = 'first [REDACTED] ' + ('tail [REDACTED]' if failure == 'incomplete' else '')
    assert caught.value.raw_responses == [expected]
    record = caught.value.calls[0]
    assert record['body_status' if as_http_error else 'status'] == ('timeout' if failure == 'timeout' else 'transient')
    if as_http_error:
        assert record['status'] == 'authentication'


@pytest.mark.parametrize('body', [b'raw unit-test-key', b'{"error":{"message":"unit-test-key"}}'])
def test_http_error_body_is_preserved_and_redacted_without_auth_retry(body):
    mod = module()
    reply = Reply(body)
    err = urllib.error.HTTPError('https://openrouter.ai', 401, 'synthetic auth failure', {}, reply)
    start = len(mod.RAW_RESPONSES)
    with pytest.raises(mod.ModelCallError) as caught:
        call(err)
    assert len(caught.value.calls) == 1
    assert caught.value.calls[0]['http_status'] == 401
    assert caught.value.raw_responses
    assert 'unit-test-key' not in json.dumps(caught.value.raw_responses)
    assert '[REDACTED]' in json.dumps(caught.value.raw_responses)
    assert mod.RAW_RESPONSES[start:] == caught.value.raw_responses
    assert reply.closed


@pytest.mark.parametrize('limit', ['output', 'text_context'])
def test_expanded_allowance_is_clamped_before_lower_cap_fallback_dispatch(limit):
    conf = settings()
    fallback = conf.resolve('brief_reviewer')['fallback_models'][0]
    text = 'x' * 100000
    input_bound = len(json.dumps([{'role': 'system', 'content': 's'},
                                 {'role': 'user', 'content': text}], ensure_ascii=False).encode())
    # Synthetic catalogue capability fixture only; no live provider claims.
    row = conf._routing['catalogue']['models'][fallback]
    if limit == 'output':
        row['top_provider']['max_completion_tokens'] = 20000
    else:
        row['context_length'] = input_bound + 20000
    seen = []
    err = urllib.error.HTTPError('https://openrouter.ai', 429, 'synthetic rate limit', {}, None)
    result = module().chat_completion(None, 's', text, role='brief_reviewer', settings=conf,
        transport=transport_for(response('openai/gpt-6.1-sol-pro', reason='length'), err,
                                response(fallback), seen=seen),
        api_key='unit-test-key', sleep=lambda _: None)
    assert [entry[0]['max_tokens'] for entry in seen] == [16000, 32000, 20000]
    assert [record['status'] for record in result.calls] == ['truncated', 'rate_limit', 'success']
    assert len(result.raw_responses) == 2


def test_post_call_local_route_failure_is_structured_and_keeps_paid_evidence(monkeypatch):
    mod, conf = module(), settings()
    validate = conf.validate_model
    def fail_expansion(model, role, route):
        if route['max_tokens'] > 16000:
            raise ValueError('synthetic local route rejection')
        return validate(model, role, route)
    monkeypatch.setattr(conf, 'validate_model', fail_expansion)
    raw = response(reason='length')
    seen = []
    with pytest.raises(mod.ModelCallError, match='route') as caught:
        mod.chat_completion(None, 's', 'u', role='writer', settings=conf,
            transport=transport_for(raw, seen=seen), api_key='unit-test-key', sleep=lambda _: None)
    assert len(seen) == 1
    assert len(caught.value.calls) == 1
    assert caught.value.raw_responses == [raw]


@pytest.mark.parametrize('generation_id', [None, '', '   ', 1, False, [], {}])
def test_writer_verified_flag_without_nonempty_generation_cannot_accept(generation_id):
    result = call(response('openai/gpt-6.1-sol-pro'), role='brief_reviewer', writers=[
        {'status': 'success', 'served_model': 'anthropic/claude-opus-5.5',
         'identity_verified': True, 'generation_id': generation_id}])
    assert result.acceptance_eligible is False


def test_missing_generation_writer_cannot_hide_beside_proven_writer():
    result = call(response('openai/gpt-6.1-sol-pro'), role='brief_reviewer', writers=[
        {'status': 'success', 'served_model': 'anthropic/claude-opus-5.5',
         'identity_verified': True, 'generation_id': 'gen-synthetic-writer'},
        {'status': 'success', 'served_model': 'anthropic/claude-opus-5.5',
         'identity_verified': True}])
    assert result.acceptance_eligible is False


def test_whitespace_provider_generation_does_not_verify_served_identity():
    raw = response('openai/gpt-6.1-sol-pro')
    raw['id'] = '   '
    result = call(raw, role='brief_reviewer', writers=[
        {'status': 'success', 'served_model': 'anthropic/claude-opus-5.5',
         'identity_verified': True, 'generation_id': 'gen-synthetic-writer'}])
    assert result.calls[-1]['identity_verified'] is False
    assert result.acceptance_eligible is False


def test_native_image_base64_is_not_a_text_token_estimate_and_image_tokens_stay_unknown():
    mod, records = module(), []
    for encoded in ('AA==', 'A' * 1200000):
        # Opaque synthetic base64 unit payload; no image/model/network decoding.
        parts = [{'type': 'text', 'text': 'inspect'},
                 {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + encoded}}]
        result = mod.chat_completion(None, 's', parts, role='visual_reviewer', settings=settings(),
            transport=transport_for(response('openai/gpt-6.1-sol-pro')), api_key='unit-test-key')
        records.append(result.calls[-1])
    bounds = [record['input_text_token_upper_bound'] for record in records]
    assert all(record['input_image_tokens'] is None for record in records)
    assert bounds[0] == bounds[1]
    assert bounds[0] < 1000


def test_native_image_fallback_cannot_reduce_primary_context_before_any_call():
    mod = module()
    conf = load_settings(environ={}, overrides={'roles': {'visual_reviewer': {
        'fallback_models': ['anthropic/claude-opus-5.5']}}})
    parts = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
    seen = []
    with pytest.raises(ValueError, match='image.*context'):
        mod.chat_completion(None, 's', parts, role='visual_reviewer', settings=conf,
            transport=transport_for(response('openai/gpt-6.1-sol-pro'), seen=seen), api_key='unit-test-key')
    assert seen == []


def test_native_image_actual_served_context_cannot_reduce_primary_context():
    parts = [{'type': 'text', 'text': 'inspect'},
             {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
    raw = response('anthropic/claude-opus-5.5')
    result = module().chat_completion(None, 's', parts, role='visual_reviewer', settings=settings(),
        writers=[{'status': 'success', 'served_model': 'google/gemini-3.8-flash',
                  'identity_verified': True, 'generation_id': 'gen-synthetic-writer'}],
        transport=transport_for(raw), api_key='unit-test-key')
    assert result.calls[-1]['capability_verified'] is False
    assert result.acceptance_eligible is False
    assert result.raw_responses == [raw]
    assert len(result.calls) == 1  # Keep valid raw content without buying another review.


def test_native_image_text_parts_still_require_text_context_allowance():
    parts = [{'type': 'text', 'text': 'x' * 1050000},
             {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
    with pytest.raises(ValueError, match='context'):
        module().chat_completion(None, 's', parts, role='visual_reviewer', settings=settings(),
            transport=transport_for(), api_key='unit-test-key')


@pytest.fixture
def socketpair_response(request):
    def make(body, status=200, missing=0):
        owner, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        request.addfinalizer(owner.close)
        request.addfinalizer(peer.close)
        owner.settimeout(1)
        peer.settimeout(1)
        wire = (f'HTTP/1.1 {status} Synthetic\r\nContent-Length: {len(body) + missing}\r\n\r\n'
                .encode() + body)
        peer.sendall(wire)
        peer.shutdown(socket.SHUT_WR)
        reply = http.client.HTTPResponse(owner)
        request.addfinalizer(reply.close)
        reply.begin()
        # urllib relinquishes the owning socket; the response file keeps its fd alive.
        owner.close()
        assert getattr(reply.fp.raw, '_sock', None) is owner
        assert owner.fileno() >= 0
        return reply, owner
    return make


def test_stdlib_complete_content_length_returns_json_without_retry(socketpair_response):
    mod, replies, pauses = module(), [], []
    raw = response()
    def transport(*args, **kwargs):
        reply, owner = socketpair_response(json.dumps(raw).encode())
        replies.append((reply, owner))
        return reply
    result = mod.chat_completion(None, 's', 'u', role='writer', settings=settings(attempts=2),
        transport=transport, api_key='unit-test-key', sleep=pauses.append)
    assert result.content == 'saved answer'
    assert result.response == raw
    assert result.raw_responses == [raw]
    assert [record['status'] for record in result.calls] == ['success']
    assert len(replies) == 1
    assert pauses == []
    reply, owner = replies[0]
    assert reply.length == 0
    assert reply.isclosed()
    assert owner.fileno() == -1


def test_stdlib_complete_http_error_body_is_not_transient(socketpair_response):
    mod, seen = module(), []
    raw = {'error': {'code': 401, 'message': 'unit-test-key'}}
    reply, owner = socketpair_response(json.dumps(raw).encode(), status=401)
    def transport(*args, **kwargs):
        seen.append(True)
        raise urllib.error.HTTPError('https://openrouter.ai', 401, 'synthetic', reply.headers, reply)
    with pytest.raises(mod.ModelCallError) as caught:
        mod.chat_completion(None, 's', 'u', role='writer', settings=settings(attempts=2),
            transport=transport, api_key='unit-test-key')
    assert len(seen) == 1
    assert len(caught.value.calls) == 1
    record = caught.value.calls[0]
    assert record['status'] == 'authentication'
    assert record['http_status'] == 401
    assert 'body_status' not in record
    assert caught.value.raw_responses == [{'error': {'code': 401, 'message': '[REDACTED]'}}]
    assert reply.length == 0
    assert reply.isclosed()
    assert owner.fileno() == -1


@pytest.mark.parametrize('as_http_error', [False, True])
def test_socketpair_short_content_length_retains_partial_evidence(socketpair_response, as_http_error):
    mod = module()
    raw = json.dumps(response(text='unit-test-key')).encode()
    reply, owner = socketpair_response(raw, missing=10)
    def transport(*args, **kwargs):
        if as_http_error:
            raise urllib.error.HTTPError('https://openrouter.ai', 401, 'synthetic', reply.headers, reply)
        return reply
    with pytest.raises(mod.ModelCallError) as caught:
        mod.chat_completion(None, 's', 'u', role='writer', settings=settings(attempts=1),
            transport=transport, api_key='unit-test-key')
    assert caught.value.raw_responses == [raw.decode().replace('unit-test-key', '[REDACTED]')]
    assert caught.value.calls[0]['body_status' if as_http_error else 'status'] == 'transient'
    assert reply.length == 10
    assert owner.fileno() == -1


@pytest.mark.parametrize('as_http_error', [False, True])
def test_stdlib_incomplete_content_length_keeps_partial_instead_of_accepting_json(as_http_error):
    raw = json.dumps(response()).encode()
    class SyntheticSocket:
        def makefile(self, *args):
            # A local HTTP parser fixture, never an actual socket or HTTP request.
            wire = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(raw) + 10).encode() + b'\r\n\r\n' + raw
            return io.BytesIO(wire)
    reply = http.client.HTTPResponse(SyntheticSocket())
    reply.begin()
    def transport(*args, **kwargs):
        if as_http_error:
            raise urllib.error.HTTPError('https://openrouter.ai', 401, 'synthetic', {}, reply)
        return reply
    with pytest.raises(module().ModelCallError) as caught:
        module().chat_completion(None, 's', 'u', role='writer', settings=settings(attempts=1),
            transport=transport, api_key='unit-test-key')
    assert caught.value.raw_responses == [raw.decode()]
    assert caught.value.calls[0]['body_status' if as_http_error else 'status'] == 'transient'


def test_network_request_cannot_follow_redirect_to_unapproved_endpoint():
    mod = module()
    assert hasattr(mod, '_NoRedirect'), 'approved endpoint must not redirect the credential-bearing POST'
    handler = mod._NoRedirect()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(None, None, 307, 'redirect', {}, 'https://unapproved.invalid')
