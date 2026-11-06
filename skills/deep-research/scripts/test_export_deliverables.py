"""Portable flat exports retain policy, confinement and integrity checks."""
import json
from pathlib import Path

import pytest

import export_deliverables as exporter
from test_two_outputs import _export, _export_v2, bind_fixture_review_packet, read_manifest, refresh_fixture_acceptance_bindings, write_manifest


def test_flat_export_creates_only_the_two_reviewed_files(tmp_path):
    root, destination = _export_v2(tmp_path)
    assert not destination.exists()
    result = _export(root, destination)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((root / 'manifest.json').read_text())
    expected = {Path(item['export_path']) for item in manifest['deliverable_exports']['exports']}
    assert set(destination.iterdir()) == expected
    assert len(expected) == 2
    assert all(path.parent == destination and path.is_file() for path in expected)
    for item in manifest['deliverable_exports']['exports']:
        assert exporter.sha256_path(Path(item['export_path'])) == item['sha256'] == item['readback_sha256']


@pytest.mark.parametrize('policy', [[], 'source/adapted', [None], ['']])
def test_explicit_tag_policy_must_be_valid(policy):
    with pytest.raises(ValueError, match='allowed_tags'):
        exporter.load_allowed_tags({'allowed_tags': policy})


def test_tag_policy_is_optional_and_manifest_scoped(tmp_path):
    assert exporter.load_allowed_tags({}) is None
    assert exporter.load_allowed_tags({'allowed_tags': ['source/adapted']}) == {'source/adapted'}
    note = tmp_path / 'note.md'
    note.write_text('---\ntitle: Reader\ncreated: 2026-01-01\ntype: source\nstage: final\n'
                    'source: https://example.com\nauthor: Research\ncaptured_via: research\n'
                    'tags: [topic/custom]\nrelated:\n  - "[[companion]]"\n---\nReader text.\n')
    exporter.validate_internal_note(note)
    with pytest.raises(ValueError, match='off-vocabulary'):
        exporter.validate_internal_note(note, {'source/adapted'})


def test_nested_export_destination_is_rejected_before_copy(tmp_path):
    root, destination = _export_v2(tmp_path)
    manifest = read_manifest(root)
    manifest['deliverable_exports']['reference_pack_intended_export_path'] = str(destination / 'nested' / 'reference-pack.md')
    write_manifest(root, manifest)
    refresh_fixture_acceptance_bindings(root)
    bind_fixture_review_packet(root)
    result = _export(root, destination)
    assert result.returncode == 3
    assert 'directly under export root' in result.stdout
    assert not destination.exists()


def test_symlink_export_root_is_rejected_before_copy(tmp_path):
    root, destination = _export_v2(tmp_path)
    actual = tmp_path / 'actual'
    actual.mkdir()
    destination.symlink_to(actual, target_is_directory=True)
    result = _export(root, destination)
    assert result.returncode == 3
    assert 'symlink' in result.stdout
    assert list(actual.iterdir()) == []


@pytest.mark.parametrize('text', ['/home/example/arbitrary/secret.md', '/Users/example/Documents/secret.md'])
def test_private_home_paths_are_rejected_without_product_specific_rules(text):
    assert exporter.PRIVATE_PATTERNS['private_path'].search(text)


@pytest.mark.parametrize('declaration, expected', [
    ('tags: [research, topic/deep/nested]', {'research', 'topic/deep/nested'}),
    ('tags:\n  - research\n  - "topic/deep/nested"', {'research', 'topic/deep/nested'}),
])
def test_complete_tag_values_are_parsed(declaration, expected):
    assert exporter.declared_tags(declaration + '\nrelated:\n  - "[[companion]]"') == expected


@pytest.mark.parametrize('declaration', ['tags: research', 'tags: [*alias]', 'tags: [topic with spaces]'])
def test_unsupported_tag_syntax_fails_closed(declaration):
    with pytest.raises(ValueError, match='tags must|unsupported tag syntax'):
        exporter.declared_tags(declaration)


@pytest.mark.parametrize('target', ['self', 'nonexistent', ''])
def test_actual_companion_is_required_not_just_any_link(tmp_path, target):
    note = tmp_path / 'self.md'
    note.write_text('---\ntitle: Reader\ncreated: 2026-01-01\ntype: source\nstage: final\n'
                    'source: https://example.com\nauthor: Research\ncaptured_via: research\n'
                    'tags: [research]\nrelated: []\n---\n' + (f'[[{target}]]' if target else 'No link.'))
    with pytest.raises(ValueError, match='companion deliverable link missing'):
        exporter.validate_internal_note(note, companion_stems={'companion'})


@pytest.mark.parametrize('declaration', ['tags: [unapproved]', 'tags:\n  - unapproved', 'tags: [topic/deep/nested]'])
def test_manifest_tag_policy_checks_every_complete_tag(tmp_path, declaration):
    note = tmp_path / 'self.md'
    note.write_text('---\ntitle: Reader\ncreated: 2026-01-01\ntype: source\nstage: final\n'
                    'source: https://example.com\nauthor: Research\ncaptured_via: research\n'
                    + declaration + '\nrelated:\n  - "[[companion]]"\n---\nReader text.\n')
    with pytest.raises(ValueError, match='off-vocabulary'):
        exporter.validate_internal_note(note, {'research'}, companion_stems={'companion'})
