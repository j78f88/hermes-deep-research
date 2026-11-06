#!/usr/bin/env python3
"""Independent native-pixel review for an Archify diagram (deep-research diagram gate).

Why this exists: `hermes chat --image` pre-describes images with the auxiliary vision
model, so the reviewer model never sees pixels. This script sends each PNG as a native
`image_url` part straight to the reviewer model on OpenRouter, three views per diagram
(full, top crop, bottom crop), and records provider response ids and image hashes.
The shared client uses the existing OpenRouter secret reference lazily. Secrets are never stored.

Usage:
  diagram_review_native.py --dir <diagram-dir> --name <stem> --writer-calls <calls.json>
      [--model <catalogued reviewer>] [--builder-model <route hint>] [--out-dir <dir>]
Expects <dir>/<stem>.png, <stem>-top.png, <stem>-bottom.png, <stem>.architecture.json
(or another Archify IR named <stem>.*.json) and brief.json.
Writes <out-dir>/<stem>-<view>.json per view and <out-dir>/<stem>-visual-review.json.
Exit 0 only when all three views approve with medium/high confidence and no issues.
"""

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code
import argparse, base64, glob, hashlib, json, re, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from research_settings import load_settings  # noqa: E402
from model_client import chat_completion, ModelCallError, writer_provenance  # noqa: E402
import receipt_integrity  # noqa: E402

RUBRIC = """You are the independent Diagram QA Agent. Review the rendered image, not the source. Reject if any rule fails:
R1 Arrowheads: every arrow ends at its own arrowhead. Two lines that merge, tee into each other, or share one arrowhead into a box are ambiguous.
R2 Legibility: connector labels, box sublabels, identity text and legend are readable at this view without zooming.
R3 Dead space: reject when empty space makes the whole diagram look stretched, forces long detours, leaves a container mostly empty, or leaves a big top or bottom margin. A single empty grid cell or one empty corner inside a container is acceptable; do not fail for it alone.
R4 Nothing covers a line: no box, label, key or legend sits on or hides a connector.
R5 Furniture: identity cards, keys and legends stay close to the content and do not dominate it. A condition written on a line beats a separate key box.
R6 Identity: the title is in the image itself, and ids or paths are not broken across lines.
R7 Direction: each arrow's direction is readable from the drawing alone; if a junction makes it unclear, that is a failure.
R8 Meaning on the image: any line-style or colour meaning the reader needs is shown in the image, not assumed.
R9 One idea: the diagram answers one question; boxes for things that do not exist yet are grouped, not drawn one by one.
R10 Fidelity: the arrows match the expected list exactly in source, target, label and line style.
Judge only the attached image. Return JSON only."""


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_ir(d: Path, name: str) -> dict:
    cands = [p for p in glob.glob(str(d / f'{name}.*.json')) if not re.search(r'\.(raw|validate|check)\.', p)]
    return json.loads(Path(sorted(cands)[0]).read_text())


def prompts(ir: dict, brief: dict, name: str) -> dict:
    lab = {c['id']: c['label'] for c in ir['components']}
    arrows = '; '.join(f"{lab.get(e['from'], e['from'])}->{lab.get(e['to'], e['to'])}"
                       f" ({e.get('label') or 'no label'}, {'dashed' if e.get('variant') == 'dashed' else 'solid'})"
                       for e in ir['connections'])
    qs = [q for q in brief.get('image_answerable_questions', []) if q['id'] not in ('identity', 'bottom-clearance')]
    qtext = ' '.join(f'"{q["id"]}": {q["question"]}' for q in qs)
    tail = ('Return JSON only: {"reviewer":"independent-diagram-qa","diagram":"%s","view":"%s","verdict":"approve|reject",'
            '"confidence":"low|medium|high","rules_failed":["R1",...],"answers":{...},"issues":[...],"required_fixes":[...]}')
    return {
        'full': f"{RUBRIC}\n\nView: full image of diagram {name}. Question it answers: {brief.get('question')}\n"
                f"Expected arrows: {arrows}.\nIn \"answers\" give: arrows_match (yes/no plus mismatches), overlap (yes/no), "
                f"clipped_or_small_text (yes/no), dead_space_ok (yes/no), and one key per question id: {qtext}\n" + tail % (name, 'full'),
        'top': f"{RUBRIC}\n\nView: TOP CROP of diagram {name}. The title is the bold text in the first identity box. Check title, REF, TAGS, "
               f"EXPORT_ROOT SOURCE and NOTE are visible, readable, unbroken and clear of nodes, connectors and borders. In \"answers\" give: "
               f"title, ref, tags, export_source, note (the text you read), identity_clear (yes/no).\n" + tail % (name, 'top'),
        'bottom': f"{RUBRIC}\n\nView: BOTTOM CROP of diagram {name}. Check the last row, connectors, region border, legend and footer are "
                  f"separate, readable and padded, with nothing clipped. In \"answers\" give: bottom_clear (yes/no), legend_readable (yes/no), "
                  f"clipped (yes/no).\n" + tail % (name, 'bottom'),
    }


def call(model, prompt, img, *, settings, writers):
    parts = [{'type': 'text', 'text': prompt},
             {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(img).decode()}}]
    return chat_completion(model, '', parts, role='visual_reviewer', settings=settings, writers=writers)


def verify_writer_calls(settings, writers, *, fetch=None):
    """Authenticate every declared generation before any visual-review purchase.

    Failed attempts with no response identity cannot contribute. Any declared
    generation, including a truncated attempt, joins the independence set.
    Provider data and exception text are never copied into the saved record.
    """
    result = {'verdict': 'HOLD', 'calls': [], 'reasons': ['actual writer served identity is unknown']}
    if not writer_provenance(settings, writers)[1]:
        return result
    pending = []
    for index, record in enumerate(writers):
        gid, model = record.get('generation_id'), record.get('served_model')
        if gid is None and model is None and record.get('status') != 'success':
            continue
        if (not isinstance(gid, str) or not receipt_integrity.GENERATION_ID.fullmatch(gid)
                or record.get('identity_verified') is not True or not settings.developer(model)):
            return {**result, 'verdict': 'FAIL', 'reasons': ['invalid declared writer identity']}
        pending.append((index, gid, model))
    fetch = fetch or receipt_integrity.fetch_generation
    identities = receipt_integrity.ModelIdentityVerifier()
    for index, gid, declared in pending:
        try:
            data = fetch(gid)
        except Exception:  # provider/auth failure is non-acceptance, without sensitive exception text
            return {**result, 'reasons': ['writer provider lookup failed']}
        served = data.get('model') if isinstance(data, dict) else None
        try:
            model_matches = identities.matches(declared, served)
        except receipt_integrity.CatalogueUnavailable:
            return {**result, 'reasons': ['fresh official model catalogue unavailable']}
        # Canonical slugs need not be role-policy keys. Only an authenticated
        # exact pair may inherit the already-known declared model's developer.
        developer = settings.developer(declared) if model_matches else settings.developer(served)
        result['calls'].append({'writer_index': index, 'generation_id': gid,
            'served_model': served if developer else None, 'developer': developer})
        if data is None or not developer or not model_matches:
            reason = ('writer generation does not exist at provider' if data is None else
                      'writer provider identity is unknown' if not developer else
                      'writer provider identity differs from declared served_model')
            return {**result, 'verdict': 'FAIL', 'reasons': [reason]}
    return {**result, 'verdict': 'PASS', 'reasons': []}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True); ap.add_argument('--name', required=True)
    ap.add_argument('--model', default=None); ap.add_argument('--builder-model', default='', help='requested route hint, not served-identity evidence')
    ap.add_argument('--writer-calls', help='JSON list (or object with calls[]) of actual writer/builder and rewriting-editor calls')
    ap.add_argument('--out-dir', default=None)
    ap.add_argument('--views', default='full,top,bottom', help='comma list; calibration runs may use full only')
    a = ap.parse_args()
    d = Path(a.dir); out = Path(a.out_dir or d / 'review'); out.mkdir(parents=True, exist_ok=True)
    writer_verification = {'verdict': 'HOLD', 'calls': [], 'reasons': ['writer inventory unavailable']}
    try:
        if not a.writer_calls:
            raise ValueError('actual writer calls are required; a requested builder pin is not provenance')
        writers = json.loads(Path(a.writer_calls).read_text())
        if isinstance(writers, dict):
            writers = writers.get('calls', [])
        overrides = {'roles': {'visual_reviewer': {'model': a.model, 'fallback_models': []}}} if a.model else None
        settings = load_settings(overrides=overrides)
        writer_verification = verify_writer_calls(settings, writers)
        if writer_verification['verdict'] != 'PASS':
            raise ValueError('; '.join(writer_verification['reasons']))
        developers = {row['developer'] for row in writer_verification['calls']}
        route = settings.resolve('visual_reviewer', writer_developers=developers)
        settings.freeze(out / 'effective-settings.json')
    except (ValueError, OSError) as exc:
        (out / f'{a.name}-visual-review.json').write_text(json.dumps({
            'schema_version': 'deep-research-diagram-native-review-v1', 'diagram': a.name,
            'verdict': 'reject', 'acceptance_eligible': False, 'writer_verification': writer_verification,
            'inspected_views': [], 'views': {}, 'cost_usd': 0}, indent=1))
        print(str(exc), file=sys.stderr); return 2
    ir = load_ir(d, a.name); brief = json.loads((d / 'brief.json').read_text()); P = prompts(ir, brief, a.name)
    imgs = {'full': d / f'{a.name}.png', 'top': d / f'{a.name}-top.png', 'bottom': d / f'{a.name}-bottom.png'}

    def run(view):
        img = imgs[view].read_bytes(); t0 = datetime.now(timezone.utc).isoformat()
        try:
            result = call(None, P[view], img, settings=settings, writers=writers)
        except ModelCallError as exc:
            (out / f'{a.name}-{view}-raw.json').write_text(json.dumps({'calls': exc.calls, 'raw_responses': exc.raw_responses}, indent=1))
            return view, {'parsed': {'verdict': 'reject', 'issues': ['model_call_failed']},
                          'acceptance_eligible': False, 'cost_usd': None, 'calls': exc.calls,
                          'served_model': None, 'provider_response_id': None,
                          'input_image_sha256': sha(img), 'prompt_sha256': sha(P[view].encode())}
        # A valid paid response survives any application JSON/schema failure.
        (out / f'{a.name}-{view}-raw.json').write_text(json.dumps({'calls': result.calls, 'raw_responses': result.raw_responses}, indent=1))
        resp, text = result.response, result.content
        try:
            match = re.search(r'\{.*\}', text, re.S)
            parsed = json.loads(match.group(0) if match else text)
            if not isinstance(parsed, dict):
                raise ValueError('review must be an object')
        except (ValueError, TypeError):
            parsed = {'verdict': 'reject', 'issues': ['review_response_parse_failed']}
        rec = {'diagram': a.name, 'view': view, 'route': 'openrouter chat/completions, native image_url part',
               'requested_model': route['model'], 'served_model': resp.get('model'), 'provider_response_id': resp.get('id'),
               'invocation_time': t0, 'input_image_path': str(imgs[view]), 'input_image_sha256': sha(img),
               'prompt_sha256': sha(P[view].encode()), 'usage': resp.get('usage'), 'parsed': parsed, 'raw_text': text,
               'calls': result.calls, 'acceptance_eligible': bool(result.acceptance_eligible
                   and settings.developer(resp.get('model')) not in developers), 'cost_usd': result.cost_usd}
        (out / f'{a.name}-{view}.json').write_text(json.dumps(rec, indent=1)); return view, rec

    with ThreadPoolExecutor(route['execution']['concurrency']) as ex:
        recs = dict(ex.map(run, a.views.split(',')))
    eligible = all(r['acceptance_eligible'] for r in recs.values())
    ok = eligible and set(recs) == {'full', 'top', 'bottom'} and all(r['parsed'].get('verdict') == 'approve' and r['parsed'].get('confidence') in ('medium', 'high')
             and not r['parsed'].get('issues') and not r['parsed'].get('required_fixes') for r in recs.values())
    combined = {'schema_version': 'deep-research-diagram-native-review-v1', 'diagram': a.name, 'reviewer_model': route['model'],
                'builder_model': a.builder_model, 'verdict': 'approve' if ok else 'reject',
                'acceptance_eligible': eligible, 'writer_calls': writers, 'writer_verification': writer_verification,
                'inspected_views': list(recs),
                'image_sha256': recs.get('full', next(iter(recs.values())))['input_image_sha256'],
                'source_ir_sha256': sha(json.dumps(ir, sort_keys=True).encode()),
                'views': {v: {k: r[k] for k in ('provider_response_id', 'served_model', 'input_image_sha256', 'prompt_sha256')} | {'parsed': r['parsed']}
                          for v, r in recs.items()},
                'cost_usd': round(sum(r['cost_usd'] for r in recs.values()), 6) if all(r['cost_usd'] is not None for r in recs.values()) else None}
    (out / f'{a.name}-visual-review.json').write_text(json.dumps(combined, indent=1))
    for v, r in recs.items():
        p = r['parsed']; print(v, p.get('verdict'), p.get('confidence'), p.get('rules_failed'), p.get('issues'))
    print('VERDICT', combined['verdict'], 'cost', combined['cost_usd'])
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
