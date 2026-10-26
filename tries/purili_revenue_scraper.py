
import csv
import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone

import requests


# ============================================================
# CONFIGURATION
# ============================================================

INPUT_CSV = Path("companies_tier1.csv")
OUTPUT_DIR = Path("purili_output")

RESULTS_CSV = OUTPUT_DIR / "search_results.csv"
RESULTS_JSONL = OUTPUT_DIR / "search_results.jsonl"
CANDIDATES_CSV = OUTPUT_DIR / "revenue_candidates.csv"
PROGRESS_JSON = OUTPUT_DIR / "progress.json"

SEARCH_ENDPOINT = "https://puri.li/api/search"

# Start with 50 companies to test. Set to None to process all.
MAX_COMPANIES = 50

# Pause between API requests to reduce request frequency.
REQUEST_DELAY_SECONDS = 1.5

# Number of search-result pages per company.
PAGES_PER_COMPANY = 1

# Request timeout in seconds.
REQUEST_TIMEOUT_SECONDS = 30

# Set True to search each company using more than one query.
USE_QUERY_VARIANTS = False

USER_AGENT = "RevenueCandidateResearch/1.0 (public search results only)"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
})


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value):
    """Normalize text for case-insensitive company-name matching."""
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower()
    ).strip()


def load_company_names():
    """Read company names from the input CSV."""
    if not INPUT_CSV.exists():
        raise FileNotFoundError(
            f"Input file not found: {INPUT_CSV.resolve()}"
        )

    companies = []

    # utf-8-sig handles CSV files saved with a UTF-8 BOM.
    with INPUT_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:
        reader = csv.DictReader(file)

        if not reader.fieldnames:
            raise ValueError("The input CSV has no header row.")

        # Find the company_name column without case sensitivity.
        column_lookup = {
            name.strip().lower(): name
            for name in reader.fieldnames
            if name
        }

        company_column = column_lookup.get("company_name")

        if not company_column:
            raise ValueError(
                "CSV must contain a column named 'company_name'. "
                f"Found columns: {reader.fieldnames}"
            )

        for row in reader:
            name = (row.get(company_column) or "").strip()
            if name:
                companies.append(name)

    # Remove exact duplicate company names while preserving order.
    return list(dict.fromkeys(companies))


def load_progress():
    """Load completed company names so the script can resume."""
    if not PROGRESS_JSON.exists():
        return set()

    try:
        with PROGRESS_JSON.open("r", encoding="utf-8") as file:
            data = json.load(file)

        return set(data.get("completed_companies", []))

    except (json.JSONDecodeError, OSError):
        print("Warning: progress file could not be read.")
        print("Starting with an empty progress record.")
        return set()


def save_progress(completed_companies):
    """Save progress after each company."""
    data = {
        "updated_at": utc_now(),
        "completed_companies": sorted(completed_companies),
        "completed_count": len(completed_companies),
    }

    temp_path = PROGRESS_JSON.with_suffix(".tmp")

    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    temp_path.replace(PROGRESS_JSON)


def ensure_csv_header(path, fieldnames):
    """Create an output CSV and its header if needed."""
    if not path.exists() or path.stat().st_size == 0:
        with path.open(
            "w",
            encoding="utf-8-sig",
            newline=""
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=fieldnames
            )
            writer.writeheader()


def append_csv(path, fieldnames, row):
    """Append one row to a CSV file."""
    ensure_csv_header(path, fieldnames)

    with path.open(
        "a",
        encoding="utf-8-sig",
        newline=""
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="ignore"
        )
        writer.writerow(row)


def append_jsonl(path, row):
    """Append one JSON object per line."""
    with path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(row, ensure_ascii=False) + "\n"
        )


# ============================================================
# REVENUE CANDIDATE DETECTION
# ============================================================

AMOUNT_PATTERN = re.compile(
    r"(?:"
    r"US\$|USD\s*|[$€£₹]\s*"
    r")"
    r"\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:billion|million|trillion|bn|mn|m|k|crore|lakh))?"
    r"|"
    r"\b\d[\d,]*(?:\.\d+)?\s*"
    r"(?:billion|million|trillion|bn|mn|crore|lakh)\b",
    re.IGNORECASE,
)

REVENUE_PATTERN = re.compile(
    r"\b("
    r"revenue|revenues|annual sales|net sales|turnover"
    r")\b",
    re.IGNORECASE,
)

EXCLUSION_PATTERN = re.compile(
    r"\b("
    r"department of revenue|"
    r"board of revenue|"
    r"internal revenue code|"
    r"state revenue|"
    r"gaming terminal revenue"
    r")\b",
    re.IGNORECASE,
)


def is_possible_candidate(company, title, snippet):
    """
    Return True only when a result contains:
      1. The company name.
      2. Revenue-related wording.
      3. A monetary amount or amount with a magnitude.

    This is a rough filter, not verification of a revenue figure.
    """
    text = f"{title or ''} {snippet or ''}"
    normalized_text = normalize_text(text)
    normalized_company = normalize_text(company)

    if not normalized_company:
        return False

    # Require the full normalized company name to appear.
    # This can miss legitimate results for abbreviations or aliases.
    if normalized_company not in normalized_text:
        return False

    if not REVENUE_PATTERN.search(text):
        return False

    if EXCLUSION_PATTERN.search(text):
        return False

    if not AMOUNT_PATTERN.search(text):
        return False

    return True


# ============================================================
# SEARCH QUERIES
# ============================================================

def build_queries(company):
    """
    Return query variants.

    The default performs one search per company.
    Optional variants can improve coverage but make more requests.
    """
    if USE_QUERY_VARIANTS:
        return [
            f'"{company}" revenue',
            f'"{company}" annual revenue USD',
            f'"{company}" annual report revenue',
        ]

    return [f'"{company}" revenue']


# ============================================================
# PURILI SEARCH
# ============================================================

def search_purili(query, page=1):
    """
    Search Purili's public search endpoint.

    Only returned search-result metadata is used.
    This function does not open any result URLs.
    """
    params = {
        "q": query,
        "page": page,
        "exact": 1,
    }

    try:
        response = session.get(
            SEARCH_ENDPOINT,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        response.raise_for_status()

        try:
            data = response.json()
        except ValueError:
            print("Warning: Search response was not valid JSON.")
            return []

        results = data.get("results", [])

        if not isinstance(results, list):
            return []

        return results

    except requests.Timeout:
        print(f"Timeout while searching: {query}")
        return []

    except requests.RequestException as error:
        print(f"Request error for query '{query}': {error}")
        return []


# ============================================================
# PROCESS ONE COMPANY
# ============================================================

SEARCH_FIELDS = [
    "company_name",
    "query",
    "page",
    "title",
    "snippet",
    "url",
    "is_possible_revenue_candidate",
    "searched_at",
]

CANDIDATE_FIELDS = [
    "company_name",
    "query",
    "title",
    "snippet",
    "url",
    "searched_at",
    "review_status",
]


def process_company(company):
    """
    Search a company and save all returned search-result metadata.

    Returns the number of results saved.
    """
    saved_count = 0
    queries = build_queries(company)
    searched_at = utc_now()

    for query in queries:
        for page in range(1, PAGES_PER_COMPANY + 1):
            results = search_purili(query, page=page)

            for result in results:
                if not isinstance(result, dict):
                    continue

                # Use only metadata from the search API.
                title = str(result.get("title") or "").strip()
                snippet = str(
                    result.get("description")
                    or result.get("snippet")
                    or ""
                ).strip()
                url = str(result.get("url") or "").strip()

                candidate = is_possible_candidate(
                    company,
                    title,
                    snippet
                )

                row = {
                    "company_name": company,
                    "query": query,
                    "page": page,
                    "title": title,
                    "snippet": snippet,
                    "url": url,
                    "is_possible_revenue_candidate": candidate,
                    "searched_at": searched_at,
                }

                append_csv(
                    RESULTS_CSV,
                    SEARCH_FIELDS,
                    row
                )

                append_jsonl(RESULTS_JSONL, row)

                if candidate:
                    candidate_row = {
                        "company_name": company,
                        "query": query,
                        "title": title,
                        "snippet": snippet,
                        "url": url,
                        "searched_at": searched_at,
                        "review_status": "Needs manual verification",
                    }

                    append_csv(
                        CANDIDATES_CSV,
                        CANDIDATE_FIELDS,
                        candidate_row
                    )

                saved_count += 1

            # Pause between API requests.
            time.sleep(REQUEST_DELAY_SECONDS)

    return saved_count


# ============================================================
# MAIN
# ============================================================

def main():
    companies = load_company_names()

    if MAX_COMPANIES is not None:
        companies = companies[:MAX_COMPANIES]

    completed = load_progress()

    # Avoid searching completed companies again.
    remaining = [
        company
        for company in companies
        if company not in completed
    ]

    print("=" * 65)
    print("PURILI REVENUE CANDIDATE SCRAPER")
    print("=" * 65)
    print(f"Input file:       {INPUT_CSV.resolve()}")
    print(f"Companies loaded: {len(companies)}")
    print(f"Already complete: {len(completed)}")
    print(f"Remaining:        {len(remaining)}")
    print(f"Output folder:    {OUTPUT_DIR.resolve()}")
    print()
    print("This script reads search-result metadata only.")
    print("It does not open the returned company/source URLs.")
    print()

    if not remaining:
        print("No remaining companies to process.")
        return

    for index, company in enumerate(remaining, start=1):
        print(
            f"[{index}/{len(remaining)}] Searching: {company}"
        )

        try:
            result_count = process_company(company)

            print(
                f"    Saved {result_count} search results."
            )

            # Mark complete after processing.
            completed.add(company)
            save_progress(completed)

        except KeyboardInterrupt:
            print("\nStopped by user. Progress has been saved.")
            save_progress(completed)
            break

        except Exception as error:
            # Leave the company uncompleted so it can be retried.
            print(
                f"    Unexpected error for '{company}': {error}"
            )
            print("    It will be eligible for retry next run.")

    print()
    print("=" * 65)
    print("FINISHED")
    print("=" * 65)
    print(f"Search results CSV: {RESULTS_CSV.resolve()}")
    print(f"Search results JSONL: {RESULTS_JSONL.resolve()}")
    print(f"Revenue candidates: {CANDIDATES_CSV.resolve()}")
    print(f"Progress file: {PROGRESS_JSON.resolve()}")
    print()
    print(
        "Candidate rows are leads for manual review, "
        "not verified revenue figures."
    )


if __name__ == "__main__":
    main()
