"""Compare two explicit services on retained formula regions; agreement is not accuracy."""
from __future__ import annotations
import hashlib
import json
import os
import re
from litbridge.cloud_formula import normalize, MAX_RECORD
from litbridge.documents import MAX_DOCUMENT, original
from litbridge.errors import BridgeError, Code
from litbridge.model_services import create, formula_text
from litbridge.models import digest
from litbridge.structured import checked_path, job_lock


def records(docs, result):
    job = result.get('job_id') or result.get('document_id')
    root = checked_path(docs.store.home / 'normalization', job)
    found = {}; total = 0
    for bid, sha in docs.store.db.execute('SELECT block,sha FROM normalization_cloud_v1 WHERE job=?', (job,)):
        if not re.fullmatch(r'b[0-9]{6}', bid): raise BridgeError(Code.INVALID_CONTENT, 'Invalid comparison cache identity')
        path = checked_path(root, bid + '.json')
        if path.stat().st_size > MAX_RECORD: raise BridgeError(Code.TOO_LARGE, 'Comparison cache record exceeds bound')
        raw = path.read_bytes(); total += len(raw)
        if total > MAX_DOCUMENT: raise BridgeError(Code.TOO_LARGE, 'Comparison cache exceeds bound')
        if hashlib.sha256(raw).hexdigest() != sha: raise BridgeError(Code.INVALID_CONTENT, 'Comparison cache checksum mismatch')
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('status') not in ('sent','error','recognized','low_confidence'):
            raise BridgeError(Code.INVALID_CONTENT, 'Invalid comparison cache record')
        # Only whitelisted candidate evidence is exported. No transport headers/body/endpoint.
        found[bid] = {k: value[k] for k in ('status','text','confidence','model','profile',
            'crop_sha256','latency_seconds','usage','error') if k in value}
    return found


def fenced(text):
    longest = max((len(x) for x in re.findall(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    return fence + 'latex\n' + text + '\n' + fence


async def compare(docs, artifact_id, *, profiles, force, page_limit, cloud_limit, retry_cloud):
    if not isinstance(profiles, list) or len(profiles) != 2 or any(not isinstance(p, str) for p in profiles) or len(set(profiles)) != 2:
        raise BridgeError(Code.INVALID_INPUT, 'Compare requires two distinct model profile IDs')
    # Preflight both profiles before either service can send a paid request.
    clients = [create(docs.model_services, p) for p in profiles]
    outputs = []
    for profile, client in zip(profiles, clients):
        result = await normalize(docs, artifact_id, client=client, force=force, page_limit=page_limit,
            cloud_limit=cloud_limit, retry_cloud=retry_cloud)
        outputs.append({'profile': profile, 'normalization': result})
        if result['phase'] == 'local_layout':
            return {**result, 'engine': 'compare', 'models': outputs, 'compare_profiles': profiles,
                'action': 'Repeat with engine=compare and the same compare_profiles to finish local layout; no cloud requests sent'}
    base_ids = {x['normalization']['base_document_id'] for x in outputs}
    if len(base_ids) != 1: raise BridgeError(Code.INVALID_CONTENT, 'Model comparison baseline changed')
    base = docs._load(next(iter(base_ids)))
    a, _ = original(docs.store, artifact_id)
    candidates = [records(docs, x['normalization']) for x in outputs]
    formulas = [b for b in base.blocks if b.kind == 'formula']
    valid_ids = {b.id for b in formulas}
    if any(set(c) - valid_ids for c in candidates): raise BridgeError(Code.INVALID_CONTENT, 'Comparison formula identity mismatch')
    pairs = []; comparable = matched = 0
    stats = {p: {'completed_formulas': 0, 'recorded_latency_seconds': 0, 'reported_usage': {}} for p in profiles}
    for block in formulas:
        values = [c.get(block.id, {'status': 'pending'}) for c in candidates]
        successful = all(v['status'] in ('recognized','low_confidence') for v in values)
        same_crop = successful and bool(values[0].get('crop_sha256')) and values[0].get('crop_sha256') == values[1].get('crop_sha256')
        agreement = None
        if same_crop:
            comparable += 1
            agreement = formula_text(values[0]['text']) == formula_text(values[1]['text'])
            matched += agreement
        for profile, value in zip(profiles, values):
            if value['status'] in ('recognized','low_confidence'):
                stats[profile]['completed_formulas'] += 1
                stats[profile]['recorded_latency_seconds'] += value.get('latency_seconds', 0)
                for key, count in value.get('usage', {}).items():
                    stats[profile]['reported_usage'][key] = stats[profile]['reported_usage'].get(key, 0) + count
        if len(pairs) < 20:
            pairs.append({'block_id': block.id, 'page': block.source.page, 'bbox': block.source.bbox,
                'same_crop': same_crop, 'exact_candidate_agreement': agreement,
                'candidates': dict(zip(profiles, values))})
    for stat in stats.values(): stat['recorded_latency_seconds'] = round(stat['recorded_latency_seconds'], 3)
    complete = all(x['normalization']['phase'] == 'complete' for x in outputs)
    blocked = any(x['normalization']['status'] == 'needs_action' for x in outputs)
    state = 'partial' if complete else 'needs_action' if blocked else 'in_progress'
    count = sum(s['completed_formulas'] for s in stats.values()); goal = len(formulas) * 2
    metrics = {'total_formulas': len(formulas), 'paired_same_crop_candidates': comparable,
        'exact_candidate_matches': matched, 'candidate_agreement_pct': round(100 * matched / comparable, 1) if comparable else None,
        'processing_progress_pct': round(100 * count / goal, 1) if goal else 100,
        'verified_quality_pct': None, 'model_statistics': stats}
    report = {'schema_version': '1.0', 'artifact_id': a.id, 'original_sha256': a.sha256,
        'base_document_id': base.id, 'status': state, 'compare_profiles': profiles, 'models': outputs,
        **metrics, 'formulas': pairs, 'omitted_formulas': max(0, len(formulas) - len(pairs)),
        'agreement_rule': 'Exact LaTeX text equality after display-wrapper removal; not semantic equality or accuracy',
        'warnings': ['Candidates are untrusted and unverified; model agreement is not ground truth',
            'Latency/usage sum saved successful responses, exclude failed/uncertain billing and local layout/crop time',
            'cloud_limit bounds requests per model per invocation; comparison may send up to twice that count']}
    identity = a.id + a.sha256 + base.id + ''.join(c.version for c in clients)
    report_id = 'compare_' + digest(identity)[:24]
    root = docs.store.home / 'normalization'
    folder = checked_path(root, report_id); folder.mkdir(exist_ok=True)
    lines = ['# Formula model comparison', '', 'Status: ' + state, '',
        'Candidates require original-region verification. Agreement is not accuracy.', '',
        '```json', json.dumps(metrics, ensure_ascii=False, indent=2), '```', '']
    for pair in pairs:
        lines += [f"## {pair['block_id']} / page {pair['page']}", '',
            f"Same crop: {pair['same_crop']}; candidate agreement: {pair['exact_candidate_agreement']}", '']
        for profile, value in pair['candidates'].items():
            lines += [f"### {profile} / {value['status']}", '']
            if value.get('text'): lines += [fenced(value['text']), '']
    if report['omitted_formulas']: lines += [f"Only first 20 formula regions displayed; omitted: {report['omitted_formulas']}"]
    with job_lock(folder):
        for suffix, raw in (('.json', json.dumps(report, ensure_ascii=False, indent=2).encode()),
                            ('.md', '\n'.join(lines).encode())):
            if len(raw) > MAX_DOCUMENT: raise BridgeError(Code.TOO_LARGE, 'Comparison report exceeds bound')
            stage = checked_path(folder, 'report.tmp'); stage.write_bytes(raw)
            os.replace(stage, checked_path(folder, 'report' + suffix))
    return {'status': state, 'engine': 'compare', 'phase': 'complete' if complete else 'cloud_formulas',
        'artifact_id': a.id, 'base_document_id': base.id, 'compare_profiles': profiles, 'models': outputs,
        **metrics, 'report_id': report_id, 'report_json_path': str(folder / 'report.json'),
        'report_markdown_path': str(folder / 'report.md'),
        'action': 'Inspect the local report against original regions. Resume with the same profiles without force; uncertain requests require explicit retry_cloud=true'}
