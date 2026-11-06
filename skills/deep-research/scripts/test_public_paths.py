"""Public path defaults resolve inside isolated homes without external writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parent


@pytest.mark.parametrize("configured", [False, True])
def test_scaffold_resolves_archive_and_export_paths(tmp_path, configured):
    home = tmp_path / 'home'
    home.mkdir()
    env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
    env.pop('RESEARCH_ARCHIVE_ROOT', None)
    env.pop('DELIVERABLES_EXPORT_PATH', None)
    if configured:
        env.update(RESEARCH_ARCHIVE_ROOT='~/custom-archive', DELIVERABLES_EXPORT_PATH='~/custom-export')
    result = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'init_campaign.py'),
        '--target', 'Public paths', '--date', '2026-01-01'],
        cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    archive = home / ('custom-archive' if configured else '.hermes/research-archive')
    campaign = archive / '2026-01-01-public-paths'
    manifest = json.loads((campaign / 'manifest.json').read_text())
    assert manifest['deliverable_exports']['export_root'] == str(home / ('custom-export' if configured else 'research-deliverables'))
    assert not (tmp_path / 'RESEARCH_ARCHIVE_ROOT').exists()
    assert not (tmp_path / 'DELIVERABLES_EXPORT_PATH').exists()


@pytest.mark.parametrize("configured", [False, True])
def test_plain_brief_resolves_export_default(tmp_path, monkeypatch, configured):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('DELIVERABLES_EXPORT_PATH', raising=False)
    if configured:
        monkeypatch.setenv('DELIVERABLES_EXPORT_PATH', '~/custom-export')
    spec = importlib.util.spec_from_file_location('public_path_plain', SCRIPTS / 'plain_brief_rewrite.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.EXPORT_ROOT == tmp_path / ('custom-export' if configured else 'research-deliverables')


def test_public_release_has_no_private_historical_admissions():
    import report_purpose
    assert report_purpose.LEGACY_CAMPAIGNS == []
