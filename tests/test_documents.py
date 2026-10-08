"""Synthetic articles only; parsing tests do not assert publisher entitlements."""
import asyncio
import hashlib
from io import BytesIO
import json
from pathlib import Path
import pytest
from reportlab.pdfgen import canvas
from pypdf import PdfWriter
from litbridge.documents import Documents
from litbridge.errors import BridgeError
from litbridge.storage import Store
from conftest import XML, DOI


def pdf_bytes(pages=2):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(400,500), invariant=1)
    for n in range(1,pages+1):
        c.setFont('Helvetica',14)
        c.drawString(30,450,f'Canonical fixture page {n}')
        c.setFont('Helvetica',11)
        c.drawString(30,420,f'This is selectable original text on page {n}.')
        c.showPage()
    c.save()
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    yield s
    s.close()


async def test_xml_canonical_cache_locators_and_pagination(store, monkeypatch):
    a = store.save('paper','fixture','xml','https://example.org/paper',XML)
    docs = Documents(store)
    result = await docs.normalize(a.id)
    assert result['status']=='ready' and result['block_count']==4
    ast = json.loads(Path(result['json_path']).read_text())
    assert ast['blocks'][3]['parent_id']==ast['blocks'][2]['id']
    assert ast['original']['id']==a.id and 'Short abstract.' in ast['markdown']
    assert "local-name()='p'" in ast['blocks'][3]['source']['path']
    chunks, offset = [],0
    async def must_not_reparse(*a, **k):
        raise AssertionError('Cached reading must not spawn a parser')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',must_not_reparse)
    assert (await docs.normalize(a.id))['reused']
    while True:
        part = await docs.read(result['document_id'],offset,17)
        assert len(part['text'])<=17
        chunks.append(part['text'])
        if part['next_offset'] is None:
            break
        offset=part['next_offset']
    assert ''.join(chunks)==ast['markdown']
    assert hashlib.sha256(Path(a.path).read_bytes()).hexdigest()==a.sha256
    with pytest.raises(BridgeError):
        await docs.read(a.id,max_chars=20001)


async def test_pdf_pages_and_original_retained(store):
    raw=pdf_bytes()
    a=store.save('paper','fixture','pdf','https://example.org/paper',raw)
    docs=Documents(store)
    result=await docs.normalize(a.id)
    assert result['status']=='ready'
    read=await docs.read(a.id)
    assert 'page 1' in read['text'] and 'page 2' in read['text']
    assert {loc['source']['page'] for loc in read['locations']}=={1,2}
    assert Path(a.path).read_bytes()==raw
    ast=json.loads(Path(result['json_path']).read_text())
    for block in ast['blocks']:
        assert ast['markdown'][block['start']:block['end']].strip()


async def test_scanned_or_blank_pdf_is_not_false_readable_success(store):
    out=BytesIO(); writer=PdfWriter(); writer.add_blank_page(400,500); writer.write(out)
    a=store.save('paper','fixture','pdf','https://example.org/paper',out.getvalue())
    result=await Documents(store).normalize(a.id)
    assert result['status']=='needs_ocr' and result['block_count']==0
    assert 'OCR was not run' in ' '.join(result['warnings'])
    assert Path(a.path).is_file()


async def test_pdf_form_text_is_not_mistaken_for_a_scan(store):
    buffer=BytesIO(); c=canvas.Canvas(buffer,pagesize=(400,500),invariant=1)
    c.beginForm('native-content'); c.drawString(30,450,'Native article text inside a PDF form'); c.endForm()
    c.doForm('native-content'); c.showPage(); c.save()
    raw=buffer.getvalue(); a=store.save('paper','fixture','pdf','https://example.org',raw)
    docs=Documents(store); result=await docs.normalize(a.id); read=await docs.read(a.id)
    assert result['status']=='ready' and 'Native article text inside a PDF form' in read['text']
    assert Path(a.path).read_bytes()==raw


async def test_mixed_pdf_partial(store):
    from pypdf import PdfReader
    w=PdfWriter(); w.add_page(PdfReader(BytesIO(pdf_bytes(1))).pages[0]); w.add_blank_page(400,500)
    out=BytesIO(); w.write(out)
    a=store.save('paper','fixture','pdf','https://example.org/paper',out.getvalue())
    result=await Documents(store).normalize(a.id)
    assert result['status']=='partial'
    assert '2;' in ' '.join(result['warnings'])


async def test_bad_and_encrypted_pdf_retains_original(store):
    out=BytesIO(); w=PdfWriter(); w.add_blank_page(400,500); w.encrypt('not-for-agent'); w.write(out)
    a=store.save('paper','fixture','pdf','https://example.org/paper',out.getvalue())
    result=await Documents(store).outcome(a.id)
    assert result['status']=='error' and result['original_retained']
    assert result['error']['code']=='unsupported' and not result['error']['retryable']
    assert result['action'].startswith('Skip this password-protected original')
    assert Path(a.path).read_bytes()==out.getvalue()


@pytest.mark.parametrize('algorithm', ['RC4-128', 'AES-128', 'AES-256'])
async def test_viewable_permission_restricted_pdf_is_readable_without_rewriting(store, algorithm):
    from pypdf import PdfReader
    out=BytesIO(); w=PdfWriter(clone_from=PdfReader(BytesIO(pdf_bytes())))
    # No copy/modify permissions, but no password required to view the text.
    w.encrypt('', owner_password='synthetic-owner-only', permissions_flag=0, algorithm=algorithm)
    w.write(out); raw=out.getvalue()
    assert PdfReader(BytesIO(raw)).is_encrypted
    a=store.save('paper','fixture','pdf','https://example.org/paper',raw)
    docs=Documents(store); result=await docs.normalize(a.id); read=await docs.read(a.id)
    assert result['status']=='ready'
    assert 'page 1' in read['text'] and 'page 2' in read['text']
    assert 'empty password' in ' '.join(result['warnings'])
    assert {loc['source']['page'] for loc in read['locations']}=={1,2}
    assert Path(a.path).read_bytes()==raw and PdfReader(a.path).is_encrypted


async def test_html_clean_blocks_and_source_path(store):
    raw=b'''<html><head><title>Fixture</title><script>do_not_include_secret()</script></head><body>
    <nav>Navigation excluded</nav><article><h1>Article</h1><p>First paragraph of actual article content.</p>
    <h2>Data</h2><table><tr><th>X</th><th>Y</th></tr><tr><td>1</td><td>2</td></tr></table></article></body></html>'''
    a=store.save('paper','local','html','local:explicit-import',raw)
    docs=Documents(store); result=await docs.normalize(a.id); read=await docs.read(a.id)
    assert 'do_not_include' not in read['text'] and 'Navigation excluded' not in read['text']
    assert '| 1 | 2 |' in read['text'] and read['text'].count('First paragraph')==1
    assert all(x['source']['path'] for x in read['locations'])
    assert Path(a.path).read_bytes()==raw


async def test_namespace_structure_and_table(store):
    raw=b'''<article xmlns="urn:test"><body><sec><title>Results</title><p>Top level paragraph for test.</p>
    <sec><title>Details</title><p>Nested paragraph is not duplicated.</p></sec>
    <table><tr><th>A</th><th>B</th></tr><tr><td>one</td><td>two</td></tr></table></sec></body></article>'''
    a=store.save('paper','fixture','xml','https://example.org/paper',raw)
    docs=Documents(store); result=await docs.normalize(a.id)
    ast=json.loads(Path(result['json_path']).read_text())
    assert ast['markdown'].count('Nested paragraph')==1
    assert ast['blocks'][2]['parent_id']==ast['blocks'][0]['id']
    assert ast['blocks'][3]['parent_id']==ast['blocks'][2]['id']
    assert ast['blocks'][4]['rows']==[['A','B'],['one','two']]


@pytest.mark.parametrize('span, warns', [('1',False), ('2',True), ('bad',True)])
async def test_xml_unit_spans_are_not_reported_as_merged_cells(store, span, warns):
    raw=f'<article><body><p>Actual article text for this test.</p><table><tr><td rowspan="{span}" colspan="1">Cell</td></tr></table></body></article>'.encode()
    a=store.save('paper','fixture','xml','https://example.org/paper',raw)
    result=await Documents(store).normalize(a.id)
    assert ('spanning cells flattened' in ' '.join(result['warnings']))==warns


async def test_xml_graphic_formula_number_is_not_false_readable_success(store):
    raw=b'''<article><body><p>Actual article text.</p>
    <disp-formula><label>(1)</label><graphic href="private-asset"/></disp-formula>
    <disp-formula><graphic href="private-asset"/></disp-formula></body></article>'''
    a=store.save('paper','fixture','xml','https://example.org/paper',raw)
    docs=Documents(store); result=await docs.normalize(a.id); read=await docs.read(a.id)
    assert result['status']=='partial'
    assert '(1) [Formula content unavailable; consult original]' in read['text']
    assert read['text'].count('[Formula content unavailable; consult original]')==2
    assert 'private-asset' not in read['text']


@pytest.mark.parametrize('formula', ['<math><mi>x</mi><mo>=</mo><mn>1</mn></math>', 'x = 1'])
async def test_xml_text_formula_keeps_readable_content(store, formula):
    raw=f'''<article><body><p>Actual article text for this test.</p><disp-formula><label>(1)</label>
    {formula}</disp-formula></body></article>'''.encode()
    a=store.save('paper','fixture','xml','https://example.org/paper',raw)
    docs=Documents(store); result=await docs.normalize(a.id); read=await docs.read(a.id)
    assert result['status']=='ready' and '(1) x = 1' in read['text']
    assert 'content unavailable' not in read['text']


async def test_canonical_tamper_and_missing_source_detected(store):
    a=store.save('paper','fixture','xml','https://example.org/paper',XML)
    docs=Documents(store); result=await docs.normalize(a.id)
    Path(result['markdown_path']).write_text('tampered')
    with pytest.raises(BridgeError):
        await docs.read(result['document_id'])
    await docs.normalize(a.id,force=True)
    Path(a.path).unlink()
    with pytest.raises(BridgeError):
        await docs.read(result['document_id'])


async def test_existing_xml_can_be_normalized_after_restart(tmp_path):
    s=Store(tmp_path); a=s.save('paper','fixture','xml','https://example.org',XML); s.close()
    s=Store(tmp_path)
    try:
        read=await Documents(s).read(a.id)
        assert read['document']['status']=='ready' and 'synthetic' in read['text']
        assert s.db.execute('PRAGMA user_version').fetchone()[0]==1
    finally:
        s.close()


async def test_previous_parser_documents_remain_readable_by_explicit_id(store):
    from litbridge.documents import Document, VERSION
    from litbridge.models import digest
    a=store.save('paper','fixture','xml','https://example.org',XML)
    docs=Documents(store); current=await docs.normalize(a.id)
    doc=Document.model_validate_json(Path(current['json_path']).read_bytes())
    previous='1.0-litbridge-0.1.1'
    old=doc.model_copy(update={'id':'doc_'+digest(a.id+a.sha256+previous)[:24], 'parser_version':previous})
    raw=old.model_dump_json().encode(); md=old.markdown.encode()
    (docs.root/(old.id+'.json')).write_bytes(raw); (docs.root/(old.id+'.md')).write_bytes(md)
    with store.db:
        store.db.execute('INSERT INTO documents_v1 VALUES (?,?,?,?,?)',
            (old.id,a.id,previous,hashlib.sha256(raw).hexdigest(),hashlib.sha256(md).hexdigest()))
    assert (await docs.read(old.id))['document']['parser_version']==previous
    assert (await docs.read(a.id))['document']['parser_version']==VERSION
    assert (await docs.normalize(a.id))['reused']


async def test_cli_only_ingest_html(gateway, tmp_path):
    p=(await gateway.resolve(DOI))['paper']
    f=tmp_path/'saved.html'; f.write_text('<html><body><article><p>This is an explicitly imported local full article.</p></article></body></html>')
    result=await gateway.ingest(str(f),identifier=p['id'])
    assert result['normalization']['status']=='ready'
    assert result['artifact']['format']=='html' and result['artifact']['source_url']=='local:explicit-import'


async def test_xml_front_and_back_matter_are_not_silently_lost(store):
    raw=b'''<article><front><article-meta><title-group><article-title>Synthetic title</article-title></title-group>
    <abstract><p>Unique abstract text.</p></abstract></article-meta></front>
    <body><p>Main article body contains the substantive research text.</p></body>
    <back><ack><p>Unique acknowledgement.</p></ack><ref-list><title>References</title>
    <ref id="R1"><mixed-citation>Unique bibliographic citation.</mixed-citation></ref></ref-list></back></article>'''
    a=store.save('paper','fixture','xml','https://example.org',raw)
    read=await Documents(store).read(a.id)
    for value in ('Synthetic title','Unique abstract text.','Unique acknowledgement.','Unique bibliographic citation.'):
        assert read['text'].count(value)==1
    assert any("local-name()='ref'" in (x['source']['path'] or '') for x in read['locations'])
