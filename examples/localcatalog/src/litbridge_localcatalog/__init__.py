"""Working separately installable example: explicit local JSON metadata catalog."""
import json
from pathlib import Path
from litbridge.errors import BridgeError, Code
from litbridge.models import Paper, ProviderInfo, SearchPage, Source, digest, normalized_text
from litbridge.providers.base import Provider


class LocalCatalog(Provider):
    info = ProviderInfo(id='localcatalog', name='Local metadata catalog example', capabilities=['search'])

    def __init__(self, path):
        if not path:
            raise BridgeError(Code.NOT_CONFIGURED, 'Set plugin_options.localcatalog.path')
        try:
            with Path(path).expanduser().open('rb') as file:
                raw = file.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError()
            items = json.loads(raw)
            if not isinstance(items, list) or len(items) > 5000:
                raise ValueError()
            self.papers = []
            for i, item in enumerate(items):
                p = Paper.model_validate(item)
                p.sources = [Source(provider='localcatalog', record_id=str(i), url='localcatalog:' + str(i),
                                    title=p.title, doi=p.doi, year=p.year, authors=p.authors)]
                self.papers.append(p.canonicalized())
            self.cache_scope = digest(raw.decode('utf-8'))
        except (OSError, ValueError, UnicodeError) as exc:
            raise BridgeError(Code.INVALID_INPUT, 'Local catalog must be a valid bounded JSON array of Paper records') from exc

    async def search(self, query):
        if query.mode != 'simple':
            raise BridgeError(Code.UNSUPPORTED, 'Local catalog supports simple text only')
        try:
            start = int(query.cursor or '0')
            if start < 0:
                raise ValueError()
        except ValueError as exc:
            raise BridgeError(Code.INVALID_INPUT, 'Invalid catalog cursor') from exc
        terms = [normalized_text(t) for t in query.text.split()]
        found = [p for p in self.papers if all(t in normalized_text(p.title + ' ' + (p.abstract or '')) for t in terms)]
        end = start + query.limit
        return SearchPage(provider='localcatalog', papers=found[start:end], total=len(found),
                          next_cursor=str(end) if end < len(found) else None, query_sent=query.text,
                          semantics='All normalized query terms in local title or abstract')


def create_provider(context):
    return LocalCatalog(context.options.get('path'))
