"""Normal commands and import APIs keep source and derived caches pinned.

Run fresh children without -B or PYTHONDONTWRITEBYTECODE: the caller's pytest
interpreter must not accidentally hide the ordinary-command regression.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_acceptance_checker_dependencies import checker_repo, commit

SCRIPTS = Path(__file__).resolve().parent
MODULES = sorted(p.stem for p in SCRIPTS.glob('*.py')
                 if not p.name.startswith('test_') and p.name not in {'reader_test_support.py', 'publication_test_support.py', 'conftest.py'})


def run_child(scripts, *args):
    env = {key: os.environ[key] for key in ('PATH', 'TMPDIR', 'PLAIN_WORDS_PATH') if key in os.environ}
    home = scripts.parents[3] / 'isolated-home'
    home.mkdir(exist_ok=True)
    env['HOME'] = str(home)
    return subprocess.run([sys.executable, *args], cwd=scripts, env=env,
                          capture_output=True, text=True, timeout=90)


@pytest.fixture
def workflow_repo(tmp_path):
    root = tmp_path / 'repo'
    scripts = root / 'research/deep-research/scripts'
    scripts.mkdir(parents=True)
    ignore = SCRIPTS.parent / '.gitignore'
    if ignore.exists():
        shutil.copyfile(ignore, scripts.parent / '.gitignore')
    for path in SCRIPTS.glob('*.py'):
        if not path.name.startswith('test_') and path.name not in {'reader_test_support.py', 'publication_test_support.py', 'conftest.py'}:
            shutil.copyfile(path, scripts / path.name)
    for name in ('references', 'config'):
        shutil.copytree(SCRIPTS.parent / name, scripts.parent / name)
    def git(*args):
        subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
    git('init', '-q')
    git('add', '--all')
    git('-c', 'user.name=Workflow Test', '-c', 'user.email=workflow@test.invalid',
        'commit', '-qm', 'fixture')
    return root, scripts


def test_skill_local_ignore_and_no_tracked_generated_bytecode():
    root = SCRIPTS.parents[2]
    skill_rel = SCRIPTS.parent.relative_to(root).as_posix()
    tracked = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--',
                              skill_rel], check=True, capture_output=True).stdout.decode().split('\0')
    assert not [p for p in tracked if '__pycache__' in Path(p).parts or p.endswith(('.pyc', '.pyo'))]
    for rel in ('scripts/__pycache__/probe.cpython-313.pyc', 'scripts/__pycache__/probe.cpython-314.pyc',
                'scripts/probe.pyc', 'scripts/probe.pyo', 'scripts/probe.pyd'):
        result = subprocess.run(['git', '-C', str(root), 'check-ignore', '--no-index', '--verbose',
                                 skill_rel + '/' + rel], capture_output=True, text=True)
        assert result.returncode == 0, rel
        assert skill_rel + '/.gitignore' in result.stdout
    assert subprocess.run(['git', '-C', str(root), 'check-ignore', '--quiet', '--no-index',
                           skill_rel + '/scripts/probe.py']).returncode == 1

def test_ordinary_imports_and_scoped_sync_add_do_not_stage_caches(workflow_repo):
    root, scripts = workflow_repo
    for _ in range(2):
        proc = run_child(scripts, '-c', 'import campaign_controller, receipt_integrity; '
                         'import complete_campaign as cc, json; print(json.dumps(cc.check_checker_code()))')
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)['verdict'] == 'PASS'
        caches = list(scripts.rglob('*.pyc'))
        assert caches, 'exercise ordinary imports, not a hidden -B canary'
        for cache in caches:
            assert subprocess.run(['git', '-C', str(root), 'check-ignore', '--quiet', str(cache)]).returncode == 0
        subprocess.run(['git', '-C', str(root), 'add', '--all', '--', 'research/deep-research'], check=True)
        assert subprocess.run(['git', '-C', str(root), 'diff', '--cached', '--exit-code'], capture_output=True).returncode == 0


@pytest.mark.parametrize('module', MODULES)
def test_normal_import_api_keeps_executable_dependencies_pinned(workflow_repo, module):
    root, scripts = workflow_repo
    proc = run_child(scripts, '-c',
                     f'import {module}; import complete_campaign as cc; '
                     'import json; print(json.dumps(cc.check_checker_code()))')
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result['verdict'] == 'PASS', result
    for cache in root.rglob('*.pyc'):
        assert cache.relative_to(root).as_posix() in result['dependencies_sha256']
    assert result['loaded_code_sha256'], 'actual executed module code must be attested'


def test_normal_init_then_offline_completion_keeps_checker_pinned(workflow_repo, tmp_path):
    root, scripts = workflow_repo
    campaign = tmp_path / 'campaign'
    init = run_child(scripts, 'init_campaign.py', '--report-purpose', 'factual',
                     '--target', 'Offline bytecode hygiene control', '--root', str(campaign),
                     '--export-root', str(tmp_path / 'export'), '--no-deliverable-export')
    assert init.returncode == 0, init.stderr
    acquired = run_child(scripts, 'acquire_youtube_apify.py', 'abcdefghijk', '--dry-run',
                         '--env-file', str(tmp_path / 'absent.env'), '--out', str(tmp_path / 'corpus'))
    assert acquired.returncode == 0, acquired.stderr
    quotes = run_child(scripts, 'quote_check.py', '--campaign-root', str(campaign), '--write')
    assert quotes.returncode in (0, 2, 3), quotes.stderr
    state = run_child(scripts, '-c',
                      'import campaign_controller as c, sys; '
                      'c.save_state(sys.argv[1], c.load_state(sys.argv[1]))', str(campaign))
    assert state.returncode == 0, state.stderr
    publication = run_child(scripts, 'run_publication_reviews.py', '--campaign-root', str(campaign),
                            '--preflight-only')
    assert publication.returncode == 2, publication.stderr  # no research acceptance on a scaffold
    export = run_child(scripts, 'export_deliverables.py', '--campaign-root', str(campaign), '--dry-run')
    assert export.returncode == 2, export.stderr  # no export acceptance on a scaffold
    complete = run_child(scripts, 'complete_campaign.py', '--campaign-root', str(campaign),
                         '--offline', '--no-relaunch', '--no-write')
    assert complete.returncode in (2, 3), complete.stderr  # scaffold is not completed research
    result = json.loads(complete.stdout)
    checker = next(step for step in result['steps'] if step['step'] == 'checker_code')
    assert checker['verdict'] == 'PASS', checker
    for cache in root.rglob('*.pyc'):
        if cache.name.startswith(('preseed.', 'report_purpose.')):
            assert cache.relative_to(root).as_posix() in checker['dependencies_sha256']
    assert subprocess.run(['git', '-C', str(root), 'diff', '--exit-code', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout == ''


@pytest.mark.parametrize('cache_mode', ['timestamp', 'checked_hash', 'unchecked_hash'])
def test_untracked_cache_must_derive_exactly_from_committed_source(checker_repo, cache_mode):
    import importlib._bootstrap_external as bootstrap
    import importlib.util
    root, scripts, cc = checker_repo
    source = scripts / 'consumer.py'
    source.write_text('VALUE = 42\n')
    commit(root)
    code = compile(source.read_bytes(), str(source), 'exec', dont_inherit=True)
    if cache_mode == 'timestamp':
        payload = bootstrap._code_to_timestamp_pyc(code, int(source.stat().st_mtime), source.stat().st_size)
    else:
        payload = bootstrap._code_to_hash_pyc(code, importlib.util.source_hash(source.read_bytes()),
                                              checked=cache_mode == 'checked_hash')
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir(exist_ok=True)
    cache.write_bytes(payload)
    assert subprocess.run(['git', '-C', str(root), 'check-ignore', '--quiet', str(cache)]).returncode == 0
    assert cc._load('consumer').VALUE == 42
    result = cc.check_checker_code()
    assert result['verdict'] == 'PASS', result
    assert cache.relative_to(root).as_posix() in result['dependencies_sha256']
    cache.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    result = cc.check_checker_code()
    assert result['verdict'] == 'HOLD', result


@pytest.mark.parametrize('tamper', ['source', 'cache'])
def test_loaded_executable_cannot_be_hidden_by_restoring_source_and_cache(checker_repo, tamper):
    import importlib._bootstrap_external as bootstrap
    import importlib.util
    root, scripts, cc = checker_repo
    source = scripts / 'consumer.py'
    original = b'VALUE = 42\n'
    source.write_bytes(original)
    commit(root)
    cache = Path(importlib.util.cache_from_source(str(source)))
    if tamper == 'source':
        source.write_bytes(b'VALUE = 43\n')
    else:
        cache.parent.mkdir(exist_ok=True)
        payload = bootstrap._code_to_timestamp_pyc(compile('VALUE = 43\n', str(source), 'exec'),
                                                  int(source.stat().st_mtime), len(original))
        cache.write_bytes(payload)
        assert subprocess.run(['git', '-C', str(root), 'check-ignore', '--quiet', str(cache)]).returncode == 0
    # Harmless changed constant only, inside the isolated fixture repository.
    assert cc._load('consumer').VALUE == 43
    source.write_bytes(original)
    if cache.exists():
        cache.write_bytes(bootstrap._code_to_timestamp_pyc(compile(original, str(source), 'exec'),
                                                           int(source.stat().st_mtime), len(original)))
    result = cc.check_checker_code()
    assert result['verdict'] == 'HOLD', result
    assert any('loaded executable differs from HEAD' in reason for reason in result['reasons']), result


@pytest.mark.parametrize('tamper', ['magic', 'flags', 'timestamp', 'trailing', 'unobserved'])
def test_untracked_cache_is_not_accepted_on_header_or_source_name_alone(checker_repo, tamper):
    import importlib._bootstrap_external as bootstrap
    import importlib.util
    root, scripts, cc = checker_repo
    source = scripts / 'consumer.py'
    source.write_text('VALUE = 42\n')
    commit(root)
    if tamper != 'unobserved':
        cc._load('consumer')
    payload = bytes(bootstrap._code_to_timestamp_pyc(compile(source.read_bytes(), str(source), 'exec'),
                                                    int(source.stat().st_mtime), source.stat().st_size))
    if tamper == 'magic':
        payload = b'BAD!' + payload[4:]
    elif tamper == 'flags':
        payload = payload[:4] + (7).to_bytes(4, 'little') + payload[8:]
    elif tamper == 'timestamp':
        payload = payload[:8] + b'\x00' * 8 + payload[16:]
    elif tamper == 'trailing':
        payload += b'not derived from source'
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir(exist_ok=True)
    cache.write_bytes(payload)
    assert subprocess.run(['git', '-C', str(root), 'check-ignore', '--quiet', str(cache)]).returncode == 0
    cc._remember_cached(str(cache))
    result = cc.check_checker_code()
    assert result['verdict'] == 'HOLD', result
    assert cache.name in ' '.join(result['reasons'])


def test_verifier_child_attests_code_before_source_is_restored(checker_repo):
    root, scripts, cc = checker_repo
    helper = scripts / 'child_helper.py'
    original = 'VALUE = 42\n'
    helper.write_text(original)
    (scripts / 'verify_campaign_depth.py').write_text(
        'import importlib.util, json\nfrom pathlib import Path\n'
        f'spec = importlib.util.spec_from_file_location("child_helper", {str(helper)!r})\n'
        'module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n'
        f'Path({str(helper)!r}).write_text({original!r})\n'
        'print(json.dumps({"status": "PASS", "classification": "ok"}))\n')
    commit(root)
    helper.write_text('VALUE = 43\n')
    assert cc.check_verifier(root)['verdict'] == 'PASS'
    assert helper.read_text() == original
    result = cc.check_checker_code()
    assert result['verdict'] == 'HOLD', result
    assert any('child_helper.py: loaded executable differs from HEAD' in reason
               for reason in result['reasons']), result
