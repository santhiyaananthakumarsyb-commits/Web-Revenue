#!/usr/bin/env python3
"""Extract revenue candidates from cached DuckDuckGo JSON. Standard library only."""

import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "revenue_search_output"
OUTPUT_DIR = BASE_DIR / "revenue_extracted"

MAX_SOURCES = 10
INITIAL_SOURCES = 3

REVENUE_RE = re.compile(
    r"\b(revenue|annual sales|yearly sales|sales revenue|turnover)\b",
    re.I,
)
QUARTER_RE = re.compile(
    r"\b(quarterly|per quarter|for the quarter|third quarter|fourth quarter|"
    r"first quarter|second quarter|Q[1-4]\s*20\d{2})\b",
    re.I,
)
ANNUAL_RE = re.compile(
    r"\b(annual|annually|yearly|per year|fiscal year|FY\s*20\d{2}|"
    r"year ended|year ending)\b",
    re.I,
)
BAD_RE = re.compile(
    r"\b(valuation|valued at|market cap|funding|raised|investment|"
    r"doc(?:ument)? fee|service fee|monthly fee|hourly rate|salary|"
    r"listing price|asking price|app revenue|in-app|arpu|employees?|headcount)\b",
    re.I,
)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

RANGE_RE = re.compile(
    r"(?P<cur1>US\$|C\$|A\$|USD|CAD|AUD|EUR|GBP|INR|[$€£₹¥])?\s*"
    r"(?P<n1>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<m1>trillion|billion|million|thousand|[TBMK])?\s*"
    r"(?:-|–|—|\bto\b)\s*"
    r"(?P<cur2>US\$|C\$|A\$|USD|CAD|AUD|EUR|GBP|INR|[$€£₹¥])?\s*"
    r"(?P<n2>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<m2>trillion|billion|million|thousand|[TBMK])?",
    re.I,
)
SINGLE_RE = re.compile(
    r"(?P<cur>US\$|C\$|A\$|USD|CAD|AUD|EUR|GBP|INR|[$€£₹¥])?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<mag>trillion|billion|million|thousand|[TBMK])?",
    re.I,
)


def normalize_url(url):
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        parsed = urlsplit(url.strip())
        if not parsed.scheme or not parsed.netloc:
            return url.strip()
        return urlunsplit((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.query,
            "",
        ))
    except Exception:
        return url.strip()


def read_json(path):
    try:
        with path.open(encoding="utf-8-sig") as file:
            return json.load(file)
    except Exception as error:
        print(f"WARNING: Could not read {path}: {error}", file=sys.stderr)
        return None


def get_sources(data):
    """Combine searches[].results and deduplicate by URL."""
    raw = []
    searches = data.get("searches", [])

    if isinstance(searches, list):
        for search in searches:
            if not isinstance(search, dict):
                continue

            results = search.get("results", [])
            if not isinstance(results, list):
                continue

            for result in results:
                if not isinstance(result, dict):
                    continue

                raw.append({
                    "title": str(result.get("title") or ""),
                    "url": str(result.get("url") or ""),
                    "snippet": str(result.get("snippet") or ""),
                    "query": str(
                        result.get("query") or search.get("query") or ""
                    ),
                })

    # Use flattened results only if searches[].results is empty.
    if not raw and isinstance(data.get("results"), list):
        for result in data["results"]:
            if isinstance(result, dict):
                raw.append({
                    "title": str(result.get("title") or ""),
                    "url": str(result.get("url") or ""),
                    "snippet": str(result.get("snippet") or ""),
                    "query": str(result.get("query") or ""),
                })

    unique = []
    indexed = {}

    for source in raw:
        key = normalize_url(source["url"])
        if not key:
            key = "NOURL:" + source["title"] + ":" + source["snippet"]

        if key not in indexed:
            item = {
                "title": source["title"],
                "url": source["url"],
                "snippet": source["snippet"],
                "queries": [],
            }
            indexed[key] = item
            unique.append(item)

        item = indexed[key]

        if source["query"] and source["query"] not in item["queries"]:
            item["queries"].append(source["query"])

        if len(source["snippet"]) > len(item["snippet"]):
            item["snippet"] = source["snippet"]

    return unique


def parse_number(number, magnitude):
    try:
        value = float(number.replace(",", ""))
    except (ValueError, AttributeError):
        return None

    multiplier = {
        "k": 1_000,
        "thousand": 1_000,
        "m": 1_000_000,
        "million": 1_000_000,
        "b": 1_000_000_000,
        "billion": 1_000_000_000,
        "t": 1_000_000_000_000,
        "trillion": 1_000_000_000_000,
    }.get((magnitude or "").lower(), 1)

    return value * multiplier


def detect_currency(text, token):
    token = (token or "").upper()

    known = {
        "USD": "USD",
        "CAD": "CAD",
        "AUD": "AUD",
        "EUR": "EUR",
        "GBP": "GBP",
        "INR": "INR",
        "US$": "USD",
        "C$": "CAD",
        "A$": "AUD",
        "€": "EUR",
        "£": "GBP",
        "₹": "INR",
        "¥": "JPY_OR_CNY",
    }

    if token in known:
        return known[token]

    if "$" in token or re.search(r"(?<![A-Za-z])\$", text):
        return "UNKNOWN_DOLLAR_CURRENCY"

    for pattern, currency in [
        (r"\bUSD\b", "USD"),
        (r"\bCAD\b", "CAD"),
        (r"\bAUD\b", "AUD"),
        (r"\bEUR\b|€", "EUR"),
        (r"\bGBP\b|£", "GBP"),
        (r"\bINR\b|₹", "INR"),
        (r"\bJPY\b", "JPY"),
    ]:
        if re.search(pattern, text, re.I):
            return currency

    return "UNKNOWN"


def detect_year(text):
    patterns = [
        r"\b(?:FY|fiscal year|year ended|year ending|for|in)\s*(19\d{2}|20\d{2})\b",
        r"\b(19\d{2}|20\d{2})\s+(?:annual )?revenue\b",
        r"\brevenue\s+(?:in|for|during)\s+(19\d{2}|20\d{2})\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)

    years = sorted(set(YEAR_RE.findall(text)))
    return years[0] if len(years) == 1 else ""


def detect_period(text):
    if QUARTER_RE.search(text):
        return "quarterly"
    if ANNUAL_RE.search(text):
        return "annual"
    return "unknown"


def make_candidate(company, source, rank, matched, low, high, kind, context):
    currency_match = re.search(
        r"US\$|C\$|A\$|USD|CAD|AUD|EUR|GBP|INR|[$€£₹¥]",
        matched,
        re.I,
    )
    currency = detect_currency(
        context,
        currency_match.group(0) if currency_match else "",
    )

    period = detect_period(context)
    year = detect_year(context)

    known_annual_value = (
        kind == "single"
        and period == "annual"
        and bool(year)
        and currency not in ("UNKNOWN", "UNKNOWN_DOLLAR_CURRENCY")
    )

    return {
        "company_name": company,
        "source_rank": rank,
        "source_title": source["title"],
        "source_url": source["url"],
        "source_queries": " || ".join(source["queries"]),
        "matched_text": matched,
        "amount_low": round(low, 2),
        "amount_high": round(high, 2),
        "amount_type": kind,
        "currency": currency,
        "reporting_year": year,
        "period": period,
        "status": "CANDIDATE_NEEDS_VERIFICATION",
        "confidence": "medium" if known_annual_value else "low",
        "evidence_snippet": source["snippet"],
    }


def parse_source(company, source, rank):
    text = source["title"] + ". " + source["snippet"]

    # Require explicit revenue-related wording.
    if not REVENUE_RE.search(text):
        return []

    candidates = []
    range_spans = []

    # Find ranges first, such as "$1M to $5M".
    for match in RANGE_RE.finditer(text):
        context = text[max(0, match.start() - 100):
                       min(len(text), match.end() + 100)]

        if BAD_RE.search(context) or QUARTER_RE.search(context):
            continue

        low = parse_number(match.group("n1"), match.group("m1"))
        high = parse_number(
            match.group("n2"),
            match.group("m2") or match.group("m1"),
        )

        if low is None or high is None:
            continue

        low, high = min(low, high), max(low, high)

        candidates.append(make_candidate(
            company, source, rank, match.group(0),
            low, high, "range", context,
        ))
        range_spans.append((match.start(), match.end()))

    # Find single values, skipping values already captured in ranges.
    for match in SINGLE_RE.finditer(text):
        if any(start <= match.start() < end for start, end in range_spans):
            continue

        context = text[max(0, match.start() - 100):
                       min(len(text), match.end() + 100)]

        # Prevent common false positives such as dealership fees.
        if BAD_RE.search(context) or QUARTER_RE.search(context):
            continue

        if not REVENUE_RE.search(context):
            continue

        value = parse_number(match.group("num"), match.group("mag"))
        if value is None:
            continue

        candidates.append(make_candidate(
            company, source, rank, match.group(0),
            value, value, "single", context,
        ))

    return candidates


def extract_company(company, sources):
    """Check the first 3 sources, then extend up to 10 if needed."""
    candidates = []
    checked = 0

    for rank, source in enumerate(sources[:INITIAL_SOURCES], start=1):
        checked += 1
        candidates.extend(parse_source(company, source, rank))

    if len(candidates) < 3:
        for rank, source in enumerate(
            sources[INITIAL_SOURCES:MAX_SOURCES],
            start=INITIAL_SOURCES + 1,
        ):
            checked += 1
            candidates.extend(parse_source(company, source, rank))

            if len(candidates) >= 3:
                break

    return candidates, min(checked, MAX_SOURCES)


def calculate_summary(candidates):
    """Only aggregate comparable annual figures with known currency and year."""
    eligible = [
        candidate for candidate in candidates
        if candidate["amount_type"] == "single"
        and candidate["period"] == "annual"
        and candidate["reporting_year"]
        and candidate["currency"] not in (
            "UNKNOWN", "UNKNOWN_DOLLAR_CURRENCY", ""
        )
    ]

    groups = {}

    for candidate in eligible:
        key = (candidate["currency"], candidate["reporting_year"])
        groups.setdefault(key, []).append(candidate)

    if not groups:
        return {
            "revenue_amount": "",
            "currency": "",
            "reporting_year": "",
            "minimum": "",
            "maximum": "",
            "average": "",
            "source_urls": "",
            "eligible_source_count": 0,
            "status": "NO_COMPARABLE_ANNUAL_DATA",
            "confidence": "low",
        }

    key, group = max(
        groups.items(),
        key=lambda item: (
            len(set(c["source_url"] for c in item[1])),
            item[0][1],
        ),
    )

    values = [candidate["amount_low"] for candidate in group]
    urls = sorted(set(
        candidate["source_url"]
        for candidate in group
        if candidate["source_url"]
    ))

    revenue_amount = values[0] if len(set(values)) == 1 else ""

    if len(urls) >= 2:
        minimum = min(values)
        maximum = max(values)
        average = round(sum(values) / len(values), 2)
    else:
        minimum = maximum = average = ""

    return {
        "revenue_amount": revenue_amount,
        "currency": key[0],
        "reporting_year": key[1],
        "minimum": minimum,
        "maximum": maximum,
        "average": average,
        "source_urls": " | ".join(urls),
        "eligible_source_count": len(group),
        "status": (
            "ANNUAL_DATA_CANDIDATE"
            if len(urls) >= 2
            else "SINGLE_SOURCE_NEEDS_VERIFICATION"
        ),
        "confidence": "medium" if len(urls) >= 2 else "low",
    }


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def main():
    if not CACHE_DIR.exists():
        print(f"Cache folder not found: {CACHE_DIR}")
        print("Place this script beside the revenue_search_output folder.")
        return 2

    cache_files = sorted(
        path for path in CACHE_DIR.rglob("*.json") if path.is_file()
    )

    if not cache_files:
        print(f"No JSON cache files found under {CACHE_DIR}")
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_candidates = []
    summaries = []
    sources_checked = []

    for path in cache_files:
        data = read_json(path)
        if not isinstance(data, dict):
            continue

        company = str(
            data.get("company_name")
            or data.get("company")
            or path.stem
        )
        company_key = str(data.get("company_key") or company.lower())
        sources = get_sources(data)

        candidates, checked_count = extract_company(company, sources)

        for candidate in candidates:
            candidate["company_key"] = company_key
            candidate["cache_file"] = str(path.relative_to(BASE_DIR))
            all_candidates.append(candidate)

        for rank, source in enumerate(sources[:checked_count], start=1):
            sources_checked.append({
                "company_key": company_key,
                "company_name": company,
                "source_rank": rank,
                "source_title": source["title"],
                "source_url": source["url"],
                "source_queries": " || ".join(source["queries"]),
                "evidence_snippet": source["snippet"],
                "cache_file": str(path.relative_to(BASE_DIR)),
            })

        stats = calculate_summary(candidates)
        searches = data.get("searches", [])
        search_statuses = [
            str(search.get("status", ""))
            for search in searches
            if isinstance(search, dict)
        ] if isinstance(searches, list) else []

        if not sources:
            status = (
                "NO_RESULTS_PARSED"
                if "NO_RESULTS_PARSED" in search_statuses
                else "NO_SEARCH_RESULTS"
            )
        elif not candidates:
            status = "SEARCH_RESULTS_NO_REVENUE_CANDIDATE"
        else:
            status = stats["status"]

        summaries.append({
            "company_key": company_key,
            "company_name": company,
            "company_website": data.get("company_website", ""),
            **stats,
            "unique_cached_source_count": len(sources),
            "candidate_count": len(candidates),
            "search_status": " | ".join(sorted(set(search_statuses))),
            "status": status,
            "cache_file": str(path.relative_to(BASE_DIR)),
        })

    candidate_fields = [
        "company_key", "company_name", "source_rank", "source_title",
        "source_url", "source_queries", "matched_text", "amount_low",
        "amount_high", "amount_type", "currency", "reporting_year",
        "period", "status", "confidence", "evidence_snippet", "cache_file",
    ]

    summary_fields = [
        "company_key", "company_name", "company_website", "revenue_amount",
        "currency", "reporting_year", "minimum", "maximum", "average",
        "source_urls", "eligible_source_count", "unique_cached_source_count",
        "candidate_count", "search_status", "status", "confidence",
        "cache_file",
    ]

    audit_fields = [
        "company_key", "company_name", "source_rank", "source_title",
        "source_url", "source_queries", "evidence_snippet", "cache_file",
    ]

    write_csv(
        OUTPUT_DIR / "revenue_candidates.csv",
        all_candidates, candidate_fields,
    )
    write_csv(
        OUTPUT_DIR / "revenue_summary.csv",
        summaries, summary_fields,
    )
    write_csv(
        OUTPUT_DIR / "sources_checked.csv",
        sources_checked, audit_fields,
    )

    (OUTPUT_DIR / "revenue_candidates.json").write_text(
        json.dumps(all_candidates, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "revenue_summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Processed {len(cache_files)} cache JSON files.")
    print(f"Companies summarized: {len(summaries)}")
    print(f"Revenue candidates found: {len(all_candidates)}")
    print(f"Outputs saved to: {OUTPUT_DIR}")
    print("Existing cache files were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())