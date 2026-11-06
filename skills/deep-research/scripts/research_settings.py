#!/usr/bin/env python3
"""Small, credential-free resolver for run-scoped research operating choices.

Configuration is data, not authority to weaken acceptance or formatting checks.
No network, credential reads, or global Hermes configuration changes occur here.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path

import yaml

ENDPOINT = 'https://openrouter.ai/api/v1/chat/completions'
ROLES = {'model', 'evidence', 'synthesis', 'writer', 'rewriting_editor',
         'brief_reviewer', 'reviewer', 'visual_reviewer'}
REVIEWERS = {'brief_reviewer', 'reviewer', 'visual_reviewer'}
ROLE_FIELDS = {'model', 'fallback_models', 'max_tokens', 'reasoning_effort', 'parameters'}
EXEC_FIELDS = {'concurrency', 'timeout_seconds', 'worker_duration_seconds', 'attempts',
               'retry_classes', 'fallback_classes', 'backoff_seconds', 'backoff_multiplier',
               'max_backoff_seconds', 'max_call_tokens'}
RETRY_CLASSES = {'rate_limit', 'transient', 'timeout', 'empty', 'truncated'}
FALLBACK_CLASSES = {'rate_limit', 'transient', 'timeout'}
PARAMETERS = {'temperature', 'top_p', 'seed', 'stop', 'response_format', 'verbosity'}
OPERATIONAL_ENV = {'DEEP_RESEARCH_COMPLETION_LAUNCH', 'DEEP_RESEARCH_COMPLETION_REQUESTER'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _keys(value, allowed, label, required=()):
    if not isinstance(value, dict) or set(value) - allowed or set(required) - set(value):
        raise ValueError(f'unknown or missing {label} keys')


def _number(value, label, minimum=0, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'invalid {label}')
    if value < minimum or (integer and not isinstance(value, int)):
        raise ValueError(f'invalid {label}')


def _response_format(value):
    _keys(value, {'type', 'json_schema'}, 'response_format', {'type'})
    kind = value['type']
    if kind in ('text', 'json_object'):
        _keys(value, {'type'}, 'response_format')
        return
    if kind != 'json_schema' or 'json_schema' not in value:
        raise ValueError('invalid response_format')
    spec = value['json_schema']
    _keys(spec, {'name', 'schema', 'strict', 'description'}, 'response_format json_schema',
          {'name', 'schema'})
    if (not isinstance(spec['name'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', spec['name']) or
            not isinstance(spec['schema'], dict) or
            ('strict' in spec and not isinstance(spec['strict'], bool)) or
            ('description' in spec and not isinstance(spec['description'], str))):
        raise ValueError('invalid response_format json_schema')
    try:
        from jsonschema import Draft202012Validator, SchemaError
    except ImportError:
        raise ValueError('response_format json_schema validation requires jsonschema') from None
    try:
        _canonical(spec['schema'])
        # Meta-schema validation only: never dereference user $ref URLs or parse replies.
        Draft202012Validator.check_schema(spec['schema'])
    except (SchemaError, TypeError, ValueError):
        raise ValueError('invalid response_format json_schema schema') from None


class _UniqueLoader(yaml.SafeLoader):
    """Reject duplicate keys instead of silently accepting last-wins safety data."""


def _mapping(loader, node):
    result = {}
    for key, value in node.value:
        k = loader.construct_object(key, deep=True)
        if k in result:
            raise ValueError(f'duplicate configuration key: {k}')
        result[k] = loader.construct_object(value, deep=True)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _read(path):
    try:
        return yaml.load(Path(path).read_text(), Loader=_UniqueLoader)
    except (yaml.YAMLError, OSError) as exc:
        raise ValueError(f'invalid settings file: {Path(path).name}') from exc


class Settings:
    def __init__(self, routing, execution, profile, roles):
        self._routing = copy.deepcopy(routing)
        self._execution = copy.deepcopy(execution)
        self._roles = copy.deepcopy(roles)
        self.profile = profile
        for role in ROLES:
            self.resolve(role)

    def resolve(self, role, overrides=None, writer_developers=()):
        if role not in ROLES:
            raise ValueError(f'unknown role: {role}')
        _keys(overrides or {}, ROLE_FIELDS, 'role override')
        route = copy.deepcopy(self._roles[role])
        route.update(copy.deepcopy(overrides or {}))
        _keys(route, ROLE_FIELDS, 'role', ROLE_FIELDS)
        if not isinstance(route['model'], str) or not isinstance(route['fallback_models'], list):
            raise ValueError('invalid model/fallback chain')
        chain = [route['model']] + route['fallback_models']
        if any(not isinstance(m, str) for m in chain) or len(chain) != len(set(chain)):
            raise ValueError('invalid model/fallback chain')
        _number(route['max_tokens'], 'max_tokens', 1, integer=True)
        if route['max_tokens'] > self._execution['max_call_tokens']:
            raise ValueError('max_tokens exceeds truncation ceiling')
        _keys(route['parameters'], PARAMETERS, 'generation parameter')
        for key, value in route['parameters'].items():
            if key in {'temperature', 'top_p'}:
                _number(value, key)
                if value > (2 if key == 'temperature' else 1):
                    raise ValueError(f'invalid {key}')
            elif key == 'seed':
                _number(value, key, minimum=0, integer=True)
            elif key == 'stop' and not (isinstance(value, str) or
                    isinstance(value, list) and all(isinstance(v, str) for v in value)):
                raise ValueError('invalid stop')
            elif key == 'verbosity' and value not in ('low', 'medium', 'high'):
                raise ValueError('invalid verbosity')
            elif key == 'response_format':
                _response_format(value)
        req = self._routing['requirements'][role]
        for model in chain:
            self.validate_model(model, role, route)
            developer = self.developer(model)
            if role in REVIEWERS and developer in writer_developers:
                raise ValueError('reviewer independence conflicts with actual writer developer')
        catalogue = self._routing['catalogue']
        route.update(role=role, endpoint=ENDPOINT, secret_reference='OPENROUTER_API_KEY',
                     requirements=copy.deepcopy(req), execution=copy.deepcopy(self._execution),
                     catalogue={k: copy.deepcopy(catalogue[k]) for k in
                                ('source_url', 'captured_at', 'source_sha256', 'evidence_sha256')})
        route['catalogue']['model'] = copy.deepcopy(catalogue['models'][route['model']])
        return route

    def developer(self, model):
        if not isinstance(model, str):
            return None
        return self._routing['model_policy'].get(model, {}).get('developer')

    def validate_model(self, model, role, route):
        """Validate both requested routes and exact served identities, never host prefixes."""
        row = self._routing['catalogue']['models'].get(model)
        policy = self._routing['model_policy'].get(model)
        req = self._routing['requirements'][role]
        if not row or not policy or not policy.get('developer'):
            raise ValueError('unknown catalogue model identity')
        if req['judgement'] and not policy['judgement']:
            raise ValueError('model unsuitable for judgement role')
        if policy['privacy'] != 'openrouter_data_collection_deny':
            raise ValueError('model privacy requirement not met')
        if row['context_length'] < req['min_context_tokens'] or not set(req['input_modalities']).issubset(
                row['architecture']['input_modalities']):
            raise ValueError('catalogue context/modalities do not meet role requirements')
        supported = set(row['supported_parameters'])
        if not set(route['parameters']).issubset(supported) or 'max_tokens' not in supported:
            raise ValueError('unsupported generation parameters')
        effort = route['reasoning_effort']
        if not isinstance(effort, str) or effort not in row.get('reasoning', {}).get('supported_efforts', []):
            raise ValueError('unsupported reasoning effort')
        if 'reasoning' not in supported or (req['judgement'] and effort not in ('high', 'xhigh', 'max')):
            raise ValueError('judgement requires supported high reasoning')
        ceiling = row['top_provider'].get('max_completion_tokens')
        if not ceiling or route['max_tokens'] > ceiling or route['max_tokens'] >= row['context_length']:
            raise ValueError('output allowance exceeds catalogue capability')

    def snapshot(self):
        return {'schema_version': 1, 'profile': self.profile,
                'roles': {role: self.resolve(role) for role in sorted(ROLES)}}

    def freeze(self, path):
        """Exclusive create; a resume must use exactly the original nonsecret settings."""
        path = Path(path)
        text = json.dumps(self.snapshot(), indent=2, sort_keys=True, allow_nan=False) + '\n'
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('x', encoding='utf-8') as handle:
                handle.write(text)
        except FileExistsError:
            if path.is_symlink() or path.read_text() != text:
                raise ValueError('effective settings are already frozen with different content')
        return path


def load_settings(config_dir=None, profile=None, overrides=None, environ=None):
    config = Path(config_dir) if config_dir else Path(__file__).resolve().parent.parent / 'config'
    routing = _read(config / 'model-routing.yaml')
    execution = _read(config / 'execution-policy.yaml')
    _keys(routing, {'schema_version', 'default_profile', 'endpoint', 'secret_reference',
                    'catalogue', 'model_policy', 'requirements', 'profiles'}, 'routing',
          {'catalogue', 'profiles', 'requirements', 'model_policy'})
    _keys(execution, {'schema_version', 'profiles'}, 'execution', {'schema_version', 'profiles'})
    if routing.get('schema_version') != 1 or execution['schema_version'] != 1 or routing.get('endpoint') != ENDPOINT or routing.get('secret_reference') != 'OPENROUTER_API_KEY':
        raise ValueError('unsupported schema or unapproved route/secret reference')
    cat = routing['catalogue']
    _keys(cat, {'source_url', 'captured_at', 'source_sha256', 'evidence_sha256', 'models'}, 'catalogue',
          {'source_url', 'captured_at', 'source_sha256', 'evidence_sha256', 'models'})
    if cat['source_url'] != 'https://openrouter.ai/api/v1/models' or not re.fullmatch(r'[0-9a-f]{64}', cat['source_sha256']):
        raise ValueError('invalid catalogue source binding')
    if hashlib.sha256(_canonical(cat['models']).encode()).hexdigest() != cat['evidence_sha256']:
        raise ValueError('catalogue evidence digest mismatch')
    if set(routing['requirements']) != ROLES or set(routing['model_policy']) != set(cat['models']):
        raise ValueError('invalid role/catalogue policy coverage')
    for model, policy in routing['model_policy'].items():
        _keys(policy, {'developer', 'judgement', 'privacy', 'latency_measurement'}, 'model policy',
              {'developer', 'judgement', 'privacy', 'latency_measurement'})
        if not isinstance(policy['judgement'], bool) or not isinstance(policy['developer'], str) or not policy['developer']:
            raise ValueError('invalid model policy')
    for role, req in routing['requirements'].items():
        _keys(req, {'min_context_tokens', 'input_modalities', 'judgement', 'acceptance_reviewer'},
              'role requirements', {'min_context_tokens', 'input_modalities', 'judgement', 'acceptance_reviewer'})
        _number(req['min_context_tokens'], 'min_context_tokens', 100000, integer=True)
        if req['judgement'] is not (role != 'evidence') or req['acceptance_reviewer'] is not (role in REVIEWERS):
            raise ValueError('configuration cannot disable judgement/acceptance requirements')
        needed = {'text', 'image'} if role == 'visual_reviewer' else {'text'}
        if not isinstance(req['input_modalities'], list) or not needed.issubset(req['input_modalities']):
            raise ValueError('role modalities cannot be weakened')
    env = os.environ if environ is None else environ
    profile = profile or env.get('DEEP_RESEARCH_PROFILE') or routing['default_profile']
    if profile not in routing['profiles'] or profile not in execution['profiles']:
        raise ValueError('unknown settings profile')
    for data in routing['profiles'].values():
        _keys(data, {'roles'}, 'profile', {'roles'})
        if set(data['roles']) != ROLES:
            raise ValueError('unknown/missing profile role')
        for data_role in data['roles'].values():
            _keys(data_role, ROLE_FIELDS, 'profile role', ROLE_FIELDS)
    roles = copy.deepcopy(routing['profiles'][profile]['roles'])
    policy = copy.deepcopy(execution['profiles'][profile])
    _keys(overrides or {}, {'roles', 'execution'}, 'run override')
    for key, raw in env.items():
        if not key.startswith('DEEP_RESEARCH_') or key in OPERATIONAL_ENV or key == 'DEEP_RESEARCH_PROFILE':
            continue
        matched = False
        for role in ROLES:
            prefix = f'DEEP_RESEARCH_{role.upper()}_'
            if key.startswith(prefix):
                field = key[len(prefix):].lower()
                if field not in ROLE_FIELDS:
                    raise ValueError(f'unknown role environment setting: {key}')
                try:
                    roles[role][field] = raw if field in {'model', 'reasoning_effort'} else json.loads(raw)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(f'invalid environment setting: {key}') from exc
                matched = True
                break
        if not matched:
            prefix = 'DEEP_RESEARCH_EXECUTION_'
            if not key.startswith(prefix) or key[len(prefix):].lower() not in EXEC_FIELDS:
                raise ValueError(f'unknown environment setting: {key}')
            try:
                policy[key[len(prefix):].lower()] = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f'invalid environment setting: {key}') from exc
    override_roles = (overrides or {}).get('roles', {})
    _keys(override_roles, ROLES, 'role override')
    for role, data in override_roles.items():
        _keys(data, ROLE_FIELDS, 'role override')
        roles[role].update(copy.deepcopy(data))
    _keys((overrides or {}).get('execution', {}), EXEC_FIELDS, 'execution override')
    policy.update(copy.deepcopy((overrides or {}).get('execution', {})))
    _keys(policy, EXEC_FIELDS, 'execution', EXEC_FIELDS)
    for field in EXEC_FIELDS - {'retry_classes', 'fallback_classes'}:
        _number(policy[field], field, 1 if field in {'concurrency', 'attempts', 'max_call_tokens'} else 0,
                integer=field in {'concurrency', 'attempts', 'max_call_tokens'})
    if policy['timeout_seconds'] <= 0 or policy['worker_duration_seconds'] < policy['timeout_seconds'] or policy['backoff_multiplier'] < 1 or policy['max_backoff_seconds'] < policy['backoff_seconds']:
        raise ValueError('invalid execution policy combination')
    for field, allowed in [('retry_classes', RETRY_CLASSES), ('fallback_classes', FALLBACK_CLASSES)]:
        if not isinstance(policy[field], list) or any(not isinstance(v, str) or v not in allowed for v in policy[field]):
            raise ValueError(f'invalid {field}')
    if not set(policy['fallback_classes']).issubset(policy['retry_classes']):
        raise ValueError('fallback classes must also be retryable')
    return Settings(routing, policy, profile, roles)
