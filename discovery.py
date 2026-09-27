"""HTML-wide URL discovery and temporary human classification workflow."""
from collections import Counter
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

from playwright.sync_api import Error, sync_playwright

from page_export import save_final_page


ASSET_EXTENSIONS = set(('js jss mjs css map png jpg jpeg gif svg ico webp avif bmp '
    'woff woff2 ttf otf eot mp3 mp4 wav ogg webm mov avi zip gz taYr rar 7z pdf').split())


def url_key(value):
    p = urlsplit(value)
    port = p.port
    host = (p.hostname or '').lower()
    if ':' in host:
        host = f'[{host}]'
    if port and (p.scheme.lower(), port) not in {('http', 80), ('https', 443)}:
        host += f':{port}'
    # Preserve query ordering, trailing slashes and SPA hash routes.
    fragment = p.fragment if p.fragment.startswith(('/', '!')) else ''
    return urlunsplit((p.scheme.lower(), host, p.path or '/', p.query, fragment))


def eligible_url(value, base, asset_extensions=ASSET_EXTENSIONS):
    value = unescape(value).strip().replace('\\/', '/')
    if not value or (value.startswith('#') and not value.startswith(('#/', '#!'))):
        return None
    try:
        absolute = urljoin(base, value)
        p = urlsplit(absolute)
        if p.scheme.lower() not in {'http', 'https'} or not p.hostname or p.username or p.password:
            return None
        if any(segment.rsplit('.', 1)[-1] in asset_extensions
               for segment in unquote(p.path).lower().split('/') if '.' in segment):
            return None
        return url_key(absolute)
    except ValueError:
        return None


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []
        self.base = None
        self.code = []
        self.resources = []

    def handle_data(self, data):
        self.code.append(data)

    def handle_comment(self, data):
        self.code.append(data)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'base' and self.base is None:
            self.base = attrs.get('href')
        for name, value in attrs.items():
            if not value or tag == 'base' or name in {'action', 'formaction'}:
                continue
            if ((tag in {'img', 'script', 'source', 'video', 'audio', 'track', 'embed'}
                 and name in {'src', 'data-src', 'data-lazy-src', 'poster'}) or
                (tag == 'link' and name == 'href' and
                 set(attrs.get('rel', '').lower().split()) &
                 {'stylesheet', 'icon', 'apple-touch-icon', 'preload', 'prefetch', 'preconnect', 'dns-prefetch'})):
                self.resources.append(value)
                continue
            if name in {'href', 'src', 'data-href', 'data-url', 'data-link'}:
                self.values.append(value)
            else:
                self.code.append(value)
        if tag == 'meta' and attrs.get('http-equiv', '').lower() == 'refresh':
            match = re.search(r'url\s*=\s*(.+)', attrs.get('content', ''), re.I)
            if match:
                self.values.append(match[1].strip("'\" "))


def discover_counts(html, base, asset_extensions=ASSET_EXTENSIONS):
    parser = Links()
    parser.feed(html)
    base = urljoin(base, parser.base) if parser.base else base
    code = unescape('\n'.join(parser.code)).replace('\\/', '/')
    # Absolute URLs anywhere in HTML, plus quoted paths in inline JS/JSON.
    raw = parser.values + re.findall(r'https?://[^\s<>"\'`\\]+', code)
    raw += re.findall(r'''["']((?:/|\./|\.\./)[^"'\s<>]+)["']''', code)
    resources = {eligible_url(value, base, set()) for value in parser.resources}
    return Counter(url for value in raw
                   if (url := eligible_url(value, base, asset_extensions)) and url not in resources)


def discover_html(html, base, asset_extensions=ASSET_EXTENSIONS):
    return list(discover_counts(html, base, asset_extensions))


def action_priority(label):
    from crawler import ACTION_LABELS
    normalized = re.sub(r'\s+', ' ', label).strip().casefold()
    # Exact labels must win over a shorter phrase contained in the label.
    for index, wanted in enumerate(ACTION_LABELS):
        if normalized == wanted.casefold():
            return index
    for index, wanted in enumerate(ACTION_LABELS):
        if re.search(rf'(?<!\w){re.escape(wanted.casefold())}(?!\w)', normalized):
            return index
    return len(ACTION_LABELS)


class VisitQueue:
    def __init__(self):
        self.pending = {}
        self.counts = Counter()
        self.priorities = {}
        self.order = 0

    def add(self, url, action=None, count=0, priority=1000):
        key = (url, action)
        self.counts[key] += count
        self.priorities[key] = min(priority, self.priorities.get(key, priority))
        if key not in self.pending:
            self.pending[key] = self.order
            self.order += 1

    def pop(self):
        key = min(self.pending, key=lambda key: (
            self.priorities[key], -self.counts[key], self.pending[key]))
        del self.pending[key]
        return key

    def __len__(self):
        return len(self.pending)


def load_brands(filename):
    data = json.loads(Path(filename).read_text(encoding='utf-8-sig'))
    if isinstance(data, dict):
        data = data.get('brands', data.get('brand_name', data.get('brand')))
    if isinstance(data, str):
        data = [data]
    if not isinstance(data, list) or not data or any(not isinstance(x, str) or not x.strip() for x in data):
        raise ValueError('brand.json must contain {"brands": ["Your Brand"]} with at least one real brand.')
    return [x.strip() for x in data]


def dominant_brand(html, brands):
    """Count non-overlapping mentions; longer brand names win overlaps.

    Equal counts are resolved by the order in brand.json.
    """
    names = {}
    for brand in brands:
        names.setdefault(brand.casefold(), brand)
    if not names:
        return None, {}
    pattern = re.compile('|'.join(re.escape(name) for name in
                                 sorted(names, key=len, reverse=True)))
    counts = dict.fromkeys(names.values(), 0)
    for match in pattern.finditer(unescape(html).casefold()):
        counts[names[match.group()]] += 1
    counts = {brand: count for brand, count in counts.items() if count}
    return (max(counts, key=counts.get) if counts else None), counts


def human_logo(url):
    while True:
        answer = input(f'Logo identified at {url}? [Y/N]: ').strip().upper()
        if answer in {'Y', 'N'}:
            return answer == 'Y'
        print('Please enter Y for Yes or N for No.')


CONTROL_SNAPSHOT_JS = """els => ({
    base: document.baseURI,
    controls: els.map((el, index) => {
        const href = el.getAttribute('href') || '';
        const style = getComputedStyle(el);
        return {
            index,
            label: [el.innerText, el.matches('input[type=button], input[type=submit]') ? el.value : '',
                el.getAttribute('aria-label'), el.getAttribute('title'),
                ...Array.from(el.querySelectorAll('img[alt]'), i => i.alt)].filter(Boolean).join(' '),
            target: href || el.getAttribute('data-href') || el.getAttribute('data-url') || el.getAttribute('data-link') || '',
            safe: !el.closest('form') && !el.matches(':disabled, [aria-disabled="true"], input[type=submit], input[type=image]') &&
                !el.hasAttribute('download') && !el.querySelector('input') &&
                !!el.getClientRects().length && style.visibility !== 'hidden' && style.display !== 'none' &&
                (!href || /^javascript:/i.test(href) || (href.startsWith('#') && el.hasAttribute('onclick')))
        };
    })
})"""


def snapshot_controls(frame):
    """One browser round trip; no per-element locator auto-waits on a changing DOM."""
    from crawler import CLICKABLE_SELECTOR
    return frame.locator(CLICKABLE_SELECTOR).evaluate_all(CONTROL_SNAPSHOT_JS)


def crawl_all(start_url, brands, output_dir='Output', headless=False,
              challenge_timeout=180, max_pages=100,
              classifier=human_logo, asset_extensions=ASSET_EXTENSIONS):
    from crawler import (parse_start_url, settle, wait_for_verification,
                         CLICKABLE_SELECTOR, BLOCKED_TERMS, NAVIGATION_TIMEOUT_MS)
    start_url = parse_start_url(start_url)
    from datetime import datetime
    root = Path(output_dir) / datetime.now().strftime('crawl_%Y%m%d_%H%M%S_%f')
    root.mkdir(parents=True)
    report = {'start_url': start_url, 'brands': brands, 'classifier': 'manual',
              'classification': 'INCONCLUSIVE', 'pages': [], 'redirects': [], 'errors': [],
              'max_pages': max_pages, 'stop_on_logo_missing': True}
    queue = VisitQueue()
    queue.add(start_url)
    visited = set()
    attempted = set()
    counted_pages = {}
    attempts = 0
    yes_streak = no_streak = 0
    crawl_brand = None
    finished = False

    def schedule(page, counts):
        print('[Discovery] Identifying URLs and prioritizing navigation actions...', flush=True)
        priorities = {}
        actions = []
        frames = list(page.frames)
        for fi, frame in enumerate(frames):
            print(f'[Discovery] Scanning frame {fi + 1}/{len(frames)}...', flush=True)
            try:
                snapshot = snapshot_controls(frame)
            except Error as exc:
                print(f'[Discovery] Frame changed or detached; skipping controls: {str(exc).splitlines()[0]}', flush=True)
                continue
            print(f'[Discovery] Read {len(snapshot["controls"])} controls in one snapshot.', flush=True)
            for control in snapshot['controls']:
                label = control['label']
                priority = action_priority(label)
                target = eligible_url(control['target'], snapshot['base'], asset_extensions)
                if target:
                    priorities[target] = min(priority, priorities.get(target, priority))
                    counts.setdefault(target, 1)
                    continue
                if control['safe'] and not any(term in label.lower() for term in BLOCKED_TERMS):
                    actions.append((fi, control['index'], priority))
        fallback = action_priority('')
        print(f'[Discovery] {sum(counts.values())} eligible URL occurrences; {len(counts)} unique; '
              f'{sum(counts.values()) - len(counts)} duplicate occurrences removed. '
              'Static assets and non-HTTP URLs excluded.', flush=True)
        skipped = sum(url in visited or (url, None) in attempted for url in counts)
        merged = sum((url, None) in queue.pending for url in counts)
        previous_counts = counted_pages.setdefault(url_key(page.url), Counter())
        for url, count in counts.items():
            if url not in visited and (url, None) not in attempted:
                queue.add(url, count=max(0, count - previous_counts[url]),
                          priority=priorities.get(url, fallback))
            previous_counts[url] = max(count, previous_counts[url])
        for fi, ei, priority in actions:
            if (page.url, (fi, ei)) not in attempted:
                queue.add(page.url, (fi, ei), count=1, priority=priority)
        print(f'[Queue] Skipped {skipped} already visited/attempted URLs; merged {merged} already queued URLs. '
              f'{len(queue)} destinations/actions pending.', flush=True)
        return sorted(counts, key=lambda url: (priorities.get(url, fallback), -counts[url]))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(ignore_https_errors=True, accept_downloads=False)
        context.set_default_timeout(5000)
        try:
            while queue and (max_pages == 0 or attempts < max_pages) and not finished:
                requested, action = queue.pop()
                attempted.add((requested, action))
                if action is None and url_key(requested) in visited:
                    print(f'[Skip] Already visited: {requested}', flush=True)
                    continue
                attempts += 1
                page = context.new_page()
                record = {'requested_url': requested}
                try:
                    print(f'[Open {attempts}/{max_pages or "unlimited"}] {requested} (navigation timeout: {NAVIGATION_TIMEOUT_MS // 1000}s)', flush=True)
                    response = page.goto(requested, wait_until='domcontentloaded', timeout=NAVIGATION_TIMEOUT_MS)
                    print('[Load] Waiting briefly for redirects and page content...', flush=True)
                    settle(page)
                    if action is not None:
                        print('[Action] Activating queued navigation control (timeout: 5s)...', flush=True)
                        frame_index, element_index = action
                        prior = list(context.pages)
                        page.frames[frame_index].locator(CLICKABLE_SELECTOR).nth(element_index).click(timeout=5000)
                        settle(page)
                        from crawler import choose_active_page
                        page = choose_active_page(context, prior, page)
                        settle(page)
                    if response:
                        hops = []
                        req = response.request
                        while req:
                            hops.append(req.url)
                            req = req.redirected_from
                        report['redirects'].append({'requested_url': requested, 'http_hops': hops[::-1], 'reached_url': page.url})
                    if not wait_for_verification(page, challenge_timeout, headless):
                        record.update(url=page.url, classification='BLOCKED')
                        record['folder'] = str(save_final_page(page, root, 'Blocked by verification'))
                        report['pages'].append(record)
                        continue
                    key = url_key(page.url)
                    print(f'[Reached] {page.url}', flush=True)
                    htmls = [(frame.content(), frame.url) for frame in page.frames]
                    counts = Counter()
                    for html, base in htmls:
                        counts.update(discover_counts(html, base, asset_extensions))
                    if key in visited:
                        print('[Skip] Duplicate destination; no repeat logo question.', flush=True)
                        if action is not None:
                            schedule(page, counts)
                        continue
                    links = list(counts)
                    visited.add(key)
                    record['url'] = page.url
                    record['links'] = links
                    selected_brand, brand_counts = dominant_brand(
                        '\n'.join(h for h, _ in htmls), brands)
                    matches = [selected_brand] if selected_brand else []
                    record['brand_matches'] = matches
                    record['page_dominant_brand'] = selected_brand
                    if crawl_brand is None:
                        crawl_brand = selected_brand
                    record['selected_brand'] = crawl_brand
                    report['selected_brand'] = crawl_brand
                    record['brand_counts'] = brand_counts
                    print('[Export] Saving HTML, PNG logo and favicon...', flush=True)
                    folder = save_final_page(page, root, 'Awaiting manual classification')
                    record['folder'] = str(folder)
                    print(f'\nURL: {page.url}')
                    if crawl_brand is None:
                        classification = 'TEMPROVERLY STRAY DOMAIN'
                        report['classification'] = classification
                        finished = True
                    else:
                        if len(visited) == 1:
                            print(f'BRAND FOUND: {crawl_brand} ({brand_counts[crawl_brand]} occurrences)')
                        print(f'Check the logo for {crawl_brand}.')
                        positive = classifier(page.url)
                        print(f'[Verification] {"Y confirmed; processing next step" if positive else "N confirmed; stopping as PHISHING"}.', flush=True)
                        record['logo_identified'] = positive
                        yes_streak = yes_streak + 1 if positive else 0
                        no_streak = 0 if positive else no_streak + 1
                        record.update(yes_streak=yes_streak, no_streak=no_streak)
                        report.update(yes_streak=yes_streak, no_streak=no_streak)
                        classification = 'LOGO CONFIRMED'
                        if not positive:
                            classification = f'{crawl_brand} PHISHING'
                            finished = True
                        if finished:
                            report['classification'] = classification
                        else:
                            # Only Y continues; the first N ends the entire crawl.
                            links = schedule(page, counts)
                        print(f'Logo-positive pages: {yes_streak}; no positive-answer stopping limit.', flush=True)
                    record['links'] = links
                    record['url_counts'] = dict(counts)
                    record.update(total_urls=len(counts), total_url_occurrences=sum(counts.values()),
                                  duplicate_url_occurrences=sum(counts.values()) - len(counts),
                                  frame_count=len(htmls) - 1)
                    print(f'Unique eligible URLs ({len(links)}), in priority order:')
                    for link in links:
                        print(f'  {link} ({counts[link]} occurrences)')
                    record['classification'] = classification
                    print(classification, flush=True)
                    metadata_path = folder / 'metadata.json'
                    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                    metadata.update(classification=classification, classifier='manual',
                                    logo_identified=record.get('logo_identified'), brand_matches=matches,
                                    selected_brand=crawl_brand, page_dominant_brand=selected_brand, brand_counts=brand_counts,
                                    yes_streak=yes_streak, no_streak=no_streak,
                                    configured_brands=brands, used_brand=crawl_brand,
                                    detected_brands=list(brand_counts),
                                    requested_url=requested, frame_count=len(htmls) - 1,
                                    total_urls=len(counts), total_url_occurrences=sum(counts.values()),
                                    duplicate_url_occurrences=sum(counts.values()) - len(counts),
                                    url_counts=dict(counts), urls_in_priority_order=links,
                                    navigation_attempt=attempts,
                                    stop_reason=classification)
                    metadata_path.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
                    report['pages'].append(record)
                except (Error, ValueError, IndexError) as exc:
                    report['errors'].append({'url': requested, 'error': str(exc)})
                    print(f'Could not inspect {requested}: {exc}', flush=True)
                finally:
                    for opened in list(context.pages):
                        opened.close()
                    (root / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            report['pending_count'] = len(queue)
            blocked = any(p['classification'] == 'BLOCKED' for p in report['pages'])
            if not finished and not queue and not report['errors'] and not blocked and yes_streak:
                report['classification'] = 'OUR SITE'
            report.update(total_unique_urls=len({key[0] for key in queue.counts} | visited),
                          visited_url_count=len(visited), navigation_attempts=attempts,
                          checked_page_count=sum('logo_identified' in p for p in report['pages']),
                          classification_scope='Visited pages; OUR SITE requires queue exhaustion without errors or blocks')
            report['status'] = 'CLASSIFIED' if finished else 'LIMIT REACHED' if queue else 'COMPLETED WITH ERRORS' if report['errors'] else 'COMPLETED'
        finally:
            (root / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            browser.close()
    print(f'\n{report["classification"]} - {report["status"]}. Report: {(root / "report.json").resolve()}')
    return report
