"""Credential-free contracts for shared research operating choices."""
import importlib
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    assert importlib.util.find_spec('research_settings'), 'shared settings resolver is missing'
    return importlib.import_module('research_settings')


def test_quality_profile_uses_catalogued_capable_roles():
    settings = module().load_settings(environ={})
    writer = settings.resolve('writer')
    reviewer = settings.resolve('brief_reviewer')
    assert writer['model'] == 'anthropic/claude-opus-5.5'
    assert reviewer['model'] == 'openai/gpt-6.1-sol-pro'
    assert writer['reasoning_effort'] == reviewer['reasoning_effort'] == 'high'
    assert writer['catalogue']['model']['context_length'] >= 100000
    assert len(writer['catalogue']['source_sha256']) == 64
    assert writer['endpoint'] == 'https://openrouter.ai/api/v1/chat/completions'


def test_run_overrides_win_role_environment_then_profile():
    settings = module().load_settings(environ={'DEEP_RESEARCH_WRITER_MAX_TOKENS': '18000'},
        overrides={'roles': {'writer': {'max_tokens': 20000}}})
    assert settings.resolve('writer')['max_tokens'] == 20000
    assert settings.resolve('writer', {'max_tokens': 22000})['max_tokens'] == 22000
    assert module().load_settings(environ={'DEEP_RESEARCH_WRITER_MAX_TOKENS': '18000'}).resolve('writer')['max_tokens'] == 18000


@pytest.mark.parametrize('override', [
    {'acceptance': False}, {'execution': {'acceptance_checks': False}},
    {'roles': {'writer': {'min_words': 0}}}, {'roles': {'unknown': {'model': 'x'}}},
    {'execution': {'endpoint': 'https://evil.invalid'}},
    {'roles': {'writer': {'model': 'unknown/model'}}},
    {'roles': {'writer': {'model': 'google/gemini-3.8-flash'}}},
    {'roles': {'visual_reviewer': {'model': 'deepseek/deepseek-v4-pro'}}},
    {'roles': {'writer': {'parameters': {'temperature': 0.3}}}},
    {'roles': {'writer': {'reasoning_effort': 'none'}}},
    {'roles': {'writer': {'max_tokens': 9999999}}},
    {'execution': {'concurrency': 0}}, {'execution': {'attempts': True}},
    {'execution': {'timeout_seconds': -1}},
    {'execution': {'worker_duration_seconds': 1, 'timeout_seconds': 2}},
])
def test_unsafe_or_unknown_settings_rejected(override):
    with pytest.raises(ValueError):
        module().load_settings(environ={}, overrides=override)


@pytest.mark.parametrize('env', [
    {'DEEP_RESEARCH_WRITER_BAD_FIELD': 'x'},
    {'DEEP_RESEARCH_ACCEPTANCE_CHECKS': 'false'},
    {'DEEP_RESEARCH_EXECUTION_CONCURRENCY': 'false'},
    {'DEEP_RESEARCH_WRITER_PARAMETERS': '{"acceptance": false}'},
    {'DEEP_RESEARCH_WRITER_MAX_TOKENS': 'not-an-int'},
    {'DEEP_RESEARCH_PROFILE': 'not-a-profile'},
])
def test_invalid_environment_cannot_bypass_validation(env):
    with pytest.raises(ValueError):
        module().load_settings(environ=env)


def test_review_resolution_excludes_same_developer_and_unsafe_fallback():
    settings = module().load_settings(environ={})
    with pytest.raises(ValueError, match='independence'):
        settings.resolve('brief_reviewer', writer_developers=['openai'])
    with pytest.raises(ValueError):
        module().load_settings(environ={}, overrides={'roles': {
            'brief_reviewer': {'fallback_models': ['google/gemini-3.8-flash']}}})


def test_freeze_is_nonsecret_and_cannot_change_on_resume(tmp_path):
    settings = module().load_settings(environ={'OPENROUTER_API_KEY': 'do-not-freeze'})
    path = tmp_path / 'effective.json'
    settings.freeze(path)
    data = json.loads(path.read_text())
    assert 'do-not-freeze' not in path.read_text()
    assert data['roles']['writer']['catalogue']['source_sha256']
    settings.freeze(path)
    changed = module().load_settings(environ={}, overrides={'execution': {'concurrency': 2}})
    with pytest.raises(ValueError, match='frozen'):
        changed.freeze(path)
    assert json.loads(path.read_text()) == data


def test_resolved_dicts_cannot_mutate_settings():
    settings = module().load_settings(environ={})
    route = settings.resolve('writer')
    route['catalogue']['model']['context_length'] = 1
    route['parameters']['fake'] = True
    assert settings.resolve('writer')['catalogue']['model']['context_length'] >= 100000
    assert 'fake' not in settings.resolve('writer')['parameters']


@pytest.mark.parametrize('value', [
    {'type': 'text', 'unexpected': True},
    {'type': 'json_object', 'schema': {}},
    {'type': 'json_schema'},
    {'type': 'json_schema', 'json_schema': []},
    {'type': 'json_schema', 'json_schema': {'name': 'unit'}},
    {'type': 'json_schema', 'json_schema': {'schema': {}}},
    {'type': 'json_schema', 'json_schema': {'name': '', 'schema': {}}},
    {'type': 'json_schema', 'json_schema': {'name': 'bad name', 'schema': {}}},
    {'type': 'json_schema', 'json_schema': {'name': 'unit', 'schema': {}, 'extra': 1}},
    {'type': 'json_schema', 'json_schema': {'name': 'unit', 'schema': {}, 'strict': 1}},
    {'type': 'json_schema', 'json_schema': {'name': 'unit', 'schema': {}, 'description': []}},
    {'type': 'json_schema', 'json_schema': {'name': 'unit', 'schema': 'not a schema'}},
    {'type': 'json_schema', 'json_schema': {'name': 'unit', 'schema': {'type': 'bogus'}}},
    {'type': 'json_schema', 'json_schema': {'name': 'unit', 'schema': {'properties': []}}},
    {'type': 'json_schema', 'json_schema': {'name': 'unit', 'schema': {'properties': {'x': {'type': 5}}}}},
    {'type': 'json_schema', 'json_schema': {'name': 'unit', 'schema': {'required': 'x'}}},
    {'type': 'json_schema', 'json_schema': {'name': 'unit', 'schema': {'minimum': float('nan')}}},
])
def test_response_format_rejects_unknown_wrapper_keys_and_invalid_nested_schemas(value):
    with pytest.raises(ValueError, match='response_format'):
        module().load_settings(environ={}, overrides={'roles': {
            'brief_reviewer': {'parameters': {'response_format': value}}}})


@pytest.mark.parametrize('value', [
    {'type': 'text'}, {'type': 'json_object'},
    {'type': 'json_schema', 'json_schema': {'name': 'unit_schema', 'strict': True,
        'description': 'Synthetic unit schema', 'schema': {
            'type': 'object', 'properties': {'x': {'type': ['string', 'null']}},
            'required': ['x'], 'additionalProperties': False}}},
])
def test_response_format_accepts_supported_shapes_unchanged(value):
    conf = module().load_settings(environ={}, overrides={'roles': {
        'brief_reviewer': {'parameters': {'response_format': value}}}})
    assert conf.resolve('brief_reviewer')['parameters']['response_format'] == value


def test_json_schema_validation_fails_closed_if_validator_is_unavailable(monkeypatch):
    import builtins
    original = builtins.__import__
    def without_jsonschema(name, *args, **kwargs):
        if name == 'jsonschema':
            raise ImportError('synthetic missing optional validator')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_jsonschema)
    with pytest.raises(ValueError, match='response_format.*jsonschema'):
        module().load_settings(environ={}, overrides={'roles': {'brief_reviewer': {
            'parameters': {'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'unit', 'schema': {'type': 'object'}}}}}}})


def test_catalogue_evidence_tampering_fails(tmp_path):
    resolver = module()
    source = Path(__file__).resolve().parent.parent / 'config'
    import yaml
    for p in source.iterdir():
        if p.suffix == '.yaml':
            (tmp_path / p.name).write_text(p.read_text())
    routing = yaml.safe_load((tmp_path / 'model-routing.yaml').read_text())
    routing['catalogue']['models']['anthropic/claude-opus-5.5']['context_length'] = 1
    (tmp_path / 'model-routing.yaml').write_text(yaml.safe_dump(routing))
    with pytest.raises(ValueError, match='catalogue'):
        module().load_settings(config_dir=tmp_path, environ={})
