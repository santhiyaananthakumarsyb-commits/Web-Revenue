
import argparse
import csv
import json
import re
import time
import random
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# SETTINGS
# ============================================================

DEFAULT_INPUT = "companies_tier1.csv"
DEFAULT_OUTPUT_DIR = "ecosia_revenue_output"

MAX_RESULTS_PER_QUERY = 10
MAX_QUERY_VARIANTS = 4

# Delay between searches to reduce request frequency.
MIN_DELAY_SECONDS = 3
MAX_DELAY_SECONDS = 6

# Search-result evidence only. This script NEVER opens result URLs.
REVENUE_KEYWORDS = re.compile(
    r"\b(revenue|revenues|annual sales|net sales|turnover|"
    r"generated in sales|sales revenue|total sales)\b",
    re.IGNORECASE,
)

# A broad amount pattern. Matches are candidates, not verified revenue.
AMOUNT_PATTERN = re.compile(
    r"(?i)(?:"
    r"(?:US\$|USD\s*\$?|\$|€|£|INR\s*₹?|₹)\s*"
    r"\d[\d,]*(?:\.\d+)?\s*"
    r"(?:trillion|billion|million|bn|mn|m|k|crore|lakh)?"
    r"|"
    r"\d[\d,]*(?:\.\d+)?\s*"
    r"(?:trillion|billion|million|bn|mn|crore|lakh)\b"
    r")"
)

CSV_FIELDS = [
    "company_name",
    "search_query",
    "result_title",
    "result_snippet",
    "result_url",
    "revenue_candidate",
    "candidate_amounts",
    "candidate_reason",
    "search_status",
    "collected_at_utc",
]


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_filename_part(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")


def get_company_column(headers):
    """Find a likely company-name column in an input CSV."""
    normalized = {
        re.sub(r"[^a-z0-9]", "", h.lower()): h
        for h in headers
    }

    preferred = [
        "companyname",
        "company",
        "businessname",
        "organizationname",
        "organisationname",
        "name",
    ]

    for key in preferred:
        if key in normalized:
            return normalized[key]

    # Fallback: use the first column.
    return headers[0] if headers else None


def read_companies(input_path, max_companies=None):
    input_path = Path(input_path)

    if not input_path.exists():
        raise FileNotFoundError(
            f"Input file not found: {input_path.resolve()}"
        )

    companies = []
    seen = set()

    # utf-8-sig handles Excel CSV files with a BOM.
    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ValueError("The input CSV has no header row.")

        company_column = get_company_column(reader.fieldnames)

        if not company_column:
            raise ValueError("Could not find a company-name column.")

        print(f"Using company column: {company_column}")

        for row in reader:
            company = normalize_text(row.get(company_column, ""))

            if not company:
                continue

            key = company.casefold()
            if key in seen:
                continue

            seen.add(key)
            companies.append(company)

            if max_companies and len(companies) >= max_companies:
                break

    return companies


def build_queries(company):
    """
    Build search queries using the company name.
    These are Ecosia searches only.
    """
    return [
        f'"{company}" revenue',
        f'"{company}" annual revenue',
        f'"{company}" revenue million billion',
        f'"{company}" annual report revenue',
    ][:MAX_QUERY_VARIANTS]


def revenue_candidate(title, snippet):
    """
    Mark a result as a candidate only when:
    - the title/snippet mentions revenue-related wording, and
    - the same title/snippet contains a number that may be an amount.

    This is a rough screen, not verification.
    """
    text = normalize_text(f"{title} {snippet}")

    keyword_match = REVENUE_KEYWORDS.search(text)
    amount_matches = AMOUNT_PATTERN.findall(text)

    # Keep unique amount strings, without claiming that they are revenue.
    amounts = []
    for amount in amount_matches:
        amount = normalize_text(amount)
        if amount and amount not in amounts:
            amounts.append(amount)

    is_candidate = bool(keyword_match and amounts)

    if is_candidate:
        reason = (
            "Revenue-related wording and a possible amount appear "
            "in the search-result title/snippet; verify manually."
        )
    elif keyword_match:
        reason = (
            "Revenue-related wording found, but no amount matched "
            "the broad amount pattern."
        )
    else:
        reason = "No revenue-related wording detected."

    return is_candidate, amounts, reason


# ============================================================
# ECOSIA SEARCH ONLY
# ============================================================

def is_captcha_or_block(page):
    """Detect common access-block or CAPTCHA indicators."""
    try:
        url = page.url.lower()
        body = normalize_text(page.locator("body").inner_text(timeout=3000))
        body_lower = body.lower()
    except Exception:
        return False, ""

    url_indicators = [
        "captcha",
        "challenge",
        "blocked",
        "sorry",
    ]

    text_indicators = [
        "verify you are human",
        "verify that you are human",
        "unusual traffic",
        "automated requests",
        "complete the captcha",
        "enter the characters",
        "access denied",
        "temporarily blocked",
        "too many requests",
    ]

    if any(term in url for term in url_indicators):
        return True, f"Possible CAPTCHA/block URL: {page.url}"

    if any(term in body_lower for term in text_indicators):
        return True, "Possible CAPTCHA or access block detected in page text."

    return False, ""


def extract_ecosia_results(page, company, query, max_results):
    """
    Extract titles, snippets, and URLs from the Ecosia results page.
    Does not navigate to any result URL.
    """
    results = []

    # Ecosia's markup may change. Try several common result containers.
    containers = page.locator(
        "article, .result, .mainline-results .result, "
        "[data-test-id='mainline-result']"
    )

    try:
        count = min(containers.count(), max_results * 3)
    except Exception:
        count = 0

    seen_urls = set()

    for i in range(count):
        container = containers.nth(i)

        try:
            link = container.locator("a[href]").first
            href = link.get_attribute("href", timeout=1500)
            title = normalize_text(link.inner_text(timeout=1500))
        except Exception:
            continue

        if not href or not title:
            continue

        # Keep only normal web links. Never visit them.
        if href.startswith("/"):
            # Relative links are not treated as company source URLs.
            continue

        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue

        host = parsed.netloc.lower()
        if "ecosia.org" in host:
            continue

        if href in seen_urls:
            continue
        seen_urls.add(href)

        try:
            snippet = normalize_text(
                container.inner_text(timeout=1500)
            )
        except Exception:
            snippet = ""

        # Remove the title from the start of the container text if present.
        if snippet.startswith(title):
            snippet = snippet[len(title):].strip(" \n-–—|")

        # Limit noisy card text.
        snippet = snippet[:1200]

        candidate, amounts, reason = revenue_candidate(title, snippet)

        results.append({
            "company_name": company,
            "search_query": query,
            "result_title": title,
            "result_snippet": snippet,
            "result_url": href,
            "revenue_candidate": candidate,
            "candidate_amounts": "; ".join(amounts),
            "candidate_reason": reason,
            "search_status": "RESULT_FOUND",
            "collected_at_utc": utc_now(),
        })

        if len(results) >= max_results:
            break

    return results


def search_ecosia(page, company, query, max_results):
    """
    Open only an Ecosia search page.
    No result/source pages are opened.
    """
    search_url = "https://www.ecosia.org/search?q=" + quote_plus(query)

    try:
        page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(1500)

    except PlaywrightTimeoutError:
        # A timeout does not justify opening any other site.
        print(f"  Search page timed out for query: {query}")
        return [], "SEARCH_TIMEOUT"

    except Exception as exc:
        print(f"  Search error: {exc}")
        return [], "SEARCH_ERROR"

    blocked, reason = is_captcha_or_block(page)
    if blocked:
        print(f"\nSTOPPING: {reason}")
        return [], "CAPTCHA_OR_BLOCK"

    try:
        results = extract_ecosia_results(
            page=page,
            company=company,
            query=query,
            max_results=max_results,
        )
    except Exception as exc:
        print(f"  Could not parse results: {exc}")
        return [], "PARSE_ERROR"

    if results:
        return results, "RESULTS_FOUND"

    # An empty extraction may mean no results or changed page markup.
    return [], "NO_RESULTS_PARSED"


# ============================================================
# OUTPUT AND RESUME
# ============================================================

def load_processed_companies(jsonl_path):
    """
    Resume based on companies already written to the JSONL log.
    """
    processed = set()

    if not jsonl_path.exists():
        return processed

    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
                company = normalize_text(record.get("company_name", ""))
                if company:
                    processed.add(company.casefold())
            except json.JSONDecodeError:
                continue

    return processed


def count_candidate_companies(jsonl_path):
    """
    Count distinct companies with at least one result marked as a candidate.
    """
    count = 0

    if not jsonl_path.exists():
        return count

    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            if record.get("has_revenue_candidate"):
                count += 1

    return count


def append_csv_rows(csv_path, rows):
    file_exists = csv_path.exists()

    with csv_path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists or csv_path.stat().st_size == 0:
            writer.writeheader()

        if rows:
            writer.writerows(rows)


def append_jsonl(jsonl_path, record):
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Search Ecosia for company revenue candidates using "
            "search-result titles/snippets only. Never opens result websites."
        )
    )

    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-companies", type=int, default=0)
    parser.add_argument("--target-candidates", type=int, default=1000)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser without a visible window.",
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "revenue_search_log.csv"
    jsonl_path = output_dir / "revenue_search_log.jsonl"

    limit = args.max_companies if args.max_companies > 0 else None
    companies = read_companies(args.input, limit)

    processed = load_processed_companies(jsonl_path)
    candidate_count = count_candidate_companies(jsonl_path)

    print("=" * 65)
    print("ECOSIA REVENUE SEARCH")
    print("=" * 65)
    print(f"Input file: {Path(args.input).resolve()}")
    print(f"Companies loaded: {len(companies)}")
    print(f"Already processed: {len(processed)}")
    print(f"Existing candidate companies: {candidate_count}")
    print(f"Target candidates: {args.target_candidates}")
    print("Search engine: Ecosia ONLY")
    print("Open result websites: NO")
    print("=" * 65)

    if candidate_count >= args.target_candidates:
        print("Target already reached according to the existing JSONL log.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()
        stop_reason = None

        try:
            for company_number, company in enumerate(companies, start=1):
                company_key = company.casefold()

                if company_key in processed:
                    continue

                if candidate_count >= args.target_candidates:
                    print("\nTarget candidate count reached.")
                    break

                print(
                    f"\n[{company_number}/{len(companies)}] "
                    f"Searching: {company}"
                )

                all_results = []
                statuses = []
                seen_urls = set()

                for query in build_queries(company):
                    print(f"  Ecosia query: {query}")

                    results, status = search_ecosia(
                        page=page,
                        company=company,
                        query=query,
                        max_results=MAX_RESULTS_PER_QUERY,
                    )
                    statuses.append(status)

                    if status == "CAPTCHA_OR_BLOCK":
                        stop_reason = (
                            "CAPTCHA/access block detected. "
                            "Stopped without attempting to bypass it."
                        )
                        break

                    for result in results:
                        url = result["result_url"]
                        if url not in seen_urls:
                            seen_urls.add(url)
                            all_results.append(result)

                    time.sleep(
                        random.uniform(
                            MIN_DELAY_SECONDS,
                            MAX_DELAY_SECONDS,
                        )
                    )

                if stop_reason:
                    print(f"\n{stop_reason}")
                    break

                has_candidate = any(
                    result["revenue_candidate"]
                    for result in all_results
                )

                if has_candidate:
                    candidate_count += 1

                # Write a placeholder row if Ecosia returned no parseable results.
                csv_rows = all_results or [{
                    "company_name": company,
                    "search_query": "",
                    "result_title": "",
                    "result_snippet": "",
                    "result_url": "",
                    "revenue_candidate": False,
                    "candidate_amounts": "",
                    "candidate_reason": "No parseable search-result candidates.",
                    "search_status": ";".join(statuses) or "NO_RESULTS_PARSED",
                    "collected_at_utc": utc_now(),
                }]

                # Include combined search statuses on each result row.
                for row in csv_rows:
                    row["search_status"] = ";".join(statuses)

                append_csv_rows(csv_path, csv_rows)

                record = {
                    "company_name": company,
                    "has_revenue_candidate": has_candidate,
                    "result_count": len(all_results),
                    "search_statuses": statuses,
                    "results": all_results,
                    "collected_at_utc": utc_now(),
                }
                append_jsonl(jsonl_path, record)

                processed.add(company_key)

                print(
                    f"  Results: {len(all_results)} | "
                    f"Revenue candidate: {has_candidate} | "
                    f"Candidate companies so far: {candidate_count}"
                )

            if stop_reason:
                print(f"\nRun stopped: {stop_reason}")

        finally:
            context.close()
            browser.close()

    print("\nFinished.")
    print(f"CSV results: {csv_path.resolve()}")
    print(f"JSONL log: {jsonl_path.resolve()}")
    print(f"Candidate companies counted: {candidate_count}")
    print(
        "Reminder: candidates are based only on search snippets "
        "and must be checked for company identity, year, and units."
    )


if __name__ == "__main__":
    main()
