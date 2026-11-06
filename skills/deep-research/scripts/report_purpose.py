"""Explicit report purpose and a frozen, byte-bound historical exception.

No discovery, private context reads or topic-based inference. Copies of the exact
admitted historical manifest remain compatible. Only the five approved exporter
bookkeeping fields may change without explicit purpose; all semantic fields bind. Never add a blanket fixture/legacy-layout exception here.
"""

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code
import hashlib
import json
from pathlib import Path

# No private historical campaign identities are distributed.
LEGACY_CAMPAIGNS = []


def semantic_manifest_sha256(manifest):
    """Bind every semantic field, excluding only the five approved export fields."""
    frozen = json.loads(json.dumps(manifest))
    exports = frozen.get('deliverable_exports')
    if isinstance(exports, dict):
        for key in ('status', 'exported_at', 'exports', 'prior_exports', 'overwrite_approved'):
            exports.pop(key, None)
    encoded = json.dumps(frozen, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def validate_report_purpose(value):
    """Validate literal new-campaign purpose. Caller supplies the factual default."""
    if value not in ('factual', 'decision_assessment'):
        raise ValueError('report_purpose must be factual or decision_assessment; business_assessment is unsupported')
    return value


def resolve_report_purpose(manifest, root=None):
    """Return explicit purpose, or legacy only for a frozen historical manifest."""
    if not isinstance(manifest, dict):
        raise ValueError('report_purpose requires a manifest object')
    if 'report_purpose' in manifest:
        return validate_report_purpose(manifest['report_purpose'])
    if root is not None:
        path = Path(root) / 'manifest.json'
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            try:
                same_manifest = json.loads(raw) == manifest
            except (ValueError, UnicodeError):
                same_manifest = False
            if same_manifest and any(
                (entry['manifest_sha256'] == digest
                 or entry.get('semantic_sha256') == semantic_manifest_sha256(manifest))
                and all(manifest.get(key) == value for key, value in entry['identity'].items())
                for entry in LEGACY_CAMPAIGNS
            ):
                return 'legacy'
    raise ValueError('report_purpose missing: campaign is not in the frozen historical inventory')
