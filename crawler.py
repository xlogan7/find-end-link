#!/usr/bin/env python3
"""Follow likely authentication/navigation actions and report the final URL."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Error, Locator, Page, TimeoutError, sync_playwright
from page_export import save_final_page
from verification import challenge_present


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
            from discovery import crawl_all, load_brands
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


if __name__ == "__main__":
    raise SystemExit(main())
