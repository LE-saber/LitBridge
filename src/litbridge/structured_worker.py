"""Pinned offline Docling adapter. Input is one retained PDF, output is bounded page records."""
from __future__ import annotations
import hashlib
from importlib.metadata import version
import json
import logging
from pathlib import Path
import re
import sys
from litbridge.structured import Item, Page, MAX_PAGE


def deny_network(event, args):
    if event in ('socket.connect','socket.getaddrinfo'):
        raise OSError('Network disabled in structured normalization worker')


def converter(models, force_ocr, formulas):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    options=PdfPipelineOptions(artifacts_path=models,enable_remote_services=False,
        allow_external_plugins=False,do_ocr=True,do_table_structure=True,
        do_formula_enrichment=formulas,do_picture_classification=False,
        do_picture_description=False,document_timeout=180,
        generate_page_images=True,images_scale=3.0,
        accelerator_options=AcceleratorOptions(device='cpu',num_threads=2),
        ocr_options=RapidOcrOptions(backend='onnxruntime',lang=['ch'],force_full_page_ocr=force_ocr))
    conv=DocumentConverter(allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF:PdfFormatOption(pipeline_options=options)})
    if formulas:
        from docling.models.stages.code_formula.code_formula_vlm_model import CodeFormulaVlmModel
        conv.initialize_pipeline(InputFormat.PDF)
        pipeline=conv._get_pipeline(InputFormat.PDF)  # Pinned 2.132 adapter.
        for model in pipeline.enrichment_pipe:
            if isinstance(model,CodeFormulaVlmModel):
                # Default 18% expansion captures the neighboring column on our
                # real two-column articles. Use the detected region itself.
                model.expansion_factor=0.0
    return conv


def table_rows(item,doc,reader,height):
    """Re-read suspicious font mappings from the visible cell, never guess punctuation."""
    rows=[[cell.text for cell in row] for row in item.data.grid]
    repaired=0; unresolved=0
    source=doc.pages[item.prov[0].page_no]
    image=source.image.pil_image
    scale=image.height/height
    for cell in item.data.table_cells:
        if not re.search(r'(?:\(cid:|\bSTX\b|[\x00-\x08]|\d:\d)',cell.text):
            continue
        if cell.bbox is None:
            unresolved+=1; continue
        box=cell.bbox.to_top_left_origin(height)
        crop=image.crop((max(0,int((box.l-1)*scale)),max(0,int((box.t-1)*scale)),
            min(image.width,int((box.r+1)*scale)+1),min(image.height,int((box.b+1)*scale)+1)))
        if not 0<crop.width*crop.height<=2_000_000:
            unresolved+=1; continue
        import numpy
        # The cell is already a text region: detecting a tiny 25px line again
        # loses it. RapidOCR mutates options on calls; restore page OCR mode.
        previous={name:getattr(reader,name) for name in ('use_det','use_cls','use_rec') if hasattr(reader,name)}
        try:
            result=reader(numpy.array(crop),use_det=False,use_cls=False,use_rec=True)
        finally:
            for name,value in previous.items():
                setattr(reader,name,value)
        if result is None or result.txts is None or not len(result.txts) or min(result.scores)<.85:
            unresolved+=1; continue
        value=' '.join(result.txts)
        for row in range(cell.start_row_offset_idx,min(cell.end_row_offset_idx,len(rows))):
            for col in range(cell.start_col_offset_idx,min(cell.end_col_offset_idx,len(rows[row]))):
                rows[row][col]=value
        repaired+=1
    return rows,repaired,unresolved


def convert_page(conv, source, sha, job, number, width, height, force_ocr, formulas):
    result=conv.convert(source,page_range=(number,number),max_num_pages=100,
        max_file_size=32*1024*1024,raises_on_error=False)
    if str(result.status.value) not in ('success','partial_success'):
        raise ValueError('Page conversion did not succeed')
    doc=result.document
    items=[]; warnings=[]
    from docling.datamodel.base_models import InputFormat
    ocr_reader=conv._get_pipeline(InputFormat.PDF).ocr_model.reader
    for item, depth in doc.iterate_items():
        if not getattr(item,'prov',None):
            continue
        label=item.label.value
        if label in ('page_header','page_footer'):
            continue
        bbox=item.prov[0].bbox.to_top_left_origin(height)
        # Clamp harmless rounding overshoot, not arbitrary out-of-page geometry.
        coords=(max(0,bbox.l),max(0,bbox.t),min(width,bbox.r),min(height,bbox.b))
        value=getattr(item,'text','')
        kind={'title':'heading','section_header':'heading','table':'table',
            'picture':'figure','formula':'formula','list_item':'list_item'}.get(label,'paragraph')
        rows=[]; level=None; method=None
        if kind=='table':
            if item.data.num_rows>1000 or item.data.num_cols>1000:
                raise ValueError('Table exceeds bound')
            rows,repaired,unresolved=table_rows(item,doc,ocr_reader,height)
            if repaired:
                method='table_ocr'
                warnings.append(f'{repaired} suspicious table font mappings re-read with local cell OCR; verify numerical values')
            if unresolved:
                warnings.append(f'{unresolved} suspicious table font mappings remain unresolved; consult original region')
            value='Table'
            if any(cell.row_span>1 or cell.col_span>1 for cell in item.data.table_cells):
                warnings.append('Model table spans flattened in Markdown; consult original region')
        elif kind=='heading':
            level=min(6,max(1,getattr(item,'level',depth+1)))
        elif kind=='figure':
            value='[Figure; consult original region]'
        elif kind=='formula':
            if formulas and value.strip():
                method='formula_model'
                value='$$\n'+value.strip()+'\n$$'
                warnings.append('Model-generated formula; verify symbols, indices and layout against original region')
            else:
                value=(value.strip()+' ' if value.strip() else '')+'[Formula structure unverified; consult original region]'
                warnings.append('Formula model not enabled or no formula recognized; original region retained')
        if value.strip() or rows:
            items.append(Item(kind=kind,text=value,rows=rows,bbox=coords,level=level,method=method))
        if len(items)>20000:
            raise ValueError('Too many blocks')
    if force_ocr:
        warnings.append('Page OCR was used; recognition confidence is not measured accuracy')
    if result.status.value=='partial_success':
        warnings.append('Local page conversion reported partial success; inspect original region')
    return Page(job=job,source_sha=sha,page=number,width=width,height=height,
        method='ocr' if force_ocr else 'layout',items=items,warnings=list(dict.fromkeys(warnings)))


def main():
    source, sha, job, selected, target, models, formulas=sys.argv[1:]
    source, target, models=Path(source),Path(target),Path(models)
    formulas=formulas=='1'
    try:
        logging.disable(logging.CRITICAL)
        sys.addaudithook(deny_network)  # Defense in depth, not an OS sandbox.
        if version('docling')!='2.132.0':
            raise ValueError('Unexpected runtime version')
        inventory=json.loads((models/'litbridge-models.json').read_text(encoding='utf-8'))
        for name, dist in [('rapidocr','rapidocr'),('onnxruntime','onnxruntime'),
                ('docling_core','docling-core'),('docling_ibm_models','docling-ibm-models'),('torch','torch')]:
            if version(dist)!=inventory[name]:
                raise ValueError('Runtime changed since model prefetch')
        if source.stat().st_size>32*1024*1024 or hashlib.sha256(source.read_bytes()).hexdigest()!=sha:
            raise ValueError('Input changed')
        from pypdf import PdfReader
        reader=PdfReader(source,strict=False)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('Opening password required')
        pages=json.loads(selected)
        if not 1<=len(pages)<=5 or len(reader.pages)>100:
            raise ValueError('Invalid selected pages')
        converters={}; failures=[]
        for number in pages:
            stage='preflight'
            try:
                page=reader.pages[number-1]
                width,height=map(float,(page.mediabox.width,page.mediabox.height))
                if not 0<width*height*9<=20_000_000:
                    raise ValueError('Page pixel bound exceeded')
                stream=page.get_contents()
                if stream is not None and len(stream.get_data())>16*1024*1024:
                    raise ValueError('Content stream bound exceeded')
                native=page.extract_text() or ''
                force_ocr=len(''.join(native.split()))<200
                if force_ocr not in converters:
                    try:
                        converters[force_ocr]=converter(models,force_ocr,formulas)
                    except Exception:
                        (target/'error.json').write_text('{"error":"initialization_failed"}',encoding='utf-8')
                        return
                stage='conversion'
                record=convert_page(converters[force_ocr],source,sha,job,number,width,height,force_ocr,formulas)
                stage='serialization'
                raw=record.model_dump_json().encode('utf-8')
                if len(raw)>MAX_PAGE:
                    raise ValueError('Output bound exceeded')
                stage=target/f'{number}.tmp'
                stage.write_bytes(raw)
                stage.replace(target/f'{number}.json')
            except Exception:
                # Parent reports missing page IDs and keeps prior completed pages.
                failures.append({'page':number,'stage':stage})
                continue
        (target/'failures.json').write_text(json.dumps(failures),encoding='utf-8')
    except Exception:
        (target/'error.json').write_text('{"error":"initialization_failed"}',encoding='utf-8')


if __name__=='__main__':
    main()
