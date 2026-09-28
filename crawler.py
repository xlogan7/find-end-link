#!/usr/bin/env python3
"""Discover, classify, navigate, and export website destinations."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
from typing import Optional
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlparse, urlsplit, urlunsplit

from playwright.sync_api import BrowserContext, Error, Locator, Page, TimeoutError, sync_playwright

def challenge_present(page):
    try:
        return page.evaluate("""() => {
            const title = document.title.toLowerCase();
            const text = (document.body?.innerText || '').toLowerCase();
            return title.includes('just a moment') ||
                ['verifying you are human', 'performing security verification',
                 'checking your browser', 'verify you are human'].some(s => text.includes(s)) ||
                !!document.querySelector('#challenge-running, #challenge-stage');
        }""")
    except Error:
        # A document being replaced is not yet ready for export.
        return True


LOGO_CANDIDATES_JS = r"""brand => {
    const items = [];
    const add = (url, score, source, svg = null, signals = []) => {
        if (!url && !svg) return;
        try { items.push({url: url ? new URL(url, document.baseURI).href : null,
                          score, source, svg, signals}); } catch {}
    };
    const brandName = (brand || '').trim().toLowerCase();
    const marked = el => /logo/i.test([el.id, el.getAttribute('class'),
        el.getAttribute('alt'), el.getAttribute('aria-label'),
        el.getAttribute('title'), el.getAttribute('itemprop'), el.tagName].join(' '));
    for (const img of document.querySelectorAll('img')) {
        const parent = img.closest('logo, [class*="logo" i], [id*="logo" i], [itemprop="logo"]');
        const siteChrome = img.closest('header, nav, [role="banner"], #masthead, [class*="header" i], [id*="header" i]');
        const promotional = img.closest('[class*="popup" i], [id*="popup" i], [class*="modal" i], [id*="modal" i], footer, [class*="footer" i], [id*="footer" i]');
        const link = img.closest('a[href]');
        const identity = [img.alt, img.getAttribute('aria-label'), img.title,
            img.id, img.className, parent?.id, parent?.className].filter(Boolean).join(' ').toLowerCase();
        let href = null;
        try { href = link ? new URL(link.href, document.baseURI) : null; } catch {}
        const homeLink = href && href.origin === location.origin && ['/', ''].includes(href.pathname);
        const ratio = img.naturalHeight ? img.naturalWidth / img.naturalHeight : 0;
        for (const url of [img.currentSrc, img.getAttribute('data-src'),
                           img.getAttribute('data-lazy-src'), img.src]) {
            let score = 0;
            const signals = [];
            if (parent) { score += 70; signals.push('logo container'); }
            if (siteChrome) { score += 45; signals.push('header/navigation'); }
            if (marked(img)) { score += 30; signals.push('logo-labelled image'); }
            if (/logo/i.test(url || '')) { score += 20; signals.push('logo URL'); }
            if (brandName && identity.includes(brandName)) {
                score += (parent || siteChrome || homeLink) ? 100 : 40;
                signals.push('brand match');
            }
            if (homeLink) { score += 30; signals.push('home link'); }
            if (ratio >= 1.5) { score += 15; signals.push('wide image'); }
            if (ratio && ratio <= 1.2) { score -= 15; signals.push('square icon'); }
            if (promotional) { score -= 100; signals.push('popup/modal/footer'); }
            if (/\b(install|shortcut|app[-_ ]?icon)\b/i.test(`${url || ''} ${identity}`)) {
                score -= 60; signals.push('install/app asset');
            }
            if (score > 0) add(url, score, 'logo image', null, signals);
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
                             kind + '_source': candidate['source'], kind + '_original_mime': mime,
                             kind + '_score': candidate.get('score'),
                             kind + '_signals': candidate.get('signals', [])})
            if candidate.get('url'):
                (folder / (kind + '_url.txt')).write_text(candidate['url'] + '\n', encoding='utf-8')
            return
        except (Error, ValueError) as exc:
            metadata[kind + '_errors'].append({'url': candidate.get('url'), 'error': str(exc)})


def save_final_page(page, output_dir, reason, brand=None):
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
        save_image(page, folder, page.evaluate(LOGO_CANDIDATES_JS, brand), 'logo', metadata)
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


MAX_CLICKS = 10
NAVIGATION_TIMEOUT_MS = 15_000
SETTLE_TIMEOUT_MS = 2_500

# Earlier entries have higher priority. Indonesian actions are deliberately first.
ACTION_LABELS = [
    "Daftar",
    "Login",
    "Masuk",
    "Daftar Sekarang",
    "Login Sekarang",
    "Register",
    "Sign In",
    "Join",
    "Continue",
    "Create Account",
    "Sign Up",
    "Log In",
    "Mulai",
    "Lanjut",
]

CLICKABLE_SELECTOR = ", ".join(
    [
        "a",
        "button",
        "[role='button']",
        "[role='link']",
        "[onclick]",
        "[tabindex]",
        "input[type='button']",
        "input[type='submit']",
    ]
)

# Never click controls that imply credential, payment, or irreversible submission.
BLOCKED_TERMS = {
    "bayar", "payment", "pay now", "purchase", "checkout", "deposit",
    "kirim", "submit", "confirm", "konfirmasi", "verify", "verifikasi",
    "password", "kata sandi", "otp", "pin",
}


@dataclass
class Candidate:
    locator: Locator
    text: str
    description: str
    score: tuple[int, int, int, int]


@dataclass
class Step:
    number: int
    kind: str
    source_url: str
    destination_url: str
    clicked_text: str = ""
    clicked_element: str = ""


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_start_url(value: str) -> str:
    value = value.strip()
    markdown = re.fullmatch(r"\[[^\]]*\]\((https?://.+)\)", value, flags=re.I)
    if markdown:
        value = markdown.group(1)
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provide an HTTP(S) URL, for example https://example.com")
    return value


def normalized_url(url: str) -> str:
    """Normalize only superficial URL differences, without changing destinations."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{port}{path}?{parsed.query}#{parsed.fragment}"


def element_text(locator: Locator) -> str:
    try:
        values = locator.evaluate("""el => [el.innerText, el.getAttribute('aria-label'),
            el.value, el.getAttribute('title'),
            ...Array.from(el.querySelectorAll('img[alt]'), img => img.alt)]""")
        for value in values:
            if value and normalize_text(value):
                return normalize_text(value)
    except Error:
        pass
    return ""


def activate_candidate(candidate: Candidate) -> None:
    try:
        candidate.locator.click(timeout=5_000, no_wait_after=True)
    except TimeoutError as exc:
        # Only retry an action that was blocked before dispatch, never a click
        # that already ran and merely timed out waiting for navigation.
        if "intercepts pointer events" not in str(exc):
            raise
        candidate.locator.evaluate("""el => {
            if (!el.isConnected || el.matches(':disabled, [aria-disabled="true"]'))
                throw new Error('Control is no longer enabled');
            el.click();
        }""")
        print(f"Overlay blocked {candidate.text!r}; activated the control directly.",
              file=sys.stderr, flush=True)


def describe_element(locator: Locator) -> str:
    try:
        data = locator.evaluate(
            """el => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || '',
                role: el.getAttribute('role') || '',
                href: el.getAttribute('href') || '',
                type: el.getAttribute('type') || '',
                cls: typeof el.className === 'string' ? el.className : ''
            })"""
        )
    except Exception:
        return "clickable element"
    attrs = []
    for key in ("id", "role", "type", "href"):
        if data.get(key):
            attrs.append(f'{key}="{normalize_text(data[key])[:180]}"')
    classes = normalize_text(data.get("cls", ""))
    if classes:
        attrs.append(f'class="{classes[:100]}"')
    return f"<{data.get('tag', 'element')}{(' ' + ' '.join(attrs)) if attrs else ''}>"


def match_score(text: str, label: str, priority: int) -> Optional[tuple[int, int, int, int]]:
    actual = normalize_text(text).casefold()
    wanted = label.casefold()
    if actual == wanted:
        quality = 0
    elif re.fullmatch(rf"[\W_]*{re.escape(wanted)}[\W_]*", actual):
        quality = 1
    elif re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", actual):
        quality = 2
    else:
        return None
    # Label priority comes first, then match quality for that label.
    language_group = 0 if priority < 5 else 1
    return language_group, priority, quality, len(actual)


def find_best_candidate(page: Page) -> Optional[Candidate]:
    candidates = [candidate for frame in page.frames
                  if (candidate := find_frame_candidate(frame)) is not None]
    return min(candidates, key=lambda item: item.score) if candidates else None


def find_frame_candidate(page) -> Optional[Candidate]:
    elements = page.locator(CLICKABLE_SELECTOR)
    candidates: list[Candidate] = []
    try:
        count = min(elements.count(), 500)
    except Exception:
        return None

    for index in range(count):
        element = elements.nth(index)
        try:
            if not element.is_visible() or not element.is_enabled():
                continue
        except Exception:
            continue

        # Navigation links inside forms are fine; submission controls are not.
        try:
            if element.evaluate("""el => {
                const tag = el.tagName.toLowerCase();
                return !!el.form && ((tag === 'button' && el.type === 'submit') ||
                    (tag === 'input' && ['submit', 'image'].includes(el.type)));
            }"""):
                continue
        except Exception:
            continue

        text = element_text(element)
        lowered = text.casefold()
        if not text or any(term in lowered for term in BLOCKED_TERMS):
            continue

        for priority, label in enumerate(ACTION_LABELS):
            score = match_score(text, label, priority)
            if score is None:
                continue
            candidates.append(
                Candidate(element, text, describe_element(element), (*score[:3], index))
            )

    return min(candidates, key=lambda item: item.score) if candidates else None


def page_signature(page: Page) -> str:
    try:
        title = page.title()
        body = page.locator("body").inner_text(timeout=1_500)[:20_000]
        controls = [frame.locator(CLICKABLE_SELECTOR).evaluate_all(
            "els => els.map(el => [el.textContent, el.getAttribute('href'), el.getAttribute('aria-label')])"
        ) for frame in page.frames]
        return hashlib.sha256(f"{normalized_url(page.url)}\n{title}\n{body}\n{controls}".encode()).hexdigest()
    except Exception:
        return hashlib.sha256(normalized_url(page.url).encode()).hexdigest()


def wait_for_verification(page: Page, seconds: float, headless: bool) -> bool:
    if not challenge_present(page):
        return True
    print(f"Verification required at {page.url}.", file=sys.stderr, flush=True)
    if not headless:
        page.bring_to_front()
        print("Complete verification in the browser if prompted. Crawling resumes automatically.",
              file=sys.stderr, flush=True)
    else:
        print("Waiting for automatic verification; run without --headless if interaction is required.",
              file=sys.stderr, flush=True)
    deadline = time.monotonic() + seconds
    next_update = time.monotonic() + 15
    while time.monotonic() < deadline and not page.is_closed():
        page.wait_for_timeout(500)
        if not challenge_present(page):
            settle(page)
            if not challenge_present(page):
                print(f"Verification cleared: {page.url}", file=sys.stderr, flush=True)
                return True
        if time.monotonic() >= next_update:
            print("Still waiting for website verification...", file=sys.stderr, flush=True)
            next_update = time.monotonic() + 15
    return False


def settle(page: Page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except TimeoutError:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
    except TimeoutError:
        pass
    page.wait_for_timeout(500)


def choose_active_page(context: BrowserContext, prior_pages: list[Page], current: Page) -> Page:
    new_pages = [page for page in context.pages if page not in prior_pages and not page.is_closed()]
    if new_pages:
        selected = new_pages[-1]
        settle(selected)
        return selected
    return current


def crawl(start_url: str, headless: bool = True, output_dir=None, challenge_timeout=0) -> tuple[list[Step], str, str, str]:
    start_url = parse_start_url(start_url)

    steps: list[Step] = []
    stop_reason = "No relevant clickable action exists."

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        navigation_events: list[str] = []

        def track_navigation(frame) -> None:
            if frame == frame.page.main_frame and frame.url.startswith(("http://", "https://")):
                navigation_events.append(frame.url)

        def track_request(request) -> None:
            # HTTP redirect hops do not emit framenavigated events.
            if not request.is_navigation_request():
                return
            try:
                if request.frame != request.frame.page.main_frame:
                    return
            except Error:
                # A popup's first request can precede creation of its frame.
                pass
            if request.url.startswith(("http://", "https://")):
                navigation_events.append(request.url)

        context.on("request", track_request)
        context.on("page", lambda opened: opened.on("framenavigated", track_navigation))
        page.on("framenavigated", track_navigation)

        page.goto(start_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        settle(page)
        wait_for_verification(page, challenge_timeout, headless)
        initial_url = page.url
        previous = start_url
        for observed_url in navigation_events:
            if observed_url.startswith(("http://", "https://")) and normalized_url(previous) != normalized_url(observed_url):
                steps.append(Step(0, "Redirect", previous, observed_url))
                previous = observed_url
        visited_urls = {normalized_url(page.url)}
        visited_states = {page_signature(page)}

        for click_number in range(1, MAX_CLICKS + 1):
            if challenge_present(page):
                stop_reason = "Blocked by browser verification challenge. Destination not reached; content and logo were not exported."
                break
            candidate = find_best_candidate(page)
            deadline = time.monotonic() + SETTLE_TIMEOUT_MS / 1000
            while candidate is None and time.monotonic() < deadline:
                page.wait_for_timeout(200)
                candidate = find_best_candidate(page)
            if candidate is None:
                if challenge_present(page):
                    stop_reason = "A browser verification challenge blocks further navigation; this is the last reached URL."
                break

            source_url = page.url
            source_state = page_signature(page)
            prior_pages = list(context.pages)
            event_start = len(navigation_events)

            try:
                print(f"[{click_number}] Click: {candidate.text} at {source_url}",
                      file=sys.stderr, flush=True)
                activate_candidate(candidate)
            except Exception as exc:
                stop_reason = f"Could not click {candidate.text!r} ({candidate.description}): {exc}"
                break

            page = choose_active_page(context, prior_pages, page)
            settle(page)
            # Popups may be created asynchronously after the click handler returns.
            page = choose_active_page(context, prior_pages, page)
            wait_for_verification(page, challenge_timeout, headless)
            destination_url = page.url
            print(f"    -> {destination_url}", file=sys.stderr, flush=True)
            observed = []
            for observed_url in navigation_events[event_start:]:
                if not observed or normalized_url(observed_url) != normalized_url(observed[-1]):
                    observed.append(observed_url)
            click_result_url = observed[0] if observed else destination_url

            steps.append(
                Step(
                    click_number,
                    "Click",
                    source_url,
                    click_result_url,
                    candidate.text,
                    candidate.description,
                )
            )

            # Record subsequent client/server navigations in their observed order.
            previous = click_result_url
            for observed_url in observed[1:]:
                if normalized_url(observed_url) != normalized_url(previous):
                    steps.append(Step(click_number, "Redirect", previous, observed_url))
                    previous = observed_url
            if normalized_url(previous) != normalized_url(destination_url):
                steps.append(Step(click_number, "Redirect", previous, destination_url))

            current_url_key = normalized_url(page.url)
            current_state = page_signature(page)
            if (current_url_key != normalized_url(source_url) and current_url_key in visited_urls) or current_state in visited_states or current_state == source_state:
                stop_reason = "A repeated URL/page state was detected."
                break
            visited_urls.add(current_url_key)
            visited_states.add(current_state)
        else:
            stop_reason = f"Maximum depth of {MAX_CLICKS} clicks reached."

        final_url = page.url
        if challenge_present(page):
            stop_reason = "Blocked by browser verification challenge. Destination not reached; content and logo were not exported."
        if output_dir is not None:
            save_final_page(page, output_dir, stop_reason)
        browser.close()
        return steps, initial_url, final_url, stop_reason


def print_report(start_url: str, steps: list[Step], initial_url: str, final_url: str, reason: str) -> None:
    print("START")
    print(start_url)
    if not any(step.number == 0 for step in steps) and normalized_url(start_url) != normalized_url(initial_url):
        print("\n[0] Redirect")
        print(f"{start_url} -> {initial_url}")

    display_number = 0
    for step in steps:
        display_number += 1
        print(f"\n[{display_number}] {step.kind}" + (f": {step.clicked_text}" if step.clicked_text else ""))
        if step.clicked_element:
            print(f"Element: {step.clicked_element}")
        print(f"{step.source_url} -> {step.destination_url}")

    print("\nLAST REACHED URL (BLOCKED)" if reason.startswith("Blocked") else "\nFINAL URL")
    print(final_url)
    print(f"\nStop reason: {reason}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover website URLs and classify reached pages using brand matching and manual logo verification."
    )
    parser.add_argument("url", help="Starting URL, including http:// or https://")
    parser.add_argument('--brands', default='brand.json', help='JSON file containing brand names')
    parser.add_argument('--max-pages', type=int, default=100, help='Maximum navigation attempts per run; 0 checks until the queue is exhausted')
    parser.add_argument('--legacy', action='store_true', help='Use the original prioritized button-chain crawler')
    parser.add_argument("--output-dir", default="Output", help="Folder for page HTML, logos, favicons and reports (default: Output)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--headed",
        action="store_true",
        help="Show Chromium while crawling (useful for debugging).",
    )
    mode.add_argument("--headless", action="store_true", help="Hide the browser; terminal Y/N prompts remain enabled.")
    parser.add_argument("--challenge-timeout", type=float, default=180,
                        help="Seconds to wait for verification (default: 180). Browser is visible by default.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        args.url = parse_start_url(args.url)
        if args.challenge_timeout < 0:
            raise ValueError("--challenge-timeout must be nonnegative")
        if args.max_pages < 0:
            raise ValueError('--max-pages must be nonnegative (0 means unlimited)')
        if not args.legacy:
            report = crawl_all(args.url, load_brands(args.brands), args.output_dir,
                args.headless, args.challenge_timeout, args.max_pages)
            return 2 if report['status'] == 'LIMIT REACHED' or any(
                p['classification'] == 'BLOCKED' for p in report['pages']) else 1 if report['errors'] else 0
        steps, initial_url, final_url, reason = crawl(args.url, headless=args.headless,
            output_dir=args.output_dir, challenge_timeout=args.challenge_timeout)
        print_report(args.url, steps, initial_url, final_url, reason)
        return 2 if reason.startswith("Blocked") else 1 if reason.startswith("Could not click") else 0
    except KeyboardInterrupt:
        print("Crawler interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Crawler failed: {exc}", file=sys.stderr)
        return 1


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
    return frame.locator(CLICKABLE_SELECTOR).evaluate_all(CONTROL_SNAPSHOT_JS)


def crawl_all(start_url, brands, output_dir='Output', headless=False,
              challenge_timeout=180, max_pages=100,
              classifier=human_logo, asset_extensions=ASSET_EXTENSIONS):
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
    opened_urls = set()
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
            try:
                snapshot = snapshot_controls(frame)
            except Error as exc:
                print(f'[Discovery] Frame changed or detached; skipping controls: {str(exc).splitlines()[0]}', flush=True)
                continue
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
                    requested_key = url_key(requested)
                    if action is None:
                        opened_urls.add(requested_key)
                        known_urls = {key[0] for key in queue.counts} | visited | opened_urls
                        print(f'[Open {len(opened_urls)}/{len(known_urls)}] {requested}', flush=True)
                    else:
                        print(f'[Action] Reopening source page: {requested}', flush=True)
                    response = page.goto(requested, wait_until='domcontentloaded', timeout=NAVIGATION_TIMEOUT_MS)
                    print('[Load] Waiting briefly for redirects and page content...', flush=True)
                    settle(page)
                    if action is not None:
                        action_source = page.url
                        print('[Action] Activating queued navigation control (timeout: 5s)...', flush=True)
                        frame_index, element_index = action
                        prior = list(context.pages)
                        page.frames[frame_index].locator(CLICKABLE_SELECTOR).nth(element_index).click(timeout=5000)
                        settle(page)
                        page = choose_active_page(context, prior, page)
                        settle(page)
                        if url_key(action_source) != url_key(page.url):
                            print(f'[Redirect] {action_source} -> {page.url}', flush=True)
                    if response:
                        hops = []
                        req = response.request
                        while req:
                            hops.append(req.url)
                            req = req.redirected_from
                        hops = hops[::-1]
                        report['redirects'].append({'requested_url': requested, 'http_hops': hops, 'reached_url': page.url})
                        displayed_hops = list(hops)
                        if not displayed_hops:
                            displayed_hops.append(requested)
                        if url_key(displayed_hops[-1]) != url_key(page.url):
                            displayed_hops.append(page.url)
                        for source, destination in zip(displayed_hops, displayed_hops[1:]):
                            if url_key(source) != url_key(destination):
                                print(f'[Redirect] {source} -> {destination}', flush=True)
                    if not wait_for_verification(page, challenge_timeout, headless):
                        record.update(url=page.url, classification='BLOCKED')
                        record['folder'] = str(save_final_page(page, root, 'Blocked by verification'))
                        report['pages'].append(record)
                        continue
                    key = url_key(page.url)
                    print(f'[Page] {page.url}', flush=True)
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
                    folder = save_final_page(page, root, 'Awaiting manual classification', crawl_brand)
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


if __name__ == "__main__":
    raise SystemExit(main())
