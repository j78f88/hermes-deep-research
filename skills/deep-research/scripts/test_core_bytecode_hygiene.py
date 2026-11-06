"""Normal completion is read-only without weakening loaded-source/cache pinning."""
import json
import subprocess
import sys
import shutil
from pathlib import Path

import pytest

from test_acceptance_checker_dependencies import checker_repo, dynamic_helper, commit


@pytest.mark.parametrize('entry', ['plain_brief_rewrite.py', 'verify_campaign_depth.py', 'complete_campaign.py'])
def test_normal_readonly_entrypoint_import_does_not_create_bytecode(tmp_path, entry):
    source = Path(__file__).parent
    scripts = tmp_path / 'repo/research/deep-research/scripts'
    scripts.mkdir(parents=True)
    for path in source.glob('*.py'):
        shutil.copyfile(path, scripts / path.name)
    refs = scripts.parent / 'references'; refs.mkdir()
    shutil.copyfile(source.parent / 'references/reader-pair-style.md', refs / 'reader-pair-style.md')
    proc = subprocess.run([sys.executable, str(scripts / entry), '--help'], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert not list(tmp_path.rglob('*.pyc'))


def test_read_repository_style_and_settings_are_pinned(checker_repo):
    root, scripts, cc = checker_repo
    ref = scripts.parent / 'references/reader-style.md'
    config = scripts.parent / 'config/policy.yaml'
    for path in (ref, config):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Original policy.\n')
    (scripts / 'consumer.py').write_text('from pathlib import Path\n' +
        f'STYLE = Path({str(ref)!r}).read_text()\n' +
        f'SETTINGS = Path({str(config)!r}).read_text()\n')
    commit(root)
    cc._load('consumer')
    config.write_text('Changed policy.\n')
    result = cc.check_checker_code()
    assert result['verdict'] == 'HOLD'
    assert config.relative_to(root).as_posix() in result['dependencies_sha256']
    assert ref.relative_to(root).as_posix() in result['dependencies_sha256']


def test_normal_completion_dependency_load_does_not_create_cache_or_dirty_repo(checker_repo):
    root, scripts, _ = checker_repo
    helper = dynamic_helper(scripts)
    commit(root)
    code = (
        'import json, runpy, sys; ns=runpy.run_path(sys.argv[1]); '
        'ns["_load"]("consumer"); print(json.dumps(ns["check_checker_code"]()))'
    )
    proc = subprocess.run([sys.executable, '-c', code, str(scripts / 'complete_campaign.py')],
                          capture_output=True, text=True, check=True)
    result = json.loads(proc.stdout)
    assert result['verdict'] == 'PASS', result
    assert not list(root.rglob('*.pyc'))
    assert helper.relative_to(root).as_posix() in result['dependencies_sha256']
    assert subprocess.run(['git', '-C', str(root), 'status', '--porcelain'],
                          capture_output=True, text=True, check=True).stdout == ''
