#!/usr/bin/env python3
"""Rewrite existing research packs as plain briefs the owner can read in ten minutes.

Usage:
  python3 plain_brief_rewrite.py --manifest packs.json [--publish] [--only KEY ...]
  python3 plain_brief_rewrite.py --campaign-root ROOT     # canonical true_deep_research step
  python3 plain_brief_rewrite.py --check FILE             # lint only; exit 1 on failure

--campaign-root writes the plain brief for a finished campaign to
internal/plain-brief.md (or manifest plain_brief.path) and the review record to
qa/plain-brief-review.json. Run it only after the full pack (layout v2: the reference pack) is final; the
review record binds the brief to the full-pack hash so later edits make it stale.

packs.json: {"KEY": {"title": "...", "slug": "kebab-slug", "date": "YYYY-MM-DD",
                     "sources": ["export-relative or absolute paths"], "intent": "what the owner asked for"}}
Without --publish, briefs are written under --out only. Deprecated --publish refuses loose
packs: it requires an explicit original campaign, its purpose, exact source paths, intended
brief destination and byte-identical accepted reader under --out. It delegates to the guarded
campaign exporter; it never changes source notes, commits or pushes.
Models, concurrency and retry policy come from the frozen shared settings. Up to two fix rounds.
"""

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code
import argparse, hashlib, json, os, re, subprocess, time, pathlib, urllib.request, concurrent.futures as cf
import contextlib
import contextvars
import copy
import uuid
from types import SimpleNamespace
from datetime import datetime, timezone

import sys
# Keep script execution read-only with respect to repository caches. Existing
# caches remain in the completion dependency inventory and must match HEAD.
sys.dont_write_bytecode = True

import model_client
from research_settings import load_settings
from research_qa_repair import (campaign_readiness, build_pending_editor_decisions, confined_output_path,
                                build_review_packet, verify_review_packet, JUDGEMENT_FIELDS,
                                served_review_identity_errors, _history_findings)

EXPORT_ROOT = pathlib.Path(os.environ.get('DELIVERABLES_EXPORT_PATH', '~/research-deliverables')).expanduser()
AUTO = EXPORT_ROOT
# Compatibility role aliases, not model choices. Models come only from resolved settings.
WRITER, EDITOR = 'writer', 'brief_reviewer'
COST: list[float | None] = [None]
CALLS = []
MAX_WORDS = 2000  # cap only; human-facing briefs have no word floor
REVIEW_SCHEMA = 'deep-research-plain-brief-review-v1'
DEFAULT_BRIEF_REL = 'internal/plain-brief.md'
DEFAULT_REVIEW_REL = 'qa/plain-brief-review.json'
DEFAULT_EDITOR_DECISIONS_REL = 'qa/editor-note-decisions.json'
FOOTER_MARK = '\n---\nDetail and'
_RUN = contextvars.ContextVar('plain_brief_run', default=None)


class CallText(str):
    """Text compatibility plus the shared client's served-identity acceptance result."""
    acceptance_eligible: bool

    def __new__(cls, content, acceptance_eligible=False):
        value = super().__new__(cls, content)
        value.acceptance_eligible = acceptance_eligible
        return value


@contextlib.contextmanager
def run_context(settings, raw_dir, *, writers=()):
    raw_dir = pathlib.Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    settings.freeze(raw_dir / 'effective-settings.json')
    state = SimpleNamespace(settings=settings, raw_dir=raw_dir, writers=copy.deepcopy(list(writers)),
                            calls=[], raw_files=[], next_call=0)
    token = _RUN.set(state)
    try:
        yield state
    finally:
        _RUN.reset(token)


def api_key():
    """Compatibility reference; shared client owns credential resolution."""
    return model_client._api_key()


def call(model, system, user, max_tokens=None, role='model', *, review_binding=None):
    """Use shared retry/routing policy; save raw attempts before application parsing."""
    state = _RUN.get()
    if state is None:
        raise RuntimeError('model calls require a frozen run_context')
    route_role = {'editor': 'brief_reviewer'}.get(role, role)
    model_override = None if model in (WRITER, EDITOR, None) else model
    state.next_call += 1
    raw_path = state.raw_dir / f'call-{state.next_call:04d}.raw.json'
    try:
        result = model_client.chat_completion(model_override, system, user, max_tokens=max_tokens,
            role=route_role, settings=state.settings, writers=state.writers)
    except model_client.ModelCallError as exc:
        raw_path.write_text(json.dumps({'calls': exc.calls, 'raw_responses': exc.raw_responses}, indent=2) + '\n')
        state.calls.extend(copy.deepcopy(exc.calls)); CALLS.extend(copy.deepcopy(exc.calls))
        state.raw_files.append(str(raw_path))
        raise
    raw_path.write_text(json.dumps({'calls': result.calls, 'raw_responses': result.raw_responses,
        'acceptance_eligible': result.acceptance_eligible,
        'review_binding': review_binding}, indent=2) + '\n')
    state.calls.extend(copy.deepcopy(result.calls)); CALLS.extend(copy.deepcopy(result.calls))
    state.raw_files.append(str(raw_path))
    if route_role in ('writer', 'rewriting_editor'):
        state.writers.extend(copy.deepcopy(result.calls))
    costs = [c.get('cost_usd') for c in CALLS]
    COST[0] = sum(costs) if costs and all(c is not None for c in costs) else None
    return CallText(result.content, result.acceptance_eligible)


LEGACY_SECTIONS = ['## What was asked', '## What we found', '## Options compared', '## What to do', '## Risks and when to stop', '## What is still missing', '## What the owner needs to decide']
BANNED = [r'\[S\d+', r'\[L\d', r'\bG[0-6]\b', r'\bcontroller\b', r'internal[_ ]context', r'external[_ ]evidence', r'ActionRegister', r'clean-room',
          r'\bfrozen\b', r'\bverifier\b', r'\bowner gate', r'\bPhase 0\b', r'\bExhibit\b', r'sha-?256', r'\bposture\b', r'\bleverage\b', r'\bspine\b', r'\bwedge\b', r'\bseams?\b']
# Research-process vocabulary that belongs in sidecar files, never in text the owner reads.
# Kept to compound/process-specific forms: plain words such as "receipt" or "provenance" are
# legitimate topic words in some briefs.
CITATION_CODE = re.compile(r'\[[A-Z]{1,4}\d+(?:[.:-][A-Z]{0,4}\d+)*(?:\s*@[^\]]*)?\]')  # [S12], [L3.S4], [L03-S2 @00:12:34]
PROCESS_TERMS = [r'\bAction ?Register\b', r'\bAR\d{1,3}\b',
                 r'\bcontroller[_ ]inference\b', r'\bcontext[ _-]brief\b', r'\breconciliation table\b',
                 r'\b(?:QA|verification|freeze|PASS|HOLD) receipts?\b', r'\bresearch lanes?\b', r'\blane (?:outputs?|packets?|workers?|plans?)\b',
                 r'\bworker packets?\b', r'\bprovenance (?:layers?|labels?)\b', r'\bevidence bundle\b', r'\b[0-9a-f]{40,64}\b']

def validate_c1_call_block(md, require_section=False):
    """Return errors for the C1 calls in ``What the owner needs to decide``."""
    heading_re = re.compile(r'^## What the owner needs to decide\s*$', re.I | re.M)
    headings = list(heading_re.finditer(md))
    if not headings:
        return ['C1 section missing: ## What the owner needs to decide'] if require_section else []
    if len(headings) != 1:
        return ['C1 section must appear exactly once']
    start = headings[0].end()
    next_heading = re.search(r'^##\s+.+$', md[start:], re.M)
    body = md[start:start + next_heading.start() if next_heading else len(md)].strip()
    if body == 'No call needed.':
        return []
    item_re = re.compile(r'^(\d+)\.\s+(.+)$', re.M)
    items = list(item_re.finditer(body))
    if not items:
        return ['C1 section must contain at least one numbered call']
    errors = []
    if body[:items[0].start()].strip():
        errors.append('C1 section must start with a numbered call, without introductory prose')
    required = ['why it matters', 'if you do nothing', 'suggested', 'sources']
    allowed = set(required + ['urgency'])
    for index, item in enumerate(items, 1):
        number = int(item.group(1))
        if number != index:
            errors.append(f'C1 call {index}: item number must be {index}')
        if not re.fullmatch(r'\*\*[^*\n]+\?\*\*', item.group(2).strip()):
            errors.append(f'C1 call {index}: use a bold question ending in ?')
        end = items[index].start() if index < len(items) else len(body)
        lines = [line for line in body[item.end():end].splitlines() if line.strip()]
        fields = []
        values = {}
        for line in lines:
            match = re.fullmatch(r'\s{3,}-\s+([^:]+):\s*(\S.*)', line)
            if not match:
                errors.append(f'C1 call {index}: every field must be one indented bullet on one line')
                continue
            label = match.group(1).strip().lower()
            if label not in allowed:
                errors.append(f'C1 call {index}: unknown field {match.group(1).strip()}')
                continue
            if label in values:
                errors.append(f'C1 call {index}: duplicate field {match.group(1).strip()}')
                continue
            fields.append(label)
            values[label] = match.group(2).strip()
        display_labels = {
            'why it matters': 'Why it matters',
            'if you do nothing': 'If you do nothing',
            'suggested': 'Suggested',
            'sources': 'Sources',
        }
        for label in required:
            if label not in values:
                errors.append(f'C1 call {index}: missing {display_labels[label]}')
        expected_order = ['why it matters', 'if you do nothing', 'suggested']
        if 'urgency' in values:
            expected_order.append('urgency')
        expected_order.append('sources')
        if fields != expected_order:
            errors.append(f'C1 call {index}: fields must follow the C1 order')
        suggested = values.get('suggested', '')
        if suggested and not re.fullmatch(r'(Yes|No|Later)\s+(?:-|–)\s+\S.*', suggested):
            errors.append(f'C1 call {index}: Suggested must be Yes, No, or Later, then an en dash or ASCII hyphen and one line')
        urgency = values.get('urgency')
        if urgency is not None and urgency not in {'now', 'week', 'later'}:
            errors.append(f'C1 call {index}: Urgency must be now, week, or later')
    return errors


def process_hits(md):
    """Process vocabulary and citation codes in reader text. Link targets are ignored and link text is kept."""
    text = re.sub(r'(?<!\[)\[([^\[\]]*)\]\([^)]*\)', r'\1', md)
    text = re.sub(r'<https?://[^>]*>|https?://\S+', ' ', text)
    hits = {m.group(0) for b in PROCESS_TERMS for m in re.finditer(b, text, re.I)}
    hits |= {m.group(0) for m in CITATION_CODE.finditer(text)}
    return sorted(hits)


PROSE_CHECK = pathlib.Path(__file__).resolve().with_name('prose_check.py')


STYLE_REFERENCE = pathlib.Path(__file__).resolve().parent.parent / 'references' / 'reader-pair-style.md'
READER_STYLE = STYLE_REFERENCE.read_text(encoding='utf-8')
PROSE_RULES = READER_STYLE.split('## Mechanical prose checks\n', 1)[1].split('\n## ', 1)[0]
SECTIONS = re.findall(r'^- `([^`]+)`', READER_STYLE.split('## Factual brief\n', 1)[1].split('\n## ', 1)[0], re.M)
ASSESSMENT_SECTIONS = re.findall(r'^- `([^`]+)`', READER_STYLE.split('## Technical assessment brief\n', 1)[1].split('\n## ', 1)[0], re.M)


def _purpose_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('report_purpose', pathlib.Path(__file__).with_name('report_purpose.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def style_for_purpose(purpose):
    if purpose == 'legacy':
        return READER_STYLE + '\nPreserve the historical brief sections: ' + ', '.join(LEGACY_SECTIONS)
    _purpose_module().validate_report_purpose(purpose)
    common = READER_STYLE.split('## Factual brief', 1)[0]
    factual = READER_STYLE.split('## Factual brief\n', 1)[1].split('\n## ', 1)[0]
    assessment = READER_STYLE.split('## Technical assessment brief\n', 1)[1].split('\n## ', 1)[0]
    extra_rules = (
        '\nNever put Markdown tables in the plain brief (exhibits and tables belong only in the reference pack; '
        'write connected plain prose or short flat bullets instead). Never link to local .md filenames such as '
        '(reference-pack.md); the footer links the reference pack automatically.\n'
    )
    return common + '\n' + factual + ('\n' + assessment if purpose == 'decision_assessment' else '') + extra_rules


STYLE = style_for_purpose('factual')
EDITOR_SYS = READER_STYLE + '''
You are the independent editor of the plain brief (the BRIEF section at the end of the prompt).
The reference pack and saved lane files in PACK AND SAVED EVIDENCE are read-only source material already reviewed in an earlier phase.
Judge ONLY the plain brief (BRIEF:) against the reference pack and saved evidence; never block on sentences, tables or punctuation inside the read-only reference pack.
Check important quantities and qualifications in the plain brief semantically. Number counts are not retention proof.
Return only raw JSON with blocking, minor, verdict, checks and important_quantity_qualification_review.
checks must contain factual, reasoning, editorial and presentation objects, each with verdict PASS/FAIL and a non-empty basis.
important_quantity_qualification_review must contain verdict PASS/FAIL and a non-empty basis explaining the evidence comparison.
Put any changed subject, unit, period, date, definition, scope, certainty, attribution or unsupported claim in the plain brief in blocking, never minor.
Use PASS only when every dimension of the plain brief passes and blocking is empty. Do not invent criticism of clear supported prose.
minor is optional style only. Preserve enough why and how. Apply the declared report_purpose, not topic-based recommendations.
'''


def parse_editor_response(raw):
    """Reparse saved text locally; never dispatch a model to repair JSON parsing."""
    decoder = json.JSONDecoder()
    candidates = []
    for match in re.finditer(r'\{', str(raw)):
        try:
            value, _ = decoder.raw_decode(str(raw)[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and 'verdict' in value and 'blocking' in value:
            candidates.append(value)
    if len(candidates) != 1:
        raise ValueError('exactly one editor verdict object required')
    return candidates[0]


def editor_review_schema_errors(review, *, purpose, retained_findings=(), review_binding=None):
    """Mechanical validation only; never turn invalid JSON into factual criticism."""
    if not isinstance(review, dict):
        return ['editor review must be an object']
    errors = []
    if review.get('verdict') not in ('PASS', 'FAIL'):
        errors.append('verdict must be PASS or FAIL')
    for key in ('blocking', 'minor'):
        value = review.get(key, [] if key == 'minor' else None)
        if not isinstance(value, list) or any(not isinstance(note, str) or not note.strip() for note in value):
            errors.append(f'{key} must be an array of non-empty strings')
    if purpose != 'legacy':
        checks = review.get('checks')
        parts = [(f'checks.{key}', checks.get(key) if isinstance(checks, dict) else None)
                 for key in ('factual', 'reasoning', 'editorial', 'presentation')]
        parts.append(('important_quantity_qualification_review', review.get('important_quantity_qualification_review')))
        for name, part in parts:
            if (not isinstance(part, dict) or part.get('verdict') not in ('PASS', 'FAIL')
                    or not isinstance(part.get('basis'), str) or not part['basis'].strip()):
                errors.append(f'{name} requires PASS/FAIL and a non-empty string basis')
    expected = set(retained_findings)
    rows = review.get('note_dispositions')
    if expected and isinstance(rows, list) and rows:
        for row in rows:
            if (set(row) != set(JUDGEMENT_FIELDS) or row.get('classification') not in
                    ('apply_correction', 'already_resolved', 'mistaken_reviewer', 'optional_style')
                    or any(row.get(k) not in ('supported', 'partial', 'unsupported')
                           for k in ('classification_verdict', 'evidence_verdict'))
                    or not isinstance(row.get('correction_applied'), bool)
                    or row.get('factual_impact') not in ('none', 'resolved', 'reviewer_error', 'unresolved')
                    or not isinstance(row.get('rationale'), str) or not row['rationale'].strip()
                    or not isinstance(row.get('evidence_bindings'), list) or not row['evidence_bindings']):
                errors.append('each note disposition requires typed classification and evidence judgements')
    return errors


def editor_review_passes(review, *, purpose, retained_findings=(), review_binding=None):
    if editor_review_schema_errors(review, purpose=purpose, retained_findings=retained_findings,
                                   review_binding=review_binding):
        return False
    if not isinstance(review, dict) or review.get('verdict') != 'PASS' or review.get('blocking') != []:
        return False
    if purpose == 'legacy':
        return True
    checks = review.get('checks')
    if not isinstance(checks, dict):
        return False
    parts = [checks.get(key) for key in ('factual', 'reasoning', 'editorial', 'presentation')]
    parts.append(review.get('important_quantity_qualification_review'))
    return all(isinstance(part, dict) and part.get('verdict') == 'PASS'
               and isinstance(part.get('basis'), str) and part['basis'].strip() for part in parts)


def prose_result(md):
    """the owner's plain-prose checker on reader text: {'ok', 'fails', 'long_sentences'}."""
    import contextlib
    import importlib.util
    import io
    if not PROSE_CHECK.is_file():
        return {'ok': False, 'fails': [f'plain-prose checker not found at {PROSE_CHECK}'], 'long_sentences': []}
    spec = importlib.util.spec_from_file_location('prose_check', PROSE_CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The C1 decision block is a fixed form whose fields are indented under each numbered
    # call. Count its fields as flat bullets so the checker's nested-bullet rule does not fight the
    # form; every sentence in it is still checked.
    md = re.sub(r'(?m)^[ \t]{2,}([-*+]\s+(?:Why it matters|If you do nothing|Suggested|Urgency|Sources):)', r'\1', md)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = bool(mod.check('brief', md))
    out = buf.getvalue().splitlines()
    fail_line = next((l for l in out if l.startswith('FAIL: ')), '')
    return {'ok': ok, 'fails': [f.strip() for f in fail_line[6:].split(';') if f.strip()],
            'long_sentences': [l.strip() for l in out if l.strip().startswith('[') and 'w] ' in l][:8]}


def lint(md, prose=False, purpose='factual'):
    body = re.sub(r'\]\([^)]*\)', ']', md)
    words = len(re.findall(r"[A-Za-z0-9'’-]+", body))
    sents = [x for x in re.split(r'(?<=[.!?])\s+', re.sub(r'^[#|>\-*].*$', '', body, flags=re.M)) if len(x.split()) > 3]
    avg = round(sum(len(x.split()) for x in sents) / max(1, len(sents)), 1)
    hits = sorted({m.group(0) for b in BANNED for m in re.finditer(b, body, re.I)})
    proc = process_hits(md)
    required_sections = LEGACY_SECTIONS if purpose == 'legacy' else SECTIONS + (ASSESSMENT_SECTIONS if purpose == 'decision_assessment' else [])
    if purpose != 'legacy':
        _purpose_module().validate_report_purpose(purpose)
    missing = [s for s in required_sections if s not in md]
    table_pipe = bool(re.search(r'^\|.*\[\[[^\]]*\|', md, re.M))
    table_in_brief = bool(prose and re.search(r'^\|.*\|', md, re.M))
    local_md_link = bool(prose and re.search(r'\]\((?!https?://)[^)#\s]+\.md(?:#[^)]*)?\)', md))
    decision_call_errors = validate_c1_call_block(md, require_section=purpose == 'legacy')
    ok = (words <= MAX_WORDS and avg <= 22 and not hits and not proc and not missing
          and not table_pipe and not table_in_brief and not local_md_link
          and not decision_call_errors and md.lstrip().startswith('# '))
    result = {'ok': ok, 'words': words, 'max_words': MAX_WORDS, 'avg_sentence_words': avg,
              'banned_hits': hits, 'process_hits': proc, 'missing_sections': missing,
              'table_alias_pipe': table_pipe, 'table_in_brief': table_in_brief,
              'local_md_link': local_md_link, 'decision_call_errors': decision_call_errors}
    if prose:  # layout v2: the brief must also pass the plain-prose checker
        result['prose'] = prose_result(md)
        result['ok'] = ok and result['prose']['ok']
    return result


def brief_body(txt):
    """Reader text of a plain brief file: drop YAML frontmatter and the 'Detail and sources' footer."""
    m = re.match(r'^---\n.*?\n---\n', txt, re.S)
    body = txt[m.end():] if m else txt
    return body.split(FOOTER_MARK)[0].strip()


def check_file(path, prose=False, purpose='factual'):
    return lint(brief_body(pathlib.Path(path).read_text(encoding='utf-8', errors='ignore')), prose=prose, purpose=purpose)


def read_sources(p):
    txt = ''
    for s in p['sources']:
        f = pathlib.Path(s) if s.startswith('/') else EXPORT_ROOT / s
        if f.exists():
            txt += f'\n\n===== {f.name} =====\n' + f.read_text(encoding='utf-8')
    return txt


def rewrite(key, p, out, *, corrected_draft=None):
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if _RUN.get() is None:
        with run_context(load_settings(), out / (p['slug'] + '-raw-' + uuid.uuid4().hex)):
            return rewrite(key, p, out, corrected_draft=corrected_draft)
    pack = read_sources(p)
    src = f"WHAT THE OWNER ASKED FOR:\n{p.get('intent','not recorded')}\n\nRESEARCH PACK (source of facts; do not copy its style):\n{pack}"
    path = out / f"{p['slug']}.md"
    prose = bool(p.get('prose'))
    purpose = p.get('report_purpose', 'factual')
    style = style_for_purpose(purpose)
    src = f'Report purpose: {purpose}\n' + src
    body_parity = None
    corrected_path = None
    if corrected_draft is not None:
        # A corrected draft is checked, not rewritten: the editor reads it exactly as written.
        draft_path = pathlib.Path(corrected_draft)
        corrected_path = str(draft_path)
        md = brief_body(draft_path.read_text(encoding='utf-8'))
        body_parity = True
        state = _RUN.get()
        if not state.writers:
            state.writers.extend(copy.deepcopy(p.get('artifact_writer_calls') or [
                {'role': 'corrected_draft', 'status': 'unknown_author'}]))
    else:
        md = call(WRITER, style, f"{src}\n\nWrite the brief. Working title: {p['title']}.", role='writer')
    log = []
    retained = list(p.get('retained_findings') or [])
    for rnd in range(3):
        L = lint(md, prose=prose, purpose=purpose)
        binding = {**(p.get('review_binding') or {}),
                   'reader_body_sha256': hashlib.sha256(str(md).strip().encode()).hexdigest()}
        review_user = (f'Report purpose: {purpose}\nREVIEW_BINDING: {json.dumps(binding)}\n'
                       f'RETAINED_FINDINGS: {json.dumps(retained)}\nPACK AND SAVED EVIDENCE:\n{pack}\n\nBRIEF:\n{md}')
        raw = call(EDITOR, EDITOR_SYS, review_user, role='editor', review_binding=binding)
        # Preserve the actual response before local parsing. A parser/schema failure
        # is not permission to buy another call or rewrite otherwise valid prose.
        (out / f"{p['slug']}.editor-r{rnd}.raw.txt").write_text(raw, encoding='utf-8')
        parse_failed = False
        try:
            rv = parse_editor_response(raw)
        except ValueError:
            parse_failed = True
            rv = {}
        schema_errors = editor_review_schema_errors(rv, purpose=purpose,
            retained_findings=retained, review_binding=binding)
        eligible = getattr(raw, 'acceptance_eligible', False) is True
        accepted = not parse_failed and not schema_errors and eligible and editor_review_passes(
            rv, purpose=purpose, retained_findings=retained, review_binding=binding)
        log.append({'round': rnd, 'lint': L, 'editor': rv,
                    'parse_failed': parse_failed, 'schema_valid': not schema_errors,
                    'schema_errors': schema_errors, 'acceptance_eligible': eligible})
        for note_key in ('blocking', 'minor'):
            values = rv.get(note_key, [])
            if isinstance(values, list):
                retained.extend(note for note in values if isinstance(note, str))
        retained = list(dict.fromkeys(retained))
        (out / f"{p['slug']}.r{rnd}.md").write_text(md)
        # Invalid review data is not permission to buy a repair or invent criticism.
        if (L['ok'] and accepted) or rnd == 2 or corrected_draft is not None or parse_failed or schema_errors or not eligible:
            break
        for _ in range(2):
            cur_lint = lint(md, prose=prose, purpose=purpose)
            issues = json.dumps({'automatic_check': cur_lint, 'blocking': rv.get('blocking', []), 'raw_editor_notes': rv.get('raw', '')}, indent=1)
            new = call(WRITER, style, role='writer', user=f'{src}\n\nCurrent brief:\n\n{md}\n\nFix each blocking problem and any automatic_check failure below with the smallest change, keep everything else, return the full brief only.\n{issues}')
            new_lint = lint(new, prose=prose, purpose=purpose)
            if not new_lint['missing_sections']:
                md = new
                if new_lint['ok']:
                    break
                rv = {'blocking': []}
    path.write_text(md)
    final = {'lint': lint(md, prose=prose, purpose=purpose), 'editor': 'PASS' if accepted else 'FAIL'}
    review_doc = {'key': key, 'rounds': log, 'final': final}
    if corrected_draft is not None:
        review_doc.update({'corrected_draft_path': corrected_path, 'body_parity': body_parity})
        final.update({'corrected_draft_path': corrected_path, 'body_parity': body_parity})
    (out / f"{p['slug']}.review.json").write_text(json.dumps(review_doc, indent=2))
    print(f"{key}: lint={final['lint']['ok']} editor={final['editor']} words={final['lint']['words']}", flush=True)
    return key, final


FM = '''---
title: {title}
created: {date}
type: source
stage: import
source: plain-brief rewrite of an existing research pack
source_type: conversation
author: Research contributors
credibility: practitioner
captured_via: agent-deep-research
tags: [source/adapted]
related:
{related}
verified_count: 0
---

'''


def bind_legacy_publish(pack, out):
    """A loose pack cannot authorise publication; bind exact original inputs and reader."""
    if not isinstance(pack.get('campaign_root'), str) or not pack['campaign_root'].strip():
        raise ValueError('legacy publication requires the original campaign_root')
    root = pathlib.Path(pack['campaign_root']).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise ValueError('campaign_root must be an absolute original directory')
    root = root.resolve(strict=True)
    manifest = json.loads(_confined(root, 'manifest.json').read_text())
    purpose = _purpose_module().resolve_report_purpose(manifest, root)
    if pack.get('report_purpose') != purpose:
        raise ValueError('legacy pack purpose differs from original campaign')
    brief_rel, review_rel, full_rel, exec_rel = campaign_plain_brief_paths(manifest)
    expected_sources = {_confined(root, full_rel)}
    if exec_rel and _confined(root, exec_rel).is_file():
        expected_sources.add(_confined(root, exec_rel))
    if (root / 'citations.json').is_file():
        expected_sources.add(_confined(root, 'citations.json'))
    sources = pack.get('sources')
    if not isinstance(sources, list) or any(not isinstance(s, str) or not pathlib.Path(s).is_absolute() for s in sources):
        raise ValueError('legacy publication requires exact original absolute sources')
    actual = set()
    for source in sources:
        relative = pathlib.Path(source).relative_to(root).as_posix()
        checked = _confined(root, relative)
        if str(checked) != source or not checked.is_file():
            raise ValueError('legacy publication source is not an original campaign input')
        actual.add(checked)
    if actual != expected_sources:
        raise ValueError('legacy publication sources do not bind the complete original pack')
    destination = (manifest.get('deliverable_exports') or {}).get('plain_brief_intended_export_path')
    if not destination or pack.get('intended_export_path') != destination:
        raise ValueError('legacy publication destination differs from original campaign')
    readiness = campaign_readiness(root, require_brief=True, review_rel=review_rel)
    if readiness.get('status') != 'PASS' or readiness.get('publication_qa_allowed') is not True:
        raise ValueError('campaign readiness: ' + '; '.join(readiness.get('findings') or ['not accepted']))
    slug = pack.get('slug')
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
        raise ValueError('unsafe legacy output slug')
    candidate = pathlib.Path(out) / (slug + '.md')
    if candidate.is_symlink() or not candidate.is_file() or candidate.read_bytes() != _confined(root, brief_rel).read_bytes():
        raise ValueError('legacy publication reader differs from accepted original bytes')
    return root


def publish(manifest, ok, out):
    """Guard the deprecated entry point; use the existing verified exporter, never git."""
    roots = [bind_legacy_publish(manifest[key], out) for key in ok]
    import sys
    exports = []
    for root in dict.fromkeys(roots):
        # The exporter repeats readiness, provider/privacy/quote checks and target readback.
        result = subprocess.run([sys.executable, '-B', str(pathlib.Path(__file__).with_name('export_deliverables.py')),
                                 '--campaign-root', str(root)], capture_output=True, text=True, check=True)
        exports.append({'campaign_root': str(root), 'stdout': result.stdout})
    return {'exports': exports, 'git_publication': False}


def sha256_file(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def _confined(root, rel):
    rel = str(rel or '').strip().replace('\\', '/')
    p = pathlib.PurePosixPath(rel)
    if not rel or p.is_absolute() or '..' in p.parts:
        raise ValueError(f'unsafe campaign path: {rel!r}')
    out, cur = root / rel, root
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f'symlink not allowed in campaign path: {rel}')
    out.resolve(strict=False).relative_to(root.resolve())
    return out


def verify_campaign_plain_brief(root, manifest):
    """Shared plain-brief gate for verify_campaign_depth.py and export_deliverables.py.

    Legacy campaigns without a ``plain_brief`` manifest block are not checked. Lint problems in the
    reader text (process vocabulary, citation codes, over the cap, missing sections, bad C1 calls) are
    failures; a missing, stale or non-independent review record is a hold.
    """
    root = pathlib.Path(root)
    readiness = campaign_readiness(root, require_brief=True,
        review_rel=campaign_plain_brief_paths(manifest)[1])
    readiness_holds = [] if (readiness.get('status') == 'PASS' and readiness.get('publication_qa_allowed') is True) else [
        'campaign_readiness ' + str(x) for x in (readiness.get('findings') or [readiness.get('status')])]
    try:
        purpose = _purpose_module().resolve_report_purpose(manifest, root)
    except ValueError as exc:
        return {'declared': isinstance(manifest.get('plain_brief'), dict), 'required': True, 'metrics': {}, 'failures': [str(exc)], 'holds': []}
    pb = manifest.get('plain_brief')
    out = {'declared': isinstance(pb, dict), 'required': False, 'metrics': {'plain_brief_status': 'not_declared'}, 'failures': [], 'holds': readiness_holds}
    if not isinstance(pb, dict):
        try:
            out['holds'].extend(reference_pack_review_holds(root, manifest))
        except (ValueError, TypeError) as exc:
            out['holds'].append(f'reference_pack_review_invalid {exc}')
        return out
    m, failures, holds = out['metrics'], out['failures'], out['holds']
    try:
        holds.extend(reference_pack_review_holds(root, manifest))
    except (ValueError, TypeError) as exc:
        holds.append(f'reference_pack_review_invalid {exc}')
    if pb.get('required', True) is False:
        m['plain_brief_status'] = 'not_required'
        if not str(pb.get('not_required_reason') or '').strip():
            holds.append('plain_brief_not_required_without_reason')
        return out
    out['required'] = True
    brief_rel, review_rel, full_rel, _ = campaign_plain_brief_paths(manifest)
    try:
        brief, review, full = _confined(root, brief_rel), _confined(root, review_rel), _confined(root, full_rel)
    except ValueError as exc:
        holds.append(f'plain_brief_path_invalid {exc}')
        return out
    if not brief.is_file():
        m['plain_brief_status'] = 'missing'
        holds.append(f'plain_brief_missing {brief_rel}')
        return out
    v2 = is_v2(manifest)
    res = check_file(brief, prose=v2, purpose=purpose)
    m.update({'plain_brief_status': 'present', 'plain_brief_words': res['words'], 'plain_brief_max_words': MAX_WORDS,
              'plain_brief_lint_ok': res['ok'], 'plain_brief_avg_sentence_words': res['avg_sentence_words']})
    vocab = sorted(set(res['banned_hits']) | set(res['process_hits']))
    if vocab:
        failures.append('plain_brief_process_vocabulary ' + ', '.join(vocab))
    if res['words'] > MAX_WORDS:
        failures.append(f"plain_brief_words {res['words']} > {MAX_WORDS}")
    other = ([f"missing sections {', '.join(res['missing_sections'])}"] if res['missing_sections'] else []) + \
            ([f"avg sentence {res['avg_sentence_words']} > 22"] if res['avg_sentence_words'] > 22 else []) + \
            (['wikilink alias pipe in table'] if res['table_alias_pipe'] else []) + res['decision_call_errors']
    if other:
        failures.append('plain_brief_lint_failed ' + '; '.join(other))
    if v2 and not res['prose']['ok']:
        m['plain_brief_prose_ok'] = False
        failures.append('plain_brief_prose_check_failed ' + '; '.join(res['prose']['fails']))
    elif v2:
        m['plain_brief_prose_ok'] = True
    if not other and not res['ok'] and not vocab and res['words'] <= MAX_WORDS and (not v2 or res['prose']['ok']):
        failures.append('plain_brief_lint_failed title must be a single # heading')
    if not review.is_file():
        holds.append(f'plain_brief_review_missing {review_rel}')
        return out
    try:
        rec = json.loads(review.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        holds.append(f'plain_brief_review_invalid {type(exc).__name__}')
        return out
    integrity = _receipt_integrity().static_check(rec, require_model_ids=False)
    if integrity['verdict'] == 'FAIL':
        m['plain_brief_review_integrity'] = 'receipt_fabricated'
        failures.append('receipt_fabricated plain brief review: ' + '; '.join(integrity['reasons']))
        return out
    if not isinstance(rec, dict) or rec.get('schema_version') != REVIEW_SCHEMA:
        holds.append('plain_brief_review_schema_invalid')
        return out
    m['plain_brief_editor_verdict'] = rec.get('editor_verdict')
    if purpose != 'legacy' and not editor_review_passes({'verdict': rec.get('editor_verdict'), 'blocking': rec.get('editor_blocking'), 'checks': rec.get('checks'), 'important_quantity_qualification_review': rec.get('important_quantity_qualification_review')}, purpose=purpose):
        holds.append('plain_brief_semantic_review_incomplete')
    if str(rec.get('status')).upper() != 'PASS' or str(rec.get('editor_verdict')).upper() != 'PASS':
        failures.append(f"plain_brief_editor_review_not_pass {rec.get('status')}/{rec.get('editor_verdict')}")
    writer, editor = str(rec.get('writer_model') or '').strip(), str(rec.get('editor_model') or '').strip()
    independent = bool(writer and editor and writer.split('/')[0] != editor.split('/')[0])
    provenance = rec.get('provenance')
    if not isinstance(provenance, dict):
        provenance = {}
    if (purpose != 'legacy' or 'artifact_writers' in rec or 'attempts' in provenance
            or provenance.get('schema_version') == _receipt_integrity().SERVED_CALLS_SCHEMA):
        settings = load_settings(environ={})
        authors, attempts = rec.get('artifact_writers'), provenance.get('attempts', [])
        authors_match = _receipt_integrity()._author_attempts_match(authors, attempts)
        developers, known = model_client.writer_provenance(settings, authors)
        editor_developer = settings.developer(editor)
        reviewer_calls = [c for c in attempts if isinstance(c, dict)
                          and c.get('role') == 'brief_reviewer' and c.get('status') == 'success'] if isinstance(attempts, list) else []
        independent = (authors_match and known and bool(editor_developer) and editor_developer not in developers
                       and rec.get('acceptance_eligible') is True and bool(reviewer_calls)
                       and reviewer_calls[-1].get('served_model') == editor
                       and reviewer_calls[-1].get('identity_verified') is True)
        independent = independent and not served_review_identity_errors(rec)
    if not independent:
        holds.append('plain_brief_review_not_independent')
    if rec.get('brief_sha256') != sha256_file(brief):
        holds.append('plain_brief_review_stale brief changed after review')
    if not full.is_file() or rec.get('full_pack_sha256') != sha256_file(full):
        holds.append('plain_brief_stale_full_pack full pack changed after the brief was written')
    return out


def _receipt_integrity():
    import importlib.util
    spec = importlib.util.spec_from_file_location('receipt_integrity', pathlib.Path(__file__).resolve().parent / 'receipt_integrity.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OUTPUT_LAYOUT_V2 = 'brief-and-reference-v2'
REFERENCE_PACK_REL = 'internal/reference-pack.md'


def is_v2(manifest):
    """layout v2 has two outputs, the plain brief and the reference pack. Anything else is legacy."""
    return isinstance(manifest, dict) and manifest.get('output_layout') == OUTPUT_LAYOUT_V2


def campaign_plain_brief_paths(manifest):
    """(brief_rel, review_rel, full_rel, exec_rel) for a campaign manifest.

    In layout v2 ``full_rel`` is the reference pack and ``exec_rel`` is None (there is no executive brief).
    """
    pb = manifest.get('plain_brief') if isinstance(manifest.get('plain_brief'), dict) else {}
    internals = manifest.get('internal_outputs') or []
    rels = [(i.get('path') if isinstance(i, dict) else i) for i in internals]
    if is_v2(manifest):
        return pb.get('path') or DEFAULT_BRIEF_REL, pb.get('review') or DEFAULT_REVIEW_REL, (rels[0] if rels else REFERENCE_PACK_REL), None
    full_rel = rels[0] if rels else 'internal/full-research-pack.md'
    exec_rel = rels[1] if len(rels) > 1 else 'internal/executive-brief.md'
    return pb.get('path') or DEFAULT_BRIEF_REL, pb.get('review') or DEFAULT_REVIEW_REL, full_rel, exec_rel


def reset_editor_note_decisions(root, *, brief_sha256, review_rel=DEFAULT_REVIEW_REL, previous_decisions=None,
                                apply_served_judgements=False):
    """Use the shared both-reader/evidence/history pending builder, never carry approval."""
    root = pathlib.Path(root)
    review = json.loads(_confined(root, review_rel).read_text())
    if review.get('brief_sha256') != brief_sha256:
        raise ValueError('pending brief hash differs from current review')
    pending = build_pending_editor_decisions(root, review=review, previous_decisions=previous_decisions,
                                             review_rel=review_rel)
    if apply_served_judgements:
        checks = {}
        for rel in review.get('classification_reviews', []):
            check = json.loads(_confined(root, rel).read_text())
            checks[check['note']] = (rel, check)
        if set(checks) == set(pending['retained_findings']):
            for note in pending['retained_findings']:
                rel, check = checks[note]
                refs = [row['path'] for row in check['evidence_bindings']] + [rel]
                pending['decisions'].append({'note': note, 'classification': check['classification'],
                    'evidence_verdict': check['evidence_verdict'], 'classification_review': rel,
                    'evidence_refs': refs, 'evidence_bindings': [
                        {'path': ref, 'sha256': sha256_file(_confined(root, ref))} for ref in refs]})
            pending['status'] = 'reviewed'
        elif review.get('status') == 'PASS' and review.get('editor_verdict') == 'PASS' and not review.get('editor_blocking'):
            pending['retained_findings'] = []
            pending['status'] = 'reviewed'
    path = confined_output_path(root, DEFAULT_EDITOR_DECISIONS_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pending, indent=2) + '\n', encoding='utf-8')
    return path


def archive_review_history(root, manifest):
    """Exclusively preserve the previous review, decisions, readers and named evidence."""
    brief_rel, review_rel, full_rel, _ = campaign_plain_brief_paths(manifest)
    review = _confined(root, review_rel)
    decisions = _confined(root, DEFAULT_EDITOR_DECISIONS_REL)
    previous = json.loads(decisions.read_text()) if decisions.exists() else {}
    if not review.exists() and not decisions.exists():
        return previous
    old_review = json.loads(review.read_text()) if review.exists() else {}
    paths = {brief_rel, review_rel, full_rel, DEFAULT_EDITOR_DECISIONS_REL, 'manifest.json'}
    paths.update(old_review.get('evidence_inputs') or [])
    for item in previous.get('decisions', []):
        paths.update(item.get('evidence_refs') or [])
    validated = []
    for rel in sorted(paths):
        source = _confined(root, rel)
        if source.exists():
            if not source.is_file():
                raise ValueError(f'archive input is not a file: {rel}')
            validated.append((rel, source.read_bytes()))
    history = confined_output_path(root, 'receipts/qa-history/' + uuid.uuid4().hex)
    history.mkdir(parents=True, exist_ok=False)
    for rel, data in validated:
        # Keep review at the discoverable historical name; other inputs retain their paths.
        dest = history / ('plain-brief-review.json' if rel == review_rel else rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open('xb') as fh:
            fh.write(data)
    return previous


CAMPAIGN_FM = '''---
title: {title}
created: {date}
type: source
stage: import
source: {source_url}
# plain brief of the {pack_label} ({full_rel})
author: Research contributors
credibility: practitioner
captured_via: agent-deep-research
tags: [source/adapted]
related:
{related}
verified_count: 0
---

'''


def extract_frontmatter_source_url(reference_text: str, fallback_url: str = 'https://hermes-agent.nousresearch.com/docs') -> str:
    """URL for the brief's ``source:`` field: the pack's frontmatter source, else its first URL, else the fallback."""
    reference_text = reference_text or ''
    m = re.match(r'^---\n(.*?)\n---\n', reference_text, re.S)
    if m:
        line = re.search(r'^source:\s*(.*)$', m.group(1), re.M)
        url = re.search(r'https?://[^\s\'\"<>)\]]+', line.group(1)) if line else None
        if url:
            return url.group(0)
    url = re.search(r'https?://[^\s\'\"<>)\]]+', reference_text)
    return url.group(0) if url else fallback_url


def render_campaign_frontmatter(*, title: str, date: str, related: str, reference_text: str = '',
                                source_url: str | None = None, full_rel: str = '', pack_label: str = 'reference pack') -> str:
    resolved = str(source_url).strip() if source_url and str(source_url).strip() else ''
    if not re.match(r'https?://\S+$', resolved):
        if pack_label == 'internal full research pack':
            resolved = f'plain brief of the {pack_label} ({full_rel})'
        else:
            resolved = extract_frontmatter_source_url(reference_text)
    return CAMPAIGN_FM.format(title=title, date=date, source_url=resolved, related=related, full_rel=full_rel, pack_label=pack_label)


def normalise_body_for_parity(text: str) -> str:
    """Reader body with frontmatter and the detail footer removed and whitespace collapsed."""
    return ' '.join(brief_body(text or '').split())


def reference_pack_review_holds(root, manifest):
    """Require existing full-pack QA before brief generation; no additional model stage."""
    root = pathlib.Path(root)
    purpose = _purpose_module().resolve_report_purpose(manifest, root)
    if purpose == 'legacy':
        return []
    _, _, full_rel, _ = campaign_plain_brief_paths(manifest)
    full = _confined(root, full_rel)
    receipts = (manifest.get('qa') or {}).get('receipts') or {}
    rel = receipts.get('evidence_privacy') or 'qa/evidence-privacy-review.json'
    review = _confined(root, rel)
    if not full.is_file() or not review.is_file():
        return ['reference_pack_review_missing']
    try:
        record = json.loads(review.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        return ['reference_pack_review_invalid']
    if not isinstance(record, dict):
        return ['reference_pack_review_invalid']
    bind_key = 'reference_pack_sha256' if is_v2(manifest) else 'internal_full_pack_sha256'
    problems = []
    if record.get('status') != 'PASS' or record.get(bind_key) != sha256_file(full):
        problems.append('reference_pack_review_not_pass_or_stale')
    if not editor_review_passes({'verdict': record.get('status'), 'blocking': record.get('blocking'),
                                 'checks': record.get('checks'),
                                 'important_quantity_qualification_review': record.get('important_quantity_qualification_review')}, purpose=purpose):
        problems.append('reference_pack_semantic_review_incomplete')
    return problems


def campaign_brief(root, *, corrected_draft=None, corrected_draft_authors=None):
    """Write the plain brief and its review record for a finished campaign. Returns the review record."""
    root = pathlib.Path(root).expanduser().resolve()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    try:
        purpose = _purpose_module().resolve_report_purpose(manifest, root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    preflight = campaign_readiness(root, require_brief=False)
    pack_holds = reference_pack_review_holds(root, manifest)
    if preflight.get('status') != 'PASS':
        pack_holds.extend(preflight.get('findings') or ['reference pack readiness failed'])
    if pack_holds:
        raise SystemExit('reference pack review not accepted: ' + '; '.join(pack_holds))
    brief_rel, review_rel, full_rel, exec_rel = campaign_plain_brief_paths(manifest)
    full, brief, review = _confined(root, full_rel), _confined(root, brief_rel), _confined(root, review_rel)
    if not full.is_file():
        raise SystemExit(f"{'reference pack' if is_v2(manifest) else 'internal full pack'} missing: {full_rel}")
    full_sha = sha256_file(full)
    target = str(manifest.get('target') or root.name)
    slug = re.sub(r'[^a-z0-9]+', '-', target.lower()).strip('-')[:60] or 'deep-research'
    # citations.json maps source codes to real URLs so the writer can link sources instead of quoting codes.
    v2 = is_v2(manifest)
    extra_sources = [_confined(root, exec_rel)] if exec_rel else []
    sources = [str(full)] + [str(x) for x in (*extra_sources, root / 'citations.json') if x.is_file()]
    if purpose != 'legacy':
        for lane in sorted((root / 'lanes').glob('*.json')):
            sources.append(str(_confined(root, str(lane.relative_to(root)))))
    p = {'title': target, 'slug': slug, 'sources': sources, 'prose': v2, 'report_purpose': purpose,
         'intent': manifest.get('decision_question') or f'Deep research on {target}'}
    settings = load_settings()
    author_inputs, supplied_authors = [], []
    if corrected_draft_authors is not None:
        if corrected_draft is None:
            raise ValueError('corrected draft author packet requires a corrected draft')
        draft = pathlib.Path(corrected_draft).expanduser().absolute()
        author_path = pathlib.Path(corrected_draft_authors).expanduser().absolute()
        draft_rel = draft.relative_to(root).as_posix()
        authors_rel = author_path.relative_to(root).as_posix()
        draft = _confined(root, draft_rel); author_path = _confined(root, authors_rel)
        authors = json.loads(author_path.read_text())
        supplied_authors = authors.get('writer_calls') if isinstance(authors, dict) else None
        if (not isinstance(authors, dict) or authors.get('draft_sha256') != sha256_file(draft)
                or not model_client.writer_provenance(settings, supplied_authors)[1]):
            raise ValueError('corrected draft author packet is stale or has unknown served authors')
        author_inputs = [draft_rel, authors_rel]
    snapshot = confined_output_path(root, 'orchestration/effective-settings.json')
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    settings.freeze(snapshot)
    input_rels = [str(pathlib.Path(x).relative_to(root)) for x in sources] + author_inputs
    evidence_review_rel = ((manifest.get('qa') or {}).get('receipts') or {}).get('evidence_privacy') or 'qa/evidence-privacy-review.json'
    if _confined(root, evidence_review_rel).is_file():
        input_rels.append(evidence_review_rel)
        source_inputs = json.loads(_confined(root, evidence_review_rel).read_text()).get('evidence_inputs', [])
        if not isinstance(source_inputs, list) or any(not isinstance(rel, str) for rel in source_inputs):
            raise ValueError('full-pack evidence_inputs must name exact source files')
        for rel in source_inputs:
            source = _confined(root, rel)
            if not source.is_file():
                raise ValueError(f'admitted evidence source is missing: {rel}')
            input_rels.append(rel)
    input_rels.append('orchestration/effective-settings.json')
    previous_decisions = archive_review_history(root, manifest)
    for decision in previous_decisions.get('decisions', []):
        check_rel = decision.get('classification_review')
        if isinstance(check_rel, str):
            previous_check = json.loads(_confined(root, check_rel).read_text())
            for row in previous_check.get('evidence_bindings', []):
                if not isinstance(row, dict) or not isinstance(row.get('path'), str):
                    raise ValueError('previous judgement has malformed evidence bindings')
                input_rels.append(row['path'])
    input_rels = list(dict.fromkeys(input_rels))
    input_packet = build_review_packet([*input_rels, 'manifest.json'], root=root)
    historical_notes, _ = _history_findings(root)
    p['retained_findings'] = list(dict.fromkeys([*historical_notes,
        *(previous_decisions.get('retained_findings') or []),
        *(d.get('note') for d in previous_decisions.get('decisions', []) if isinstance(d, dict))]))
    # The existing editor gets source bytes and bindings, not a hand-written PASS sidecar.
    evidence_sources = [rel for rel in dict.fromkeys(input_rels)
                        if rel != 'orchestration/effective-settings.json']
    p['sources'] = list(dict.fromkeys([*p['sources'], *(str(_confined(root, rel)) for rel in evidence_sources)]))
    p['review_binding'] = {'full_pack_sha256': full_sha, 'evidence_bindings': [
        {'path': rel, 'sha256': sha256_file(_confined(root, rel))} for rel in evidence_sources]}
    work = confined_output_path(root, 'qa/plain-brief-rounds/' + uuid.uuid4().hex)
    work.mkdir(parents=True, exist_ok=False)
    with run_context(settings, work / 'provider', writers=supplied_authors) as run:
        _, final = rewrite(slug, p, work, corrected_draft=corrected_draft)
    unchanged = verify_review_packet(input_packet, root=root)
    if unchanged.get('status') != 'PASS':
        raise ValueError('campaign inputs changed during brief generation/review: ' + str(unchanged))
    md = (work / f'{slug}.md').read_text(encoding='utf-8').strip()
    ve = manifest.get('deliverable_exports') if isinstance(manifest.get('deliverable_exports'), dict) else {}
    if v2:
        full_note = pathlib.Path(str(ve.get('reference_pack_intended_export_path') or 'reference-pack.md')).stem
    else:
        full_note = pathlib.Path(str(ve.get('full_pack_intended_export_path') or 'full-research-pack.md')).stem
        exec_note = pathlib.Path(str(ve.get('board_brief_intended_export_path') or 'executive-brief.md')).stem
    title = md.splitlines()[0].lstrip('# ').strip().replace(':', ' —') if md.startswith('# ') else target
    date = str(manifest.get('run_date') or manifest.get('generated_at') or datetime.now(timezone.utc).isoformat())[:10]
    if v2:
        related = '\n'.join(f'  - "[[{r}]]"' for r in [full_note])
        pack_label, footer = 'reference pack', f'Detail and sources: [[{full_note}|reference pack]].'
    else:
        related = '\n'.join(f'  - "[[{r}]]"' for r in [full_note, exec_note])
        pack_label, footer = 'internal full research pack', f'Detail and sources: [[{full_note}|full research pack]], [[{exec_note}|executive brief]].'
    # Optional cross-references to sibling notes (e.g. a companion pack's plain brief or a combined summary).
    # Each entry is a export note name or {"note": name, "label": text}; they go in frontmatter and the footer.
    extra = [(r, None) if isinstance(r, str) else (str(r.get('note')), r.get('label'))
             for r in ((manifest.get('plain_brief') or {}).get('related_notes') or []) if r]
    if extra:
        related += ''.join(f'\n  - "[[{n}]]"' for n, _ in extra)
        footer += '\nRelated: ' + ', '.join(f'[[{n}|{lab}]]' if lab else f'[[{n}]]' for n, lab in extra) + '.'
    if v2:
        md = re.sub(r'\[([^\]]+)\]\((?:internal/)?reference-pack\.md(?:#[^)]*)?\)',
                    rf'[[{full_note}|\1]]', md)
    brief.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = render_campaign_frontmatter(title=title, date=date, related=related, full_rel=full_rel, pack_label=pack_label,
                                              reference_text=full.read_text(encoding='utf-8', errors='ignore'),
                                              source_url=manifest.get('primary_source'))
    brief.write_text(frontmatter + md +
                     f'\n\n---\n{footer}\n', encoding='utf-8')
    if sha256_file(full) != full_sha:
        raise SystemExit(f"{'reference pack' if is_v2(manifest) else 'internal full pack'} changed while the plain brief was being written; rerun")
    rounds = json.loads((work / f'{slug}.review.json').read_text(encoding='utf-8'))
    lint_result = check_file(brief, prose=v2, purpose=purpose)
    editor = rounds['rounds'][-1]['editor'] if rounds.get('rounds') else {}
    if corrected_draft is not None:
        # Recompute parity from the written file, so the record states what is on disk.
        rounds['body_parity'] = final['body_parity'] = (
            normalise_body_for_parity(brief.read_text(encoding='utf-8'))
            == normalise_body_for_parity(pathlib.Path(corrected_draft).read_text(encoding='utf-8')))
    passed = lint_result['ok'] and final.get('editor') == 'PASS' and (corrected_draft is None or bool(final.get('body_parity')))
    # Raw malformed values remain in rounds; the note sidecar receives only
    # actual string notes, with schema rejection recorded separately.
    editor_notes = {key: [note for note in editor.get(key, []) if isinstance(note, str) and note.strip()]
                    if isinstance(editor.get(key, []), list) else [] for key in ('blocking', 'minor')}
    if rounds['rounds'][-1]['schema_valid'] and not rounds['rounds'][-1]['acceptance_eligible']:
        editor_notes['blocking'].append('Reviewer independence from actual authors is unverified')
    record = {
        'schema_version': REVIEW_SCHEMA,
        'status': 'PASS' if passed else 'FAIL',
        'brief_path': brief_rel,
        'brief_sha256': sha256_file(brief),
        'full_pack_path': full_rel,
        'full_pack_sha256': full_sha,
        'writer_model': next((c.get('served_model') for c in reversed(run.writers) if c.get('status') == 'success'), None),
        'editor_model': next((c.get('served_model') for c in reversed(run.calls) if c.get('role') == 'brief_reviewer' and c.get('status') == 'success'), None),
        'artifact_writers': run.writers,
        'acceptance_eligible': final.get('editor') == 'PASS',
        'evidence_inputs': list(dict.fromkeys(input_rels)),
        'lint': lint_result,
        'editor_verdict': final.get('editor'),
        'schema_valid': rounds['rounds'][-1]['schema_valid'],
        'schema_errors': rounds['rounds'][-1]['schema_errors'],
        'editor_blocking': editor_notes['blocking'],
        'editor_minor': editor_notes['minor'],
        'retained_findings': list(dict.fromkeys(
            ' '.join(note.split()) for rnd in rounds.get('rounds', [])
            for key in ('blocking', 'minor') for note in (
                rnd['editor'].get(key, []) if isinstance(rnd['editor'].get(key, []), list) else [])
            if isinstance(note, str) and note.strip())),
        'report_purpose': purpose,
        'checks': editor.get('checks'),
        'important_quantity_qualification_review': editor.get('important_quantity_qualification_review'),
        'classification_reviews': [],
        'rounds_path': str((work / f'{slug}.review.json').relative_to(root)),
        'cost_usd': (sum(c['cost_usd'] for c in run.calls) if run.calls and all(c.get('cost_usd') is not None for c in run.calls) else None),
        'generated_at': datetime.now(timezone.utc).isoformat(),
        # provider ids for every model call, so the completion check can confirm them.
        'provenance': {'schema_version': 'deep-research-served-calls-v1',
                       'run_id': f"pbr-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}",
                       'script_sha256': sha256_file(__file__),
                       'final_editor_generation_id': next((c.get('generation_id') for c in reversed(run.calls)
                           if c.get('role') == 'brief_reviewer' and c.get('status') == 'success'), None),
                       'calls': [copy.deepcopy(c) for c in [*supplied_authors, *run.calls] if c.get('generation_id')],
                       'attempts': run.calls,
                       'raw_response_paths': [str(pathlib.Path(x).relative_to(root)) for x in run.raw_files]},
    }
    record['evidence_inputs'] = list(dict.fromkeys([
        *record['evidence_inputs'], record['rounds_path'], *record['provenance']['raw_response_paths']]))
    # Exclusive immutable sidecars preserve exactly what the capable comments editor
    # returned. Missing/invalid judgements remain pending; there is no parse-repair call.
    if rounds['rounds'][-1]['schema_valid'] and record['provenance']['raw_response_paths']:
        for index, judgement in enumerate(editor.get('note_dispositions') or []):
            rel = f'qa/editor-judgements/{work.name}/{index:04d}.json'
            target_path = confined_output_path(root, rel)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            check = {**judgement, 'brief_sha256': record['brief_sha256'], 'full_pack_sha256': full_sha,
                     'reviewer': record['editor_model'],
                     'generation_id': record['provenance']['final_editor_generation_id'],
                     'raw_response_path': record['provenance']['raw_response_paths'][-1],
                     'review_binding': editor.get('review_binding')}
            with target_path.open('x', encoding='utf-8') as fh:
                fh.write(json.dumps(check, indent=2) + '\n')
            record['classification_reviews'].append(rel)
        record['evidence_inputs'].extend(record['classification_reviews'])
    if corrected_draft is not None:
        record['corrected_draft_path'] = rounds.get('corrected_draft_path')
        record['body_parity'] = rounds.get('body_parity')
        record['writer_skipped'] = 'corrected_draft'
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    pb = manifest.setdefault('plain_brief', {})
    pb.update({'path': brief_rel, 'review': review_rel, 'status': 'reviewed_pass' if passed else 'review_failed',
               'brief_sha256': record['brief_sha256'], 'full_pack_sha256': full_sha})
    pb.setdefault('required', True)
    # Historical purpose-less manifests stay frozen. Their current review binds
    # the generated reader bytes; do not migrate history just to refresh a brief.
    if purpose != 'legacy':
        (root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    reset_editor_note_decisions(root, brief_sha256=record['brief_sha256'], review_rel=review_rel,
                                previous_decisions=previous_decisions, apply_served_judgements=True)
    readiness = campaign_readiness(root, require_brief=True, review_rel=review_rel)
    return {**record, 'readiness': readiness, 'acceptance_status': 'PASS' if (
        readiness.get('status') == 'PASS' and readiness.get('publication_qa_allowed') is True and passed) else 'HOLD'}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest'); ap.add_argument('--check', help='lint one published plain brief; exit 1 if it fails'); ap.add_argument('--out', default=os.path.expanduser('~/.hermes/work/plain-briefs'))
    ap.add_argument('--campaign-root', help='write internal/plain-brief.md + qa/plain-brief-review.json for a finished campaign')
    ap.add_argument('--corrected-draft', help='corrected draft to preserve; unknown authors remain HOLD')
    ap.add_argument('--corrected-draft-authors', help='campaign-confined exact draft SHA-256 and actual writer_calls packet')
    ap.add_argument('--publish', action='store_true'); ap.add_argument('--only', nargs='*')
    a = ap.parse_args()
    if a.check:
        res = check_file(a.check); print(json.dumps(res)); raise SystemExit(0 if res['ok'] else 1)
    if a.campaign_root:
        rec = campaign_brief(a.campaign_root, corrected_draft=a.corrected_draft, corrected_draft_authors=a.corrected_draft_authors)
        summary = {k: rec[k] for k in ('status', 'brief_path', 'editor_verdict', 'cost_usd')} | {'lint_ok': rec['lint']['ok'], 'words': rec['lint']['words']}
        if rec['status'] != 'PASS' and a.corrected_draft:
            summary['editor_blocking'] = rec.get('editor_blocking', [])
        summary['acceptance_status'] = rec['acceptance_status']
        summary['readiness'] = rec['readiness']
        print(json.dumps(summary, indent=1))
        raise SystemExit(0 if rec['acceptance_status'] == 'PASS' else 1)
    if not a.manifest:
        ap.error('--manifest is required unless --check or --campaign-root is used')
    out = pathlib.Path(a.out)
    manifest = json.loads(pathlib.Path(a.manifest).read_text()); keys = a.only or list(manifest)
    if a.publish:
        # Validate every selected pack before mkdir, generation, export or git writes.
        for key in keys:
            bind_legacy_publish(manifest[key], out)
        print(json.dumps({'publish': publish(manifest, keys, out)}, indent=2))
        raise SystemExit(0)
    out.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    settings.freeze(out / 'effective-settings.json')
    with cf.ThreadPoolExecutor(settings.resolve('writer')['execution']['concurrency']) as ex:
        res = dict(ex.map(lambda k: rewrite(k, manifest[k], out), keys))
    ok = [k for k, v in res.items() if v['lint']['ok'] and v['editor'] == 'PASS']
    rec = {'results': res, 'passed': ok, 'cost_usd': COST[0]}
    (out / 'receipt.json').write_text(json.dumps(rec, indent=2))
    print(json.dumps({'passed': ok, 'failed': [k for k in res if k not in ok], 'cost_usd': rec['cost_usd'], 'publish': rec.get('publish')}, indent=1))
