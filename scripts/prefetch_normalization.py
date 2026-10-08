"""Explicit model setup; never called by normalize/read and never receives papers."""
import argparse
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('models',type=Path)
    parser.add_argument('--formulas',action='store_true')
    args=parser.parse_args()
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']='1'
    os.environ['HF_HUB_DISABLE_XET']='1'
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    logging.disable(logging.WARNING)
    if version('docling')!='2.132.0':
        raise ValueError('Use the pinned structured runtime')
    from docling.utils.model_downloader import download_models
    models=args.models.expanduser().resolve()
    if models.is_symlink():
        raise ValueError('Models directory must not be a symlink')
    models.mkdir(parents=True,exist_ok=True)
    print('Prefetching public layout, table and Chinese/English OCR models',flush=True)
    download_models(output_dir=models,progress=False,with_picture_classifier=False,
        with_code_formula=args.formulas,rapidocr_models=['onnxruntime:ch'])
    inventory=[]
    for path in sorted(models.rglob('*')):
        relative=path.relative_to(models)
        if path.is_file() and path.name!='litbridge-models.json' and not any(p.startswith('.') for p in relative.parts):
            if path.is_symlink() or not path.resolve().is_relative_to(models):
                raise ValueError('Model files must remain inside the chosen directory')
            hasher=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    hasher.update(chunk)
            inventory.append({'path':relative.as_posix(),'size':path.stat().st_size,'sha256':hasher.hexdigest()})
    manifest={'docling':version('docling'),'rapidocr':version('rapidocr'),
        'onnxruntime':version('onnxruntime'),'docling_core':version('docling-core'),
        'docling_ibm_models':version('docling-ibm-models'),'torch':version('torch'),
        'formulas':args.formulas,'files':inventory}
    (models/'litbridge-models.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'ready','model_files':len(inventory),
        'model_bytes':sum(x['size'] for x in inventory),'formulas':args.formulas}),flush=True)


if __name__=='__main__':
    try:
        main()
    except Exception:
        print('Model prefetch failed; no papers were processed and no complete inventory was published',flush=True)
        raise SystemExit(2)
