"""Persistent retrieval jobs with transactional leases and append-only attempts."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import json
import time
import uuid
from litbridge.errors import BridgeError, Code, safe_error
from litbridge.models import Artifact, Source, normalize_doi, now_iso
from litbridge.transport import public_url
from litbridge.browser_workflow import MANUAL_ERRORS

TERMINAL = ('success', 'human_required', 'access_denied', 'retryable', 'provider_error')


def failure_state(errors):
    if any(e.get('code') in ('human_required', 'auth_required') for e in errors):
        return 'human_required'
    if any(e.get('retryable') for e in errors):
        return 'retryable'
    if any(e.get('code') == 'access_denied' for e in errors):
        return 'access_denied'
    return 'provider_error'


def entries(paper):
    result = [{'provider': s.provider, 'url': public_url(s.url)} for s in paper.sources]
    if paper.doi:
        result.append({'provider': 'doi', 'url': 'https://doi.org/' + paper.doi})
    return list({(e['provider'], e['url']): e for e in result if e['url']}.values())


class Ledger:
    LEASE_SECONDS = 90

    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.store.db
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS retrieval_jobs_v1
            (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, selection TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS retrieval_items_v1
            (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
             state TEXT NOT NULL, lease REAL NOT NULL DEFAULT 0, token TEXT, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS retrieval_job_order ON retrieval_items_v1(job_id,ordinal);
          CREATE TABLE IF NOT EXISTS retrieval_attempts_v1
            (sequence INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
             timestamp TEXT NOT NULL, data TEXT NOT NULL);
        ''')

    def create(self, identifiers, provider=None, format=None):
        if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 100 or
                not all(isinstance(i, str) and 0 < len(i.strip()) <= 2048 for i in identifiers)):
            raise BridgeError(Code.INVALID_INPUT, 'Batch requires 1..100 nonblank identifiers of at most 2048 characters')
        if format not in (None, 'pdf', 'xml'):
            raise BridgeError(Code.INVALID_INPUT, 'Batch format must be pdf or xml')
        if provider is not None:
            self.gateway._select('retrieve', [provider])
        job = 'job_' + uuid.uuid4().hex
        selection = {'provider': provider, 'format': format}
        seen = set()
        with self.db:
            self.db.execute('INSERT INTO retrieval_jobs_v1 VALUES (?,?,?)', (job, now_iso(), json.dumps(selection)))
            for identifier in identifiers:
                identifier = normalize_doi(identifier) or identifier.strip()
                if identifier in seen:
                    continue
                seen.add(identifier)
                p = self.gateway._stored(identifier)
                data = {'identifier': identifier, **selection, 'paper_id': p.id if p else None,
                        'entries': entries(p) if p else ([{'provider':'doi', 'url':'https://doi.org/'+identifier}]
                                   if normalize_doi(identifier) else []),
                        'artifact': None, 'normalization': None, 'errors': [], 'attempts': 0,
                        'next_action': 'Run this job', 'updated_at': now_iso()}
                self.db.execute('INSERT INTO retrieval_items_v1(id,job_id,ordinal,state,data) VALUES (?,?,?,?,?)',
                    ('item_' + uuid.uuid4().hex, job, len(seen)-1, 'queued', json.dumps(data)))
        return self.status(job)

    def _job(self, job_id):
        row = self.db.execute('SELECT created_at,selection FROM retrieval_jobs_v1 WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise BridgeError(Code.NOT_FOUND, 'Unknown retrieval job ID')
        return row

    def status(self, job_id=None, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 100:
            raise BridgeError(Code.INVALID_INPUT, 'Job pagination requires offset>=0 and limit 1..100')
        if job_id is None:
            rows = self.db.execute('SELECT id,created_at FROM retrieval_jobs_v1 ORDER BY rowid DESC LIMIT ? OFFSET ?',
                                   (limit+1, offset)).fetchall()
            return {'status':'ok', 'jobs':[{'id':j, 'created_at':d} for j,d in rows[:limit]],
                    'next_offset':offset+limit if len(rows)>limit else None}
        created, selection = self._job(job_id)
        counts = dict(self.db.execute('SELECT state,count(*) FROM retrieval_items_v1 WHERE job_id=? GROUP BY state', (job_id,)))
        rows = self.db.execute('SELECT id,state,data FROM retrieval_items_v1 WHERE job_id=? ORDER BY ordinal LIMIT ? OFFSET ?',
                               (job_id, limit+1, offset)).fetchall()
        return {'status':'ok', 'job_id':job_id, 'created_at':created, 'selection':json.loads(selection), 'counts':counts,
                'all_success':counts.get('success',0)==sum(counts.values()),
                'items':[{'item_id':i, 'state':s, **json.loads(d)} for i,s,d in rows[:limit]],
                'next_offset':offset+limit if len(rows)>limit else None, 'untrusted_content':True}

    def history(self, item_id, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 100:
            raise BridgeError(Code.INVALID_INPUT, 'History pagination requires offset>=0 and limit 1..100')
        if not self.db.execute('SELECT 1 FROM retrieval_items_v1 WHERE id=?', (item_id,)).fetchone():
            raise BridgeError(Code.NOT_FOUND, 'Unknown retrieval item ID')
        rows = self.db.execute('''SELECT timestamp,data FROM retrieval_attempts_v1 WHERE item_id=?
                                  ORDER BY sequence LIMIT ? OFFSET ?''', (item_id, limit+1, offset)).fetchall()
        return {'status':'ok', 'item_id':item_id, 'attempts':[{'timestamp':t, **json.loads(d)} for t,d in rows[:limit]],
                'next_offset':offset+limit if len(rows)>limit else None}

    def recover(self, job_id):
        with self.db:
            # Leave data/previous artifact intact; restart does not delete successful work.
            expired = self.db.execute('SELECT id FROM retrieval_items_v1 WHERE job_id=? AND state=? AND lease<?',
                                      (job_id, 'running', time.time())).fetchall()
            for (item_id,) in expired:
                changed = self.db.execute("UPDATE retrieval_items_v1 SET state='retryable', token=NULL, lease=0 WHERE id=? AND state='running' AND lease<?",
                                (item_id, time.time())).rowcount
                if changed != 1:
                    continue
                self.db.execute('INSERT INTO retrieval_attempts_v1(item_id,timestamp,data) VALUES (?,?,?)',
                    (item_id, now_iso(), json.dumps({'state':'retryable','reason':'worker_lease_expired',
                                                   'next_action':'Run job again; saved original will be reused'})))

    def claim(self, item_id, allowed):
        token = uuid.uuid4().hex
        with self.db:
            marks = ','.join('?' for _ in allowed)
            n = self.db.execute(f'''UPDATE retrieval_items_v1 SET state='running',token=?,lease=?
                                    WHERE id=? AND state IN ({marks})''',
                                (token, time.time()+self.LEASE_SECONDS, item_id, *allowed)).rowcount
            if n != 1:
                return None
            data = json.loads(self.db.execute('SELECT data FROM retrieval_items_v1 WHERE id=?', (item_id,)).fetchone()[0])
        return token, data

    def finish(self, item_id, token, state, data):
        if state not in TERMINAL:
            raise BridgeError(Code.INVALID_INPUT, 'Invalid retrieval terminal state')
        data['updated_at'] = now_iso()
        with self.db:
            n = self.db.execute('''UPDATE retrieval_items_v1 SET state=?,data=?,token=NULL,lease=0
                WHERE id=? AND state='running' AND token=? AND lease>?''',
                (state, json.dumps(data), item_id, token, time.time())).rowcount
            if n != 1:
                raise BridgeError(Code.CIRCUIT_OPEN, 'Worker lease lost; stale result was not committed', retryable=True)
            self.db.execute('INSERT INTO retrieval_attempts_v1(item_id,timestamp,data) VALUES (?,?,?)',
                (item_id, now_iso(), json.dumps({'state':state, 'errors':data['errors'],
                  'artifact_id':(data.get('artifact') or {}).get('id'), 'normalization':data.get('normalization'),
                  'next_action':data['next_action']})))

    def _finish_owned(self, item_id, token, state, data):
        try:
            self.finish(item_id, token, state, data)
        except BridgeError as exc:
            if exc.info.code != Code.CIRCUIT_OPEN:
                raise
            # Another worker reclaimed the expired lease. Do not overwrite its
            # result, or let one stale item abort the remaining batch.

    async def _heartbeat(self, item_id, token):
        while True:
            await asyncio.sleep(5)
            with self.db:
                n = self.db.execute("UPDATE retrieval_items_v1 SET lease=? WHERE id=? AND token=? AND state='running'",
                     (time.time()+self.LEASE_SECONDS, item_id, token)).rowcount
                if n != 1:
                    return

    async def _retrieve(self, data):
        p = await self.gateway._load(data['identifier'])
        data.update(paper_id=p.id, entries=entries(p))
        # Recover a validated artifact saved before a process stopped, instead of downloading again.
        rows = self.db.execute("SELECT data FROM artifacts WHERE json_extract(data,'$.paper_id')=? ORDER BY rowid DESC", (p.id,))
        for row in rows:
            a = Artifact.model_validate_json(row[0])
            if data['provider'] and a.provider != data['provider'] or data['format'] and a.format != data['format']:
                continue
            from litbridge.documents import original
            try:
                original(self.gateway.store, a.id)
            except BridgeError:
                continue
            implementation = self.gateway.providers.get(a.provider)
            bridge = getattr(implementation, 'bridge', None)
            evidence = getattr(bridge, 'identity_evidence', None)
            return {'status':'ok','artifact':a.model_dump(), 'reused_original':True, 'identity_evidence':evidence,
                    'normalization':await self.gateway.documents.outcome(a.id), 'attempt_errors':[]}
        return await self.gateway.retrieve(p.id, provider=data['provider'], format=data['format'])

    async def _item(self, item_id, allowed, human_seconds=None):
        claimed = self.claim(item_id, allowed)
        if not claimed:
            return
        token, data = claimed
        heartbeat = asyncio.create_task(self._heartbeat(item_id, token))
        data['attempts'] += 1
        try:
            if human_seconds is None:
                result = await self._retrieve(data)
            else:
                result = await self._human(data, human_seconds)
            data['entries'] = list({(e['provider'], e['url']):e for e in
                                    data['entries'] + result.get('candidate_entries', [])}.values())
            if result.get('status') == 'ok':
                data.update(artifact=result['artifact'], normalization=result.get('normalization'),
                            identity_evidence=result.get('identity_evidence'),
                            errors=result.get('attempt_errors', []), next_action='Read the canonical document',
                            reused_original=result.get('reused_original',False))
                if (data['normalization'] or {}).get('status') in ('error', 'needs_ocr', 'partial'):
                    data['next_action'] = 'Original saved; inspect normalization warnings or retry normalize, not retrieve'
                state = 'success'
            else:
                data['errors'] = result.get('errors', [])
                if not data['errors']:
                    data['errors'] = [{'code':'full_text_unavailable','message':'No matching accessible full text candidates', 'retryable':False}]
                state = self._failure_state(data)
                data['next_action'] = self._action(state)
            self._finish_owned(item_id, token, state, data)
        except asyncio.CancelledError:
            data.update(errors=[{'code':'cancelled','message':'Worker cancelled; original may already be saved', 'retryable':True}],
                        next_action='Run job again; existing originals will be reused')
            self._finish_owned(item_id, token, 'retryable', data)
            raise
        except Exception as exc:
            data['errors'] = [safe_error(exc).model_dump()]
            state = self._failure_state(data)
            data['next_action'] = self._action(state)
            self._finish_owned(item_id, token, state, data)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    def _manual_eligible(self, data):
        errors = data.get('errors', [])
        if not errors or any(e.get('code') not in MANUAL_ERRORS for e in errors):
            return False
        names = ([data['provider']] if data['provider'] else
                 [e.get('provider') for e in errors] + [e.get('provider') for e in data.get('entries', [])])
        return any(getattr(self.gateway.providers.get(name), 'human_target', None) for name in names)

    def _failure_state(self, data):
        if (getattr(self.gateway.browser, 'verification_mode', 'selectors') == 'stable'
                and self._manual_eligible(data)):
            return 'human_required'
        return failure_state(data['errors'])

    @staticmethod
    def _action(state):
        return {'human_required':'Run human-run in a visible authorized browser; complete verification manually',
                'retryable':'Retry job later; respect rate limits',
                'access_denied':'Inspect stable article entry and institutional access; no bypass attempted',
                'provider_error':'Inspect provider/configuration errors or select another source'}.get(state, '')

    async def run(self, job_id, *, retry_failed=False, limit=100):
        self._job(job_id)
        if not 1 <= limit <= 100:
            raise BridgeError(Code.INVALID_INPUT, 'Run limit must be 1..100')
        self.recover(job_id)
        allowed = ['queued', 'retryable'] + (['provider_error', 'access_denied', 'human_required'] if retry_failed else [])
        rows = self.db.execute('SELECT id,state FROM retrieval_items_v1 WHERE job_id=? ORDER BY ordinal', (job_id,)).fetchall()
        chosen = [i for i,s in rows if s in allowed][:limit]
        for item_id in chosen:
            await self._item(item_id, allowed)
        return self.status(job_id, limit=100)

    async def human_run(self, job_id, *, wait_seconds=300, limit=10):
        self._job(job_id)
        if not 1 <= wait_seconds <= 900 or not 1 <= limit <= 20:
            raise BridgeError(Code.INVALID_INPUT, 'Human wait must be 1..900 seconds per item, limit 1..20')
        if not self.gateway.browser and not any(getattr(p, 'bridge', None) for p in self.gateway.providers.values()):
            raise BridgeError(Code.NOT_CONFIGURED, 'Enable managed_browser or configure local CDP first')
        stable = (getattr(self.gateway.browser, 'verification_mode', 'selectors') == 'stable' or
                  any(getattr(getattr(p, 'bridge', None), 'verification_mode', None) == 'stable' for p in self.gateway.providers.values()))
        allowed = ['human_required'] + (['access_denied', 'provider_error'] if stable else [])
        placeholders = ','.join('?' for _ in allowed)
        rows = self.db.execute(f"SELECT id,state,data FROM retrieval_items_v1 WHERE job_id=? AND state IN ({placeholders}) ORDER BY ordinal",
                               (job_id, *allowed)).fetchall()
        selected = 0
        for item_id, state, raw in rows:
            data = json.loads(raw)
            if state != 'human_required':
                if not self._manual_eligible(data):
                    continue
            await self._item(item_id, allowed, human_seconds=wait_seconds)
            selected += 1
            if selected >= limit:
                break
        return self.status(job_id, limit=100)

    async def _human(self, data, wait_seconds):
        p = await self.gateway._load(data['identifier'])
        data.update(paper_id=p.id, entries=list({(e['provider'],e['url']):e for e in data['entries']+entries(p)}.values()))
        preferred = [e.get('provider') for e in data['errors'] if e.get('code') in ('human_required','auth_required')]
        names = [data['provider']] if data['provider'] else list(dict.fromkeys(preferred + list(self.gateway.providers)))
        target = provider = None
        for name in names:
            implementation = self.gateway.providers.get(name)
            recipe = getattr(implementation, 'human_target', None)
            if recipe:
                target = recipe(p)
                if target is None:
                    for entry in data['entries']:
                        if entry['provider'] == name:
                            enriched = p.model_copy(update={'sources': p.sources + [Source(provider=name, record_id='ledger-entry', url=entry['url'])]})
                            target = recipe(enriched)
                            if target:
                                break
                if target:
                    provider = name
                    break
        if target is None:
            raise BridgeError(Code.HUMAN_REQUIRED, 'No browser article recipe for this item; inspect stable entries or API configuration')
        bridge = getattr(self.gateway.providers[provider], 'bridge', None) or self.gateway.browser
        async with bridge.human_session(target, wait_seconds) as session:
            if session.download is not None:
                if data['format'] == 'xml':
                    raise BridgeError(Code.ACCESS_DENIED, 'Requested XML cannot be replaced by a manual PDF download')
                manual = getattr(bridge, 'manual_import', False)
                artifact = self.gateway.store.save(p.id, provider, 'pdf', getattr(bridge, 'source_url', 'local:manual-download') if manual else public_url(target.url), session.download)
                return {'status':'ok','artifact':artifact.model_dump(), 'normalization':await self.gateway.documents.outcome(artifact.id),
                        'identity_evidence':getattr(bridge, 'identity_evidence', 'Human-supplied PDF; document identity not independently verified') if manual else None}
            # Same retained tab/session; no UI confirmation required after human clearance.
            return await self.gateway.retrieve(p.id, provider=provider, format=data['format'])
