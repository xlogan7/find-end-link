"""Save rendered HTML, PNG logo, favicon and metadata without extra dependencies."""
import base64
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlparse

from playwright.sync_api import Error
from verification import challenge_present


LOGO_CANDIDATES_JS = r"""() => {
    const items = [];
    const add = (url, score, source, svg = null) => {
        if (!url && !svg) return;
        try { items.push({url: url ? new URL(url, document.baseURI).href : null,
                          score, source, svg}); } catch {}
    };
    const marked = el => /logo/i.test([el.id, el.getAttribute('class'),
        el.getAttribute('alt'), el.getAttribute('aria-label'),
        el.getAttribute('title'), el.getAttribute('itemprop'), el.tagName].join(' '));
    for (const img of document.querySelectorAll('img')) {
        const parent = img.closest('logo, [class*="logo" i], [id*="logo" i], [itemprop="logo"]');
        for (const url of [img.currentSrc, img.getAttribute('data-src'),
                           img.getAttribute('data-lazy-src'), img.src]) {
            const score = marked(img) ? 100 : parent ? 90 : /logo/i.test(url || '') ? 80 : 0;
            if (score) add(url, score, 'logo image');
        }
    }
    for (const el of document.querySelectorAll('logo, [class*="logo" i], [id*="logo" i], [itemprop="logo"]')) {
        if (el.matches('a[href], link[href]') && /\.(svg|png|jpe?g|webp|gif|ico)(\?|$)/i.test(el.href))
            add(el.href, 85, 'logo link');
        if (el.getAttribute('content')) add(el.getAttribute('content'), 95, 'logo metadata');
        for (const match of getComputedStyle(el).backgroundImage.matchAll(/url\(["']?(.*?)["']?\)/g))
            add(match[1], 85, 'logo background');
        const svg = el.matches('svg') ? el : el.querySelector('svg');
        if (svg) add(null, 90, 'inline logo SVG', new XMLSerializer().serializeToString(svg));
    }
    const walk = value => {
        if (!value || typeof value !== 'object') return;
        if (value.logo) {
            const logo = value.logo;
            add(typeof logo === 'string' ? logo : logo.contentUrl || logo.url, 95, 'structured logo');
        }
        for (const child of Object.values(value)) {
            if (Array.isArray(child)) child.forEach(walk);
            else if (child && typeof child === 'object') walk(child);
        }
    };
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
        try { walk(JSON.parse(script.textContent)); } catch {}
    }
    for (const icon of document.querySelectorAll('link[rel~="icon"], link[rel="apple-touch-icon"]'))
        add(icon.href, 10, 'site icon fallback');
    return items.sort((a, b) => b.score - a.score);
}"""

EXTENSIONS = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/svg+xml': '.svg',
              'image/webp': '.webp', 'image/gif': '.gif', 'image/x-icon': '.ico',
              'image/vnd.microsoft.icon': '.ico', 'image/avif': '.avif'}


def image_bytes(page, candidate):
    url = candidate.get('url')
    if candidate.get('svg'):
        return candidate['svg'].encode('utf-8'), 'image/svg+xml'
    if url and url.startswith('data:'):
        header, payload = url.split(',', 1)
        mime = header[5:].split(';')[0].lower()
        return (base64.b64decode(payload) if ';base64' in header else unquote_to_bytes(payload)), mime
    if url and urlparse(url).scheme in {'http', 'https'}:
        response = page.context.request.get(url, headers={'Referer': page.url}, timeout=10000)
        try:
            if not response.ok:
                raise ValueError(f'HTTP {response.status}')
            return response.body(), response.headers.get('content-type', '').split(';')[0].lower()
        finally:
            response.dispose()
    raise ValueError('Unsupported image URL')


def as_png(page, body, mime):
    if mime not in EXTENSIONS or not body:
        raise ValueError(f'Not a supported image: {mime}')
    # Decode in an isolated browser document, preserving transparency. This is
    # real conversion, including SVG/ICO, rather than renaming the extension.
    converter = page.context.new_page()
    try:
        data = 'data:' + mime + ';base64,' + base64.b64encode(body).decode('ascii')
        encoded = converter.evaluate("""data => new Promise((resolve, reject) => {
            const img = new Image();
            const timer = setTimeout(() => reject(new Error('Image decode timed out')), 5000);
            img.onerror = () => { clearTimeout(timer); reject(new Error('Image decode failed')); };
            img.onload = () => {
                clearTimeout(timer);
                try {
                    const scale = Math.min(1, 4096 / Math.max(img.naturalWidth, img.naturalHeight));
                    const canvas = document.createElement('canvas');
                    canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
                    canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
                    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
                    resolve(canvas.toDataURL('image/png').split(',')[1]);
                } catch (error) { reject(error); }
            };
            img.src = data;
        })""", data)
        return base64.b64decode(encoded)
    finally:
        converter.close()


def save_image(page, folder, candidates, kind, metadata):
    seen = set()
    for candidate in candidates:
        key = candidate.get('url') or candidate.get('svg')
        if key in seen:
            continue
        seen.add(key)
        print(f'[Export] Trying {kind} candidate {len(seen)}...', flush=True)
        try:
            body, mime = image_bytes(page, candidate)
            body = as_png(page, body, mime)
            filename = kind + '.png'
            (folder / filename).write_bytes(body)
            metadata.update({kind + '_url': candidate.get('url'), kind + '_file': filename,
                             kind + '_source': candidate['source'], kind + '_original_mime': mime})
            if candidate.get('url'):
                (folder / (kind + '_url.txt')).write_text(candidate['url'] + '\n', encoding='utf-8')
            return
        except (Error, ValueError) as exc:
            metadata[kind + '_errors'].append({'url': candidate.get('url'), 'error': str(exc)})


def save_final_page(page, output_dir, reason):
    host = re.sub(r'[^a-zA-Z0-9.-]', '_', urlparse(page.url).hostname or 'page')
    folder = Path(output_dir) / f"{host}_{datetime.now():%Y%m%d_%H%M%S_%f}"
    folder.mkdir(parents=True, exist_ok=False)
    metadata = {'final_url': page.url, 'stop_reason': reason,
                'captured_at': datetime.now().astimezone().isoformat(),
                'content_file': None, 'content_format': 'rendered HTML',
                'logo_url': None, 'logo_file': None, 'logo_source': None, 'logo_errors': [],
                'favicon_url': None, 'favicon_file': None, 'favicon_source': None, 'favicon_errors': []}
    if challenge_present(page):
        metadata.update(status='blocked', final_url=None, last_reached_url=page.url)
    else:
        (folder / 'content.txt').write_text(page.content(), encoding='utf-8')
        metadata.update(status='exported', title=page.title(), content_file='content.txt')
        save_image(page, folder, page.evaluate(LOGO_CANDIDATES_JS), 'logo', metadata)
        icons = page.evaluate("""() => [
            ...Array.from(document.querySelectorAll('link[rel~="icon"], link[rel="apple-touch-icon"]'),
                el => ({url: el.href, source: 'declared favicon'})),
            {url: new URL('/favicon.ico', location.href).href, source: 'favicon.ico fallback'}
        ]""")
        save_image(page, folder, icons, 'favicon', metadata)
    (folder / 'metadata.json').write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'[Export] Saved: {folder.resolve()}', flush=True)
    for kind in ('logo', 'favicon'):
        print(f'[Export] {kind}: {metadata[kind + "_file"] or "not available (see metadata)"}', flush=True)
    return folder
