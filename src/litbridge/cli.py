"""JSON CLI. Runtime errors are structured and never include raw HTTP exception URLs."""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pydantic import ValidationError
from litbridge.config import build_gateway, load_settings
from litbridge.errors import BridgeError, Code, safe_error


def error_result(exc):
    if isinstance(exc, ValidationError):
        exc = BridgeError(Code.INVALID_INPUT, 'Input/configuration failed validation; check types and bounds')
    return {'status': 'error', 'error': safe_error(exc).model_dump()}


async def dispatch(gateway, action: str, **kwargs):
    if action == 'providers':
        return gateway.providers_info()
    if action == 'models':
        return gateway.models_info()
    if action == 'cache_clear':
        return {'status': 'ok', 'removed_entries': gateway.store.clear_cache()}
    return await getattr(gateway, action)(**kwargs)


def parser():
    p = argparse.ArgumentParser(prog='litbridge', description='Local institutional literature gateway; JSON output')
    p.add_argument('--config', help='Explicit local TOML file; not auto-loaded from untrusted working directories')
    p.add_argument('--home', help='Override local data root')
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('providers', help='Capabilities/readiness, including planned sources')
    commands.add_parser('models', help='Local image model profile configuration status; no network requests')
    doctor = commands.add_parser('doctor', help='Configuration checks; --live performs limited network checks')
    doctor.add_argument('--live', action='store_true')
    search = commands.add_parser('search', help='Search and merge; limit is per provider')
    search.add_argument('text')
    search.add_argument('--provider', dest='providers', action='append')
    search.add_argument('--limit', type=int, default=10)
    search.add_argument('--mode', choices=['simple', 'native'], default='simple')
    search.add_argument('--cursors', default='{}', help='JSON object keyed by provider ID')
    resolve = commands.add_parser('resolve', help='Resolve DOI/PMCID or a persisted paper ID')
    resolve.add_argument('identifier')
    resolve.add_argument('--provider', dest='providers', action='append')
    resolve.add_argument('--refresh', action='store_true')
    for name in ('access', 'references'):
        cmd = commands.add_parser(name)
        cmd.add_argument('identifier')
        cmd.add_argument('--provider', dest='providers', action='append')
    retrieve = commands.add_parser('retrieve', help='Retrieve and normalize one paper; use batch for a persistent queue')
    retrieve.add_argument('identifier')
    retrieve.add_argument('--provider')
    retrieve.add_argument('--format', choices=['xml', 'pdf'])
    read = commands.add_parser('read', help='Read canonical Markdown by original artifact ID or document ID')
    read.add_argument('artifact_id')
    read.add_argument('--offset', type=int, default=0)
    read.add_argument('--max-chars', type=int, default=8000)
    imp = commands.add_parser('import-url', help='Import supported provider HTML page metadata')
    imp.add_argument('url')
    imp.add_argument('--provider', required=True)
    for name in ('batch', 'job-create'):
        cmd = commands.add_parser(name, help='Persistent bounded retrieval job; batch also executes it')
        cmd.add_argument('identifiers', nargs='+')
        cmd.add_argument('--provider')
        cmd.add_argument('--format', choices=['pdf', 'xml'])
    job = commands.add_parser('job-run', help='Resume queued/retryable work; successes are not repeated')
    job.add_argument('job_id')
    job.add_argument('--retry-failed', action='store_true')
    job.add_argument('--limit', type=int, default=100)
    status = commands.add_parser('job-status', help='Persistent job details or recent job list')
    status.add_argument('job_id', nargs='?')
    status.add_argument('--offset', type=int, default=0)
    status.add_argument('--limit', type=int, default=50)
    history = commands.add_parser('job-history', help='Paginated immutable attempt history')
    history.add_argument('item_id')
    history.add_argument('--offset', type=int, default=0)
    history.add_argument('--limit', type=int, default=20)
    human = commands.add_parser('human-run', help='Visible sequential manual verification and automatic resumed retrieval')
    human.add_argument('job_id')
    human.add_argument('--wait-seconds', type=int, default=300)
    human.add_argument('--limit', type=int, default=10)
    norm = commands.add_parser('normalize', help='Normalize an existing original without downloading again')
    norm.add_argument('artifact_id')
    norm.add_argument('--force', action='store_true')
    norm.add_argument('--engine', choices=['basic','docling','mathpix','model','compare'], default='basic')
    norm.add_argument('--model-profile', help='Configured profile ID for engine=model')
    norm.add_argument('--compare-profile', dest='compare_profiles', action='append', help='Two distinct profile IDs for engine=compare; repeat this option')
    norm.add_argument('--page-limit', type=int, default=3, help='Docling pages per resumable invocation, 1..5')
    norm.add_argument('--formulas', action='store_true', help='Opt-in local formula model; prefetch required')
    norm.add_argument('--cloud-limit', type=int, default=3, help='Formula requests per model per call, 1..20; comparison may send twice this count')
    norm.add_argument('--retry-cloud', action='store_true', help='Explicit retry of failed/uncertain cloud requests; may incur charges')
    ingest = commands.add_parser('ingest', help='Explicitly import a saved local PDF/XML/HTML original; CLI only')
    ingest.add_argument('path')
    ingest.add_argument('--paper', dest='identifier', required=True)
    commands.add_parser('cache-clear' , help='Clear metadata request cache, not papers or downloads')
    commands.add_parser('mcp', help='Run official MCP SDK stdio server; no TCP/REST listener')
    return p


async def run(settings, action, kwargs):
    gateway = build_gateway(settings)
    try:
        return await dispatch(gateway, action, **kwargs)
    finally:
        await gateway.close()


def main(argv=None):
    args = vars(parser().parse_args(argv))
    command = args.pop('command').replace('-', '_')
    config, home = args.pop('config'), args.pop('home')
    try:
        settings = load_settings(config, home)
        if command == 'mcp':
            try:
                from litbridge.mcp_server import create_server
            except ImportError as exc:
                raise BridgeError(Code.NOT_CONFIGURED, 'Install litbridge[mcp] to enable the official SDK server') from exc
            create_server(settings).run(transport='stdio')
            return 0
        if 'cursors' in args:
            try:
                args['cursors'] = json.loads(args['cursors'])
                if not isinstance(args['cursors'], dict) or not all(isinstance(v, str) for v in args['cursors'].values()):
                    raise ValueError()
            except ValueError as exc:
                raise BridgeError(Code.INVALID_INPUT, 'cursors must be a JSON object with string values') from exc
        result = asyncio.run(run(settings, command, args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        result = error_result(exc)
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr if command == 'mcp' else sys.stdout)
    return 0 if result.get('status') == 'ok' else 2
