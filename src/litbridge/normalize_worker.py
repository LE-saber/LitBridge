"""Parser subprocess. No networking, browser interaction, LLM or OCR."""
from __future__ import annotations
from collections import defaultdict
import json
from pathlib import Path
import re
import sys
from bs4 import BeautifulSoup, Tag
from defusedxml import ElementTree as ET
from litbridge.documents import Block, Document, Locator, VERSION, MAX_DOCUMENT
from litbridge.models import digest
from litbridge.errors import BridgeError, Code


def text(node):
    return ' '.join(' '.join(node.itertext()).split())


def local(tag):
    return tag.rsplit('}', 1)[-1]


def spanning_cell(node):
    for name in ('colspan', 'rowspan'):
        value = node.get(name)
        if value is not None:
            try:
                if int(value) != 1:
                    return True
            except ValueError:
                return True  # Malformed spans cannot be claimed lossless.
    start, end = node.get('namest'), node.get('nameend')
    return (start is not None or end is not None) and start != end


class Parser:
    def __init__(self, artifact, sha, fmt):
        self.artifact, self.sha, self.fmt = artifact, sha, fmt
        self.blocks, self.warnings = [], []
        self.status, self.title = 'ready', 'Full text'

    def add(self, kind, value, *, path=None, page=None, level=None, parent=None, rows=None):
        if len(self.blocks) >= 20000:
            raise ValueError('Too many blocks')
        b = Block(id=f'b{len(self.blocks)+1:06d}', kind=kind, text=value,
            level=level, parent_id=parent, rows=rows or [],
            source=Locator(artifact_id=self.artifact, format=self.fmt, path=path, page=page))
        self.blocks.append(b)
        return b.id

    def xml(self, raw):
        root = ET.fromstring(raw)
        paths = {}
        def locate(node, path):
            paths[id(node)] = path
            counts = defaultdict(int)
            for child in node:
                tag = local(child.tag)
                counts[tag] += 1
                locate(child, path + f"/*[local-name()='{tag}'][{counts[tag]}]")
        locate(root, f"/*[local-name()='{local(root.tag)}'][1]")
        titles = [e for e in root.iter() if local(e.tag) == 'article-title']
        if titles:
            self.title = text(titles[0])
        bodies = [e for e in root.iter() if local(e.tag) == 'body']
        if not bodies:
            bodies = [e for e in root.iter() if local(e.tag) == 'originalText']
        if not bodies:
            raise ValueError('No full text body')
        def walk(node, level=1, parent=None):
            tag, path = local(node.tag), paths[id(node)]
            if tag in ('sec', 'section'):
                heading = next((e for e in node if local(e.tag) in ('title', 'section-title')), None)
                if heading is not None:
                    parent = self.add('heading', text(heading), path=paths[id(heading)], level=min(level, 6), parent=parent)
                for child in node:
                    if child is not heading:
                        walk(child, level + 1, parent)
            elif tag in ('p', 'para', 'simple-para'):
                self.add('paragraph', text(node), path=path, parent=parent)
                if any(local(e.tag) in ('math', 'inline-formula') for e in node.iter()):
                    self.warnings.append('Inline mathematical layout is represented as text; consult original locator')
            elif tag in ('title', 'section-title'):
                self.add('heading', text(node), path=path, level=min(level, 6), parent=parent)
            elif tag == 'table':
                rows = [[text(c) for c in row if local(c.tag) in ('td', 'th', 'entry')]
                        for row in node.iter() if local(row.tag) in ('tr', 'row')]
                self.add('table', text(node), path=path, parent=parent, rows=[r for r in rows if r])
                if any(spanning_cell(e) for e in node.iter()):
                    self.warnings.append('Table spanning cells flattened; exact structure remains in original')
            elif tag in ('fig', 'figure'):
                self.add('figure', text(node) or '[Figure; consult original]', path=path, parent=parent)
                self.warnings.append('Figures retained in original; only captions normalized')
            elif tag in ('disp-formula', 'formula'):
                value = text(node)
                # A graphic plus an equation number is not a readable formula.
                content = ' '.join([node.text or ''] + [
                    (text(e) if local(e.tag) != 'label' else '') + ' ' + (e.tail or '')
                    for e in node]).strip()
                if not content:
                    value = (value + ' ' if value else '') + '[Formula content unavailable; consult original]'
                    self.status = 'partial'
                    self.warnings.append('Formula content unavailable; graphic assets were not fetched or recognized')
                self.add('formula', value, path=path, parent=parent)
                self.warnings.append('Formula layout may require original')
            elif tag in ('list-item', 'listitem', 'ref', 'bib-reference'):
                self.add('list_item', text(node), path=path, parent=parent)
            elif len(node):
                for child in node:
                    walk(child, level, parent)
            elif text(node):
                self.add('paragraph', text(node), path=path, parent=parent)
        # Front abstracts and back matter are not descendants of a JATS body.
        # Keep them explicitly instead of silently dropping them from agent reads.
        inside_body = {id(e) for e in bodies[0].iter()}
        if titles and id(titles[0]) not in inside_body:
            self.add('heading', self.title, path=paths[id(titles[0])], level=1)
        front_nodes = [e for e in root.iter() if local(e.tag) in ('front', 'head')]
        for front in front_nodes:
            for abstract in [e for e in front.iter() if local(e.tag) == 'abstract' and id(e) not in inside_body]:
                parent = self.add('heading', 'Abstract', path=paths[id(abstract)], level=2)
                walk(abstract, 3, parent)
        walk(bodies[0])
        if len(bodies) > 1:
            self.warnings.append('Multiple XML bodies found; only the first body is normalized; consult original for alternatives')
        for back in [e for e in root.iter() if local(e.tag) == 'back' and id(e) not in inside_body]:
            walk(back)
        if not self.blocks and text(bodies[0]):
            self.add('paragraph', text(bodies[0]), path=paths[id(bodies[0])])

    def html(self, raw):
        soup = BeautifulSoup(raw, 'html.parser')
        self.title = soup.title.get_text(' ', strip=True) if soup.title else 'Saved article'
        paths = {}
        def locate(node, path):
            counts = defaultdict(int)
            for child in node.children:
                if isinstance(child, Tag):
                    counts[child.name] += 1
                    here = path + (' > ' if path else '') + f'{child.name}:nth-of-type({counts[child.name]})'
                    paths[id(child)] = here
                    locate(child, here)
        locate(soup, '')
        for node in soup.select('script, style, noscript, nav, header, footer, form, iframe, button'):
            node.decompose()
        body = soup.select_one('article, main, [role="main"]') or soup.body
        if body is None or len(body.get_text(' ', strip=True)) < 20:
            raise ValueError('No article text')
        headings = []
        for node in body.find_all(['h1','h2','h3','h4','h5','h6','p','li','table','figure','pre']):
            if any(p.name in ('table','figure','li','p','pre') for p in node.parents if p is not body):
                continue
            value = node.get_text(' ', strip=True)
            parent = headings[-1][1] if headings else None
            if node.name.startswith('h') and len(node.name) == 2:
                level = int(node.name[1])
                while headings and headings[-1][0] >= level:
                    headings.pop()
                parent = headings[-1][1] if headings else None
                bid = self.add('heading', value, path=paths[id(node)], level=level, parent=parent)
                headings.append((level, bid))
            elif node.name == 'table':
                rows = [[c.get_text(' ', strip=True) for c in row.find_all(['td','th'], recursive=False)] for row in node.find_all('tr')]
                self.add('table', value, path=paths[id(node)], parent=parent, rows=[r for r in rows if r])
                self.warnings.append('HTML table layout may be lossy; consult original')
            else:
                self.add({'li':'list_item', 'figure':'figure'}.get(node.name, 'paragraph'), value, path=paths[id(node)], parent=parent)
        if not self.blocks:
            self.add('paragraph', body.get_text(' ', strip=True), path=paths.get(id(body)))
        self.warnings.append('Scripts/navigation removed; embedded media remain only in original; no remote assets fetched')

    def pdf(self, raw):
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw), strict=False)
        if reader.is_encrypted:
            # Permission restrictions can use an empty opening password. The
            # flag remains True after successful authentication; it is not an
            # access test. Never guess a nonempty password or rewrite the PDF.
            if not reader.decrypt(''):
                raise BridgeError(Code.UNSUPPORTED,
                    'PDF requires an opening password; normalization skipped and original retained',
                    action='Skip this password-protected original; no password guessing or repeated download')
            self.warnings.append('PDF opened with an empty password; original encryption and permissions remain unchanged')
        if len(reader.pages) > 500:
            raise ValueError('PDF exceeds 500-page parser bound')
        empty = []
        for i, page in enumerate(reader.pages, 1):
            stream = page.get_contents()
            if stream is None:
                empty.append(i)
                continue
            if len(stream.get_data()) > 16 * 1024 * 1024:
                raise ValueError('PDF page content stream exceeds parser bound')
            value = page.extract_text(extraction_mode='layout') or ''
            if not value.strip():
                value = page.extract_text() or ''
                if value.strip():
                    self.warnings.append(f'Layout extraction was empty on page {i}; native text fallback used; reading order remains heuristic')
            if not value.strip():
                empty.append(i)
                continue
            parent = self.add('heading', f'Page {i}', page=i, level=2)
            for paragraph in re.split(r'\n\s*\n', value.strip()):
                self.add('paragraph', paragraph.strip(), page=i, parent=parent)
        self.warnings.append('PDF reading order, columns, tables and formula layout are heuristic; page locators reference originals')
        if empty:
            self.status = 'partial' if self.blocks else 'needs_ocr'
            self.warnings.append('No extractable text on pages (possibly scanned or blank): ' + ','.join(map(str,empty)) + '; OCR was not run')

    def finish(self):
        pieces, cursor = [], 0
        for b in self.blocks:
            b.start = cursor
            value = b.text.replace('<', '&lt;').replace('>', '&gt;').replace('![', '!\\[')
            if b.kind == 'heading':
                value = '#' * (b.level or 2) + ' ' + value
            elif b.kind == 'list_item':
                value = '- ' + value
            elif b.kind == 'table' and b.rows:
                width, lines = max(map(len, b.rows)), []
                for i, row in enumerate(b.rows):
                    cells = [c.replace('|','\\|').replace('\n',' ').replace('<','&lt;').replace('![','!\\[') for c in row]
                    lines.append('| ' + ' | '.join(cells + [''] * (width-len(cells))) + ' |')
                    if i == 0:
                        lines.append('| ' + ' | '.join(['---'] * width) + ' |')
                value = '\n'.join(lines)
            pieces.append(value + '\n\n')
            cursor += len(pieces[-1])
            if cursor > MAX_DOCUMENT:
                raise ValueError('Output too large')
            b.end = cursor
        return Document(id='doc_' + digest(self.artifact + self.sha + VERSION)[:24], artifact_id=self.artifact,
            original_sha256=self.sha, title=self.title, status=self.status,
            warnings=list(dict.fromkeys(self.warnings)), blocks=self.blocks, markdown=''.join(pieces))


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    except (ImportError, ValueError, OSError):
        pass  # Parent still enforces a wall deadline on Windows.
    source, output, artifact, sha, fmt = sys.argv[1:]
    try:
        path = Path(source)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Input too large')
        raw = path.read_bytes()
        import hashlib
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('Input changed before parsing')
        parser = Parser(artifact, sha, fmt)
        getattr(parser, fmt)(raw)
        result = parser.finish().model_dump_json()
        if len(result.encode('utf-8')) > MAX_DOCUMENT:
            raise ValueError('Output too large')
    except BridgeError as exc:
        result = json.dumps({'error': exc.info.code, 'message': exc.info.message, 'action': exc.info.action})
    except Exception:
        result = json.dumps({'error':'invalid_content', 'message':'Original could not be normalized within parser constraints; original retained'})
    Path(output).write_text(result, encoding='utf-8')


if __name__ == '__main__':
    main()
