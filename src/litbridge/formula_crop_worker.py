"""Bounded offline rendering of selected formula boxes. No credentials or cloud API."""
import hashlib
import json
import math
from pathlib import Path
import re
import sys


def main():
    source,sha,regions,target=sys.argv[1:]
    source,target=Path(source),Path(target)
    def deny(event,args):
        if event in ('socket.connect','socket.getaddrinfo'): raise OSError('Network disabled')
    sys.addaudithook(deny)
    if source.stat().st_size>32*1024*1024 or hashlib.sha256(source.read_bytes()).hexdigest()!=sha:
        raise ValueError('Original changed')
    from pypdf import PdfReader
    reader=PdfReader(source,strict=False)
    if reader.is_encrypted and not reader.decrypt(''): raise ValueError('Opening password required')
    selected=json.loads(regions)
    if not 1<=len(selected)<=20 or len(reader.pages)>100: raise ValueError('Bounds exceeded')
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(source,password='') as doc:
        for region in selected:
            bid=region['id'];number=region['page'];l,t,r,b=region['bbox']
            if not re.fullmatch(r'b[0-9]{6}',bid) or not isinstance(number,int) or not 1<=number<=len(doc):
                raise ValueError('Invalid region identity')
            if reader.pages[number-1].rotation: raise ValueError('Rotated formula regions not supported')
            media=reader.pages[number-1].mediabox;view=reader.pages[number-1].cropbox
            if tuple(media)!=tuple(view) or float(media.left)!=0 or float(media.bottom)!=0:
                raise ValueError('Nonstandard page coordinate frame not supported')
            stream=reader.pages[number-1].get_contents()
            if stream and len(stream.get_data())>16*1024*1024: raise ValueError('Content stream bound')
            page=doc[number-1]
            try:
                width,height=page.get_width(),page.get_height()
                if (not all(math.isfinite(x) for x in (l,t,r,b)) or
                        not 0<=l<r<=width+1 or not 0<=t<b<=height+1 or
                        width*height*9>20_000_000 or (r-l)*(b-t)*9>3_000_000):
                    raise ValueError('Invalid crop bounds')
                # Fixed small padding retains glyph tops/tags without the
                # proportional expansion that captured neighboring columns.
                left,top=max(0,l-1.5),max(0,t-1.5)
                right,bottom=min(width,r+1.5),min(height,b+1.5)
                if (right-left)*(bottom-top)*9>3_000_000: raise ValueError('Padded crop bound')
                bitmap=page.render(scale=3,crop=(left,height-bottom,width-right,top))
                try:
                    image=bitmap.to_pil();image.save(target/(bid+'.png'),format='PNG')
                finally: bitmap.close()
                if (target/(bid+'.png')).stat().st_size>1024*1024: raise ValueError('PNG bound')
            finally: page.close()


if __name__=='__main__':
    try: main()
    except Exception: raise SystemExit(2)  # Never print paper content or raw exceptions.
