"""Synthetic provider contract fixtures without any website recipes."""
import httpx
import pytest
from litbridge.core import Gateway
from litbridge.models import Candidate, Paper, ProviderInfo, SearchPage, Source
from litbridge.providers.base import Provider
from litbridge.storage import Store
DOI='10.5555/litbridge.fixture'
TITLE='A reproducible literature gateway fixture for integration testing'
XML=b'<?xml version="1.0"?><article><front><abstract>Short abstract.</abstract></front><body><sec><title>Results</title><p>This synthetic full text validates the complete gateway workflow without accessing publisher content.</p></sec></body></article>'
def handler(request):
    return httpx.Response(200,content=XML)
class MetadataFixture(Provider):
    info=ProviderInfo(id='metadata',name='Synthetic metadata',capabilities=['search','resolve','references'])
    def __init__(self,transport=None):
        from types import SimpleNamespace
        self.http=SimpleNamespace(interval=0)
    def record(self):
        return Paper(title=TITLE,doi=DOI,year=2024,authors=['Ada Example'],abstract='Short abstract.',
            sources=[Source(provider=self.info.id,record_id='1',url='https://metadata.example.org/article')]).canonicalized()
    async def search(self,query):
        return SearchPage(provider=self.info.id,papers=[self.record()],query_sent=query.text,semantics='Synthetic search')
    async def resolve(self,identifier):return self.record()
    async def references(self,paper):return [{'doi':'10.5555/reference'}]
class FullTextFixture(MetadataFixture):
    info=ProviderInfo(id='fulltext',name='Synthetic full text',capabilities=['search','resolve','access','retrieve','references'])
    def record(self):
        paper=super().record();paper.abstract='A longer synthetic abstract used for enrichment.'
        return paper
    async def access(self,paper):
        return [Candidate(provider='fulltext',url='https://fulltext.example.org/article',format='xml',access='unknown',evidence='Synthetic fixture')]
    async def retrieve(self,candidate):return XML
@pytest.fixture
async def gateway(tmp_path):
    g=Gateway([MetadataFixture(),FullTextFixture()],Store(tmp_path),timeout=2)
    yield g
    await g.close()
