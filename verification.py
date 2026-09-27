"""Recognize verification interstitials without treating them as website content."""
from playwright.sync_api import Error


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
