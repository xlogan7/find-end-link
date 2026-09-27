# End URL Crawler

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-1.62.0-2EAD33)
![Model status](https://img.shields.io/badge/ML%20model-In%20development-orange)

**Trace website destinations. Identify brands. Review every unique page.**

A browser-based crawler that follows redirects, discovers navigation links,
and exports website content for brand review. The logo-classification model is
under development, so terminal **Y/N verification** currently supplies its decisions.

## Quick start

Run these commands in the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```powershell
.\.venv\Scripts\python.exe crawler.py "https://example.com"
```

## Current Workflow (only Focous on Phishing and Our Site)

1. Open the starting URL, follow redirects, and inspect the rendered page and frames.
2. Detect the Used Brand using brand.json.
3. If no brand occurs on the starting page, report `TEMPROVERLY STRAY DOMAIN`.
4. Save the reached page and ask whether the selected brand's logo is present.
5. Y marks the page `LOGO CONFIRMED` and continues. N immediately stops the entire crawl with `<brand> PHISHING`.
6. Filter and deduplicate URLs, prioritize `ACTION_LABELS` destinations in their
   configured order, then visit other unique URLs by descending occurrence count.
   Discovery order breaks ties. Newly discovered higher-priority links move ahead
   of lower-priority queued links. Redirects to an inspected URL are not asked again.
 7. If all queued pages are verified with Y, with no errors or verification blocks, report OUR SITE.

## Output

```text
Output/
└── crawl_<timestamp>/
    ├── report.json
    └── <domain>_<timestamp>/
        ├── content.txt
        ├── logo.png
        ├── favicon.png
        └── metadata.json
```
