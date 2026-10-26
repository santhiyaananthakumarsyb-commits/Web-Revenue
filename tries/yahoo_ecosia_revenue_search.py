
#!/usr/bin/env python3
"""
Yahoo + Ecosia company revenue research.

- Reads companies from a CSV.
- Searches Yahoo and Ecosia without a search API key.
- Opens selected result pages and extracts revenue-related evidence.
- Saves a JSONL checkpoint after every company and refreshes a CSV log.
- Resumes completed companies when restarted.
- Stops at the target number of candidate companies or the company limit.
- Stops if a CAPTCHA or access-block page appears.

IMPORTANT:
Search results are candidates, not verified financial facts.
Manually review source evidence before marking revenue as VERIFIED.
"""

import argparse
import csv
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ---------------- CONFIGURATION ----------------

DEFAULT_INPUT = "companies_tier1.csv"
DEFAULT_OUTPUT_DIR = "revenue_search_output"

MAX_COMPANIES = 8000
TARGET_CANDIDATES = 1000

MIN_DELAY = 3
MAX_DELAY = 5
MAX_RESULTS_PER_ENGINE = 8
MAX_SOURCE_PAGES_PER_COMPANY = 4

NAME_COLUMNS = [
    "Company Name", "company_name", "Company", "company",
    "name", "full_name", "Business Name", "business_name"
]

WEBSITE_COLUMNS = [
    "Company/Org URL", "Company URL", "company_url",
    "website", "Website", "domain", "URL", "url"
]

REVENUE_TERMS = re.compile(
    r"\b(revenue|revenues|turnover|net sales|sales revenue|"
    r"annual sales|annual report|financial results|total sales)\b",
    re.IGNORECASE,
)

BLOCK_TERMS = re.compile(
    r"(captcha|verify you are human|verify you're human|"
    r"unusual traffic|robot check|automated queries|"
    r"access denied|security check|prove you are not a robot)",
    re.IGNORECASE,
)

YEAR_PATTERN = re.compile(
    r"\b(?:19|20)\d{2}(?:\s*[-/–]\s*(?:19|20)?\d{2})?\b"
)

# Broad candidate matcher only. It does NOT establish that a number is revenue.
AMOUNT_PATTERN = re.compile(
    r"(?i)(?:(US\$|USD|C\$|CAD|A\$|AUD|£|GBP|€|EUR|₹|INR|¥|JPY|\$)\s*)?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(trillion|billion|million|thousand|tn|bn|mn|m|k)?"
    r"\s*(USD|GBP|EUR|INR|CAD|AUD|JPY|dollars|rupees|pounds|euros)?",
    re.IGNORECASE,
)


# ---------------- BASIC HELPERS ----------------

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pause():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def first_column(headers, possible_names):
    lookup = {h.strip().lower(): h for h in headers if h}
    for name in possible_names:
        if name.lower() in lookup:
            return lookup[name.lower()]
    return None


def normalize_website(value):
    value = (value or "").strip()
    if not value:
        return ""

    if not value.startswith(("http://", "https://")):
        value = "https://" + value

    try:
        parsed = urlparse(value)
        if parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass

    return value


def read_companies(csv_path, limit):
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Input CSV not found: {csv_path.resolve()}"
        )

    companies = []
    seen = set()

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        name_col = first_column(headers, NAME_COLUMNS)
        website_col = first_column(headers, WEBSITE_COLUMNS)

        if not name_col:
            raise ValueError(
                "Cannot find company-name column.\n"
                f"Available columns: {headers}\n"
                f"Expected one of: {NAME_COLUMNS}"
            )

        for row_num, row in enumerate(reader, start=2):
            name = (row.get(name_col) or "").strip()
            website = normalize_website(
                row.get(website_col, "")
            ) if website_col else ""

            if not name:
                continue

            key = (name.casefold(), website.casefold())
            if key in seen:
                continue

            seen.add(key)

            companies.append({
                "company_name": name,
                "website": website,
                "input_row": row_num,
            })

            if len(companies) >= limit:
                break

    return companies


# ---------------- SEARCH ENGINE HELPERS ----------------

def search_url(engine, query):
    q = quote_plus(query)

    if engine == "yahoo":
        return f"https://search.yahoo.com/search?p={q}"

    if engine == "ecosia":
        return f"https://www.ecosia.org/search?q={q}"

    raise ValueError(f"Unknown search engine: {engine}")


def get_body_text(page):
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def is_block_page(text, url):
    url = (url or "").lower()

    if any(x in url for x in ("captcha", "challenge", "blocked")):
        return True

    return bool(BLOCK_TERMS.search((text or "")[:15000]))


def safe_text(locator):
    try:
        return locator.inner_text(timeout=1500).strip()
    except Exception:
        return ""


def extract_results(page, engine, limit):
    """
    Extract external links from search result pages.
    Search engine markup changes, so several selectors are tried.
    """
    results = []
    seen_urls = set()

    selectors = (
        ["div.dd.algo", "div.algo", "li div.algo"]
        if engine == "yahoo"
        else ["article", "div.result"]
    )

    for selector in selectors:
        try:
            blocks = page.locator(selector)
            block_count = min(blocks.count(), 100)
        except Exception:
            continue

        for i in range(block_count):
            block = blocks.nth(i)

            try:
                anchors = block.locator("a[href]")
                anchor_count = min(anchors.count(), 10)
            except Exception:
                continue

            for j in range(anchor_count):
                anchor = anchors.nth(j)

                try:
                    href = (anchor.get_attribute("href") or "").strip()
                    title = safe_text(anchor)
                except Exception:
                    continue

                if not href.startswith("http"):
                    continue

                host = (urlparse(href).netloc or "").lower()

                if not host:
                    continue

                if any(domain in host for domain in (
                    "yahoo.com", "ecosia.org", "bing.com", "google.com"
                )):
                    continue

                if href in seen_urls or not title or len(title) < 3:
                    continue

                snippet = re.sub(
                    r"\s+", " ", safe_text(block)
                )[:1800]

                results.append({
                    "engine": engine,
                    "title": title[:500],
                    "url": href,
                    "snippet": snippet,
                })

                seen_urls.add(href)
                break

            if len(results) >= limit:
                break

        if len(results) >= limit:
            break

    # Fallback if the expected result-card selectors no longer work.
    if not results:
        try:
            anchors = page.locator("a[href]")
            for i in range(min(anchors.count(), 250)):
                anchor = anchors.nth(i)
                href = (anchor.get_attribute("href") or "").strip()
                title = safe_text(anchor)

                if not href.startswith("http"):
                    continue

                host = (urlparse(href).netloc or "").lower()

                if not host or any(domain in host for domain in (
                    "yahoo.com", "ecosia.org", "bing.com", "google.com"
                )):
                    continue

                if not title or len(title) < 3 or href in seen_urls:
                    continue

                results.append({
                    "engine": engine,
                    "title": title[:500],
                    "url": href,
                    "snippet": "",
                })
                seen_urls.add(href)

                if len(results) >= limit:
                    break
        except Exception:
            pass

    return results[:limit]


def make_queries(company_name, website):
    domain = ""

    if website:
        domain = urlparse(website).netloc.replace("www.", "")

    queries = []

    if domain:
        queries.append(
            f'"{company_name}" {domain} revenue annual report'
        )

    queries.extend([
        f'"{company_name}" annual revenue',
        f'"{company_name}" turnover financial results',
        f'"{company_name}" revenue latest fiscal year',
    ])

    return list(dict.fromkeys(queries))


def search_engine(page, engine, query):
    try:
        page.goto(
            search_url(engine, query),
            wait_until="domcontentloaded",
            timeout=30000,
        )

        try:
            page.wait_for_load_state(
                "networkidle", timeout=7000
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(1000)

    except PlaywrightTimeoutError:
        # The page may still contain usable results.
        pass

    except Exception as exc:
        return [], f"SEARCH_ERROR: {type(exc).__name__}: {exc}"

    text = get_body_text(page)

    if is_block_page(text, page.url):
        return [], "BLOCKED_OR_CAPTCHA"

    results = extract_results(
        page, engine, MAX_RESULTS_PER_ENGINE
    )

    if not results:
        return [], "NO_RESULTS_PARSED"

    return results, "OK"


# ---------------- REVENUE EVIDENCE EXTRACTION ----------------

def extract_revenue_mentions(text):
    """
    Extract text passages that mention revenue-related terms and
    possible amounts/years. These are candidates, not verified facts.
    """
    if not text:
        return []

    text = re.sub(r"[ \t]+", " ", text)
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)

    mentions = []
    seen = set()

    for sentence in sentences:
        sentence = sentence.strip()

        if not sentence or len(sentence) > 1800:
            continue

        if not REVENUE_TERMS.search(sentence):
            continue

        key = sentence[:300].casefold()

        if key in seen:
            continue

        seen.add(key)

        amounts = [
            match.group(0).strip()
            for match in AMOUNT_PATTERN.finditer(sentence)
        ]

        years = list(dict.fromkeys(
            YEAR_PATTERN.findall(sentence)
        ))

        mentions.append({
            "evidence_excerpt": sentence[:1200],
            "amount_candidates": amounts[:8],
            "year_candidates": years,
        })

        if len(mentions) >= 12:
            break

    return mentions


def inspect_source_page(page, result):
    url = result["url"]

    output = {
        "search_engine": result["engine"],
        "search_title": result["title"],
        "search_snippet": result["snippet"],
        "search_url": url,
        "source_page_url": url,
        "source_page_title": "",
        "source_page_status": "NOT_OPENED",
        "candidate_mentions": [],
    }

    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        page.wait_for_timeout(800)

        text = get_body_text(page)

        if is_block_page(text, page.url):
            output["source_page_status"] = "BLOCKED_OR_CAPTCHA"
            return output

        try:
            output["source_page_title"] = page.title()[:500]
        except Exception:
            pass

        output["source_page_url"] = page.url

        mentions = extract_revenue_mentions(text)
        output["candidate_mentions"] = mentions

        output["source_page_status"] = (
            "REVENUE_MENTION_FOUND"
            if mentions
            else "NO_CLEAR_REVENUE_MENTION"
        )

    except PlaywrightTimeoutError:
        output["source_page_status"] = "PAGE_TIMEOUT"

    except Exception as exc:
        output["source_page_status"] = (
            f"PAGE_ERROR: {type(exc).__name__}"
        )

    return output


# ---------------- CHECKPOINT AND CSV OUTPUT ----------------

def append_jsonl(path, record):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_processed_keys(log_path):
    processed = set()

    if not log_path.exists():
        return processed

    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)

                if record.get("company_status") in (
                    "COMPLETED",
                    "NOT_FOUND",
                    "BLOCKED_OR_CAPTCHA",
                ):
                    processed.add(record["company_key"])

            except Exception:
                continue

    return processed


def count_candidate_companies(log_path):
    if not log_path.exists():
        return 0

    count = 0

    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
                if record.get("has_revenue_candidate"):
                    count += 1
            except Exception:
                pass

    return count


def refresh_csv(log_path, csv_path):
    fields = [
        "timestamp_utc",
        "company_name",
        "website",
        "company_status",
        "verification_status",
        "has_revenue_candidate",
        "result_count",
        "candidate_amounts",
        "candidate_years",
        "candidate_evidence",
        "source_urls",
        "source_titles",
        "source_page_statuses",
    ]

    with log_path.open("r", encoding="utf-8") as src:
        with csv_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as dst:
            writer = csv.DictWriter(
                dst, fieldnames=fields, extrasaction="ignore"
            )
            writer.writeheader()

            for line in src:
                try:
                    record = json.loads(line)

                    row = dict(record)
                    row["candidate_amounts"] = " || ".join(
                        record.get("candidate_amounts", [])
                    )
                    row["candidate_years"] = " || ".join(
                        record.get("candidate_years", [])
                    )
                    row["candidate_evidence"] = " || ".join(
                        record.get("candidate_evidence", [])
                    )
                    row["source_urls"] = " | ".join(
                        record.get("source_urls", [])
                    )
                    row["source_titles"] = " | ".join(
                        record.get("source_titles", [])
                    )
                    row["source_page_statuses"] = " | ".join(
                        record.get("source_page_statuses", [])
                    )

                    writer.writerow(row)

                except Exception:
                    continue


# ---------------- MAIN PROGRAM ----------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help="Input companies CSV",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Output folder",
    )
    parser.add_argument(
        "--max-companies",
        type=int,
        default=MAX_COMPANIES,
    )
    parser.add_argument(
        "--target-candidates",
        type=int,
        default=TARGET_CANDIDATES,
    )
    parser.add_argument(
        "--max-source-pages",
        type=int,
        default=MAX_SOURCE_PAGES_PER_COMPANY,
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser without showing the window",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Delete existing output logs and start over",
    )

    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    log_path = output_dir / "revenue_search_log.jsonl"
    csv_path = output_dir / "revenue_search_log.csv"
    summary_path = output_dir / "run_summary.json"

    if args.fresh:
        for path in (log_path, csv_path, summary_path):
            if path.exists():
                path.unlink()

    companies = read_companies(
        input_path, args.max_companies
    )

    if not companies:
        print("No companies found.")
        return

    processed = load_processed_keys(log_path)
    candidate_count = count_candidate_companies(log_path)

    print(f"Companies loaded: {len(companies)}")
    print(f"Already processed: {len(processed)}")
    print(f"Candidate companies already logged: {candidate_count}")
    print(f"Output folder: {output_dir.resolve()}")
    print("Searching Yahoo and Ecosia.")
    print("Candidates require verification; they are not verified facts.")

    stop_reason = "INPUT_EXHAUSTED"
    attempted_this_run = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)

        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            locale="en-US",
        )

        search_page = context.new_page()
        source_page = context.new_page()

        try:
            for index, company in enumerate(companies, start=1):
                name = company["company_name"]
                website = company["website"]

                company_key = (
                    name.casefold() + "|" + website.casefold()
                )

                if company_key in processed:
                    continue

                if candidate_count >= args.target_candidates:
                    stop_reason = "TARGET_CANDIDATES_REACHED"
                    break

                attempted_this_run += 1
                print(f"\n[{index}/{len(companies)}] {name}")

                all_results = []
                queries_used = []
                search_statuses = []
                blocked = False

                queries = make_queries(name, website)

                for engine in ("yahoo", "ecosia"):
                    # Try query variants until this engine yields results.
                    for query in queries:
                        queries_used.append(
                            f"{engine}: {query}"
                        )

                        results, status = search_engine(
                            search_page, engine, query
                        )

                        search_statuses.append(
                            f"{engine}: {status}"
                        )

                        if status == "BLOCKED_OR_CAPTCHA":
                            blocked = True
                            print(
                                f"{engine} displayed a block/CAPTCHA. "
                                "Stopping without bypassing it."
                            )
                            break

                        if results:
                            for result in results:
                                result["query"] = query

                            all_results.extend(results)
                            break

                        pause()

                    if blocked:
                        break

                    pause()

                # Deduplicate search results by URL.
                deduped = []
                seen_urls = set()

                for result in all_results:
                    if result["url"] not in seen_urls:
                        seen_urls.add(result["url"])
                        deduped.append(result)

                # Prefer results with revenue-related wording.
                deduped.sort(
                    key=lambda r: 0 if REVENUE_TERMS.search(
                        r["title"] + " " + r["snippet"]
                    ) else 1
                )

                source_checks = []

                if not blocked:
                    for result in deduped[:args.max_source_pages]:
                        checked = inspect_source_page(
                            source_page, result
                        )
                        source_checks.append(checked)

                        if checked["source_page_status"] == "BLOCKED_OR_CAPTCHA":
                            print(
                                "A source page blocked access; "
                                "not attempting to bypass it."
                            )

                        pause()

                mentions = []

                # Search snippets are leads only.
                for result in deduped:
                    text = (
                        result["title"] + ". " + result["snippet"]
                    )

                    for mention in extract_revenue_mentions(text):
                        mentions.append({
                            **mention,
                            "evidence_type": "SEARCH_SNIPPET_CANDIDATE",
                            "source_url": result["url"],
                            "source_title": result["title"],
                        })

                # Opened source pages provide stronger evidence to review.
                for checked in source_checks:
                    for mention in checked["candidate_mentions"]:
                        mentions.append({
                            **mention,
                            "evidence_type": "SOURCE_PAGE_CANDIDATE",
                            "source_url": checked["source_page_url"],
                            "source_title": checked["source_page_title"],
                        })

                # Deduplicate excerpts by URL + text.
                unique_mentions = []
                seen_mentions = set()

                for mention in mentions:
                    key = (
                        mention.get("source_url", ""),
                        mention.get("evidence_excerpt", "").casefold(),
                    )

                    if key not in seen_mentions:
                        seen_mentions.add(key)
                        unique_mentions.append(mention)

                has_candidate = bool(unique_mentions)

                if blocked:
                    status = "BLOCKED_OR_CAPTCHA"
                    verification_status = "NEEDS REVIEW"
                elif has_candidate:
                    status = "COMPLETED"
                    verification_status = "NEEDS REVIEW"
                else:
                    status = "NOT_FOUND"
                    verification_status = "NOT FOUND"

                amounts = list(dict.fromkeys(
                    amount
                    for mention in unique_mentions
                    for amount in mention.get("amount_candidates", [])
                ))

                years = list(dict.fromkeys(
                    year
                    for mention in unique_mentions
                    for year in mention.get("year_candidates", [])
                ))

                evidence = [
                    f'{m["evidence_type"]}: {m["evidence_excerpt"]}'
                    for m in unique_mentions[:25]
                ]

                record = {
                    "timestamp_utc": now_utc(),
                    "company_key": company_key,
                    "input_row": company["input_row"],
                    "company_name": name,
                    "website": website,
                    "company_status": status,
                    "verification_status": verification_status,
                    "has_revenue_candidate": has_candidate,
                    "search_queries": queries_used,
                    "search_statuses": search_statuses,
                    "result_count": len(deduped),
                    "search_results": deduped,
                    "source_page_checks": source_checks,
                    "candidate_amounts": amounts,
                    "candidate_years": years,
                    "candidate_evidence": evidence,
                    "source_urls": list(dict.fromkeys(
                        [r["url"] for r in deduped]
                        + [
                            c["source_page_url"]
                            for c in source_checks
                            if c.get("source_page_url")
                        ]
                    )),
                    "source_titles": list(dict.fromkeys(
                        [r["title"] for r in deduped]
                        + [
                            c["source_page_title"]
                            for c in source_checks
                            if c.get("source_page_title")
                        ]
                    )),
                    "source_page_statuses": [
                        c["source_page_status"]
                        for c in source_checks
                    ],
                }

                # Checkpoint immediately after every company.
                append_jsonl(log_path, record)
                processed.add(company_key)

                if has_candidate:
                    candidate_count += 1

                refresh_csv(log_path, csv_path)

                print(
                    f"Results: {len(deduped)} | "
                    f"Candidate: {has_candidate} | "
                    f"Candidate companies total: {candidate_count}"
                )

                if blocked:
                    stop_reason = "BLOCKED_OR_CAPTCHA"
                    break

                if candidate_count >= args.target_candidates:
                    stop_reason = "TARGET_CANDIDATES_REACHED"
                    break

        finally:
            context.close()
            browser.close()

    summary = {
        "finished_at_utc": now_utc(),
        "input_file": str(input_path.resolve()),
        "output_dir": str(output_dir.resolve()),
        "companies_in_input_limit": len(companies),
        "processed_company_count": len(
            load_processed_keys(log_path)
        ),
        "attempted_this_run": attempted_this_run,
        "candidate_companies_logged": count_candidate_companies(
            log_path
        ),
        "target_candidates": args.target_candidates,
        "max_companies": args.max_companies,
        "stop_reason": stop_reason,
        "note": (
            "Candidates are not verified revenue records. "
            "Review the company identity, revenue definition, "
            "amount, currency, reporting period, and source before "
            "marking a result VERIFIED."
        ),
    }

    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\nFinished.")
    print(json.dumps(summary, indent=2))
    print(f"\nJSONL checkpoint: {log_path.resolve()}")
    print(f"CSV log: {csv_path.resolve()}")
    print(f"Run summary: {summary_path.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nInterrupted. Completed companies were checkpointed."
        )
