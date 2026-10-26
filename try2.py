
#!/usr/bin/env python3
"""Extract and rank company revenue and employee estimates from cached JSON.

Standard library only.
Input:  revenue_search_output/**/*.json
Output: revenue_extracted/*.csv and *.json
Cached input files are never modified.
"""

import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "revenue_search_output"
OUTPUT_DIR = BASE_DIR / "revenue_extracted"

# None means evaluate every cached source.
MAX_SOURCES = None

# ---------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------

REVENUE_RE = re.compile(
    r"\b(revenue|annual sales|yearly sales|sales revenue|turnover)\b",
    re.I,
)

ANNUAL_RE = re.compile(
    r"\b(annual|annually|yearly|per year|fiscal year|FY\s*20\d{2}|"
    r"year ended|year ending)\b",
    re.I,
)

QUARTER_RE = re.compile(
    r"\b(quarterly|per quarter|for the quarter|first quarter|"
    r"second quarter|third quarter|fourth quarter|Q[1-4]\s*20\d{2})\b",
    re.I,
)

BAD_RE = re.compile(
    r"\b(valuation|valued at|market cap|funding|raised|investment|"
    r"document fee|doc fee|service fee|monthly fee|hourly rate|"
    r"salary|listing price|asking price|app revenue|in-app|arpu)\b",
    re.I,
)

YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
CURRENCY = r"US\$|C\$|A\$|USD|CAD|AUD|EUR|GBP|INR|[$€£₹¥]"
MAG = r"trillion|billion|million|thousand|[TBMK]"

RANGE_RE = re.compile(
    rf"(?P<cur1>{CURRENCY})?\s*"
    rf"(?P<n1>{NUMBER})\s*(?P<m1>{MAG})?\s*"
    rf"(?:-|–|—|\bto\b)\s*"
    rf"(?P<cur2>{CURRENCY})?\s*"
    rf"(?P<n2>{NUMBER})\s*(?P<m2>{MAG})?",
    re.I,
)

SINGLE_RE = re.compile(
    rf"(?P<cur>{CURRENCY})?\s*"
    rf"(?P<num>{NUMBER})\s*(?P<mag>{MAG})?",
    re.I,
)

EMP_RANGE_RE = re.compile(
    r"\b(?:employs?\s+)?"
    r"(?:approximately\s+|about\s+|around\s+)?"
    r"(?P<low>\d[\d,]*)\s*(?:to|-|–|—)\s*"
    r"(?P<high>\d[\d,]*)\s*"
    r"(?P<label>employees?|people|staff)\b",
    re.I,
)

EMP_SINGLE_RE = re.compile(
    r"\b(?P<count>\d[\d,]*)\s+"
    r"(?P<label>total\s+employees?|employees?|people|staff)\b",
    re.I,
)

# Initial source quality scores. These are heuristics, not guarantees.
SOURCE_WEIGHTS = {
    "sec.gov": 100,
    "annualreports.com": 90,
    "dnb.com": 80,
    "zoominfo.com": 65,
    "rocketreach.co": 60,
    "datanyze.com": 55,
    "buzzfile.com": 55,
    "growjo.com": 50,
    "compworth.com": 45,
}


# ---------------------------------------------------------------------
# General utilities
# ---------------------------------------------------------------------

def normalize_url(url):
    if not isinstance(url, str) or not url.strip():
        return ""

    try:
        p = urlsplit(url.strip())
        if not p.scheme or not p.netloc:
            return url.strip()

        return urlunsplit((
            p.scheme.lower(),
            p.netloc.lower(),
            p.path.rstrip("/") or "/",
            p.query,
            "",
        ))
    except Exception:
        return url.strip()


def read_json(path):
    try:
        with path.open(encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as exc:
        print(f"WARNING: Could not read {path}: {exc}", file=sys.stderr)
        return None


def write_json(path, rows):
    path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows({
            field: row.get(field, "")
            for field in fields
        } for row in rows)


def parse_number(number, magnitude=""):
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


def detect_currency(text, token=""):
    token = (token or "").upper()

    known = {
        "USD": "USD", "US$": "USD",
        "CAD": "CAD", "C$": "CAD",
        "AUD": "AUD", "A$": "AUD",
        "EUR": "EUR", "€": "EUR",
        "GBP": "GBP", "£": "GBP",
        "INR": "INR", "₹": "INR",
        "¥": "JPY_OR_CNY",
    }

    if token in known:
        return known[token]

    if "$" in token or re.search(r"(?<![A-Za-z])\$", text):
        return "UNKNOWN_DOLLAR_CURRENCY"

    for pattern, currency in (
        (r"\bUSD\b", "USD"),
        (r"\bCAD\b", "CAD"),
        (r"\bAUD\b", "AUD"),
        (r"\bEUR\b|€", "EUR"),
        (r"\bGBP\b|£", "GBP"),
        (r"\bINR\b|₹", "INR"),
        (r"\bJPY\b", "JPY"),
    ):
        if re.search(pattern, text, re.I):
            return currency

    return "UNKNOWN"


def detect_year(text):
    patterns = (
        r"\b(?:FY|fiscal year|year ended|year ending|for|in)\s*"
        r"(19\d{2}|20\d{2})\b",
        r"\b(19\d{2}|20\d{2})\s+(?:annual )?revenue\b",
        r"\brevenue\s+(?:in|for|during)\s*(19\d{2}|20\d{2})\b",
    )

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


def source_score(url, official_domain=""):
    """Score source quality; a matching official domain gets priority."""
    host = urlsplit(url or "").netloc.lower().split(":")[0]
    host = host.removeprefix("www.")

    official_host = urlsplit(official_domain or "").netloc.lower()
    official_host = official_host.removeprefix("www.")

    if official_host and (
        host == official_host or host.endswith("." + official_host)
    ):
        return 90

    for domain, score in SOURCE_WEIGHTS.items():
        if host == domain or host.endswith("." + domain):
            return score

    return 30


def get_sources(data):
    """Merge nested search results and deduplicate URLs."""
    raw = []
    searches = data.get("searches", [])

    if isinstance(searches, list):
        for search in searches:
            if not isinstance(search, dict):
                continue

            for result in search.get("results", []):
                if isinstance(result, dict):
                    raw.append({
                        "title": str(result.get("title") or ""),
                        "url": str(result.get("url") or ""),
                        "snippet": str(result.get("snippet") or ""),
                        "query": str(
                            result.get("query") or search.get("query") or ""
                        ),
                    })

    if not raw and isinstance(data.get("results"), list):
        for result in data["results"]:
            if isinstance(result, dict):
                raw.append({
                    "title": str(result.get("title") or ""),
                    "url": str(result.get("url") or ""),
                    "snippet": str(result.get("snippet") or ""),
                    "query": str(result.get("query") or ""),
                })

    indexed = {}
    unique = []

    for item in raw:
        key = normalize_url(item["url"]) or (
            "NOURL:" + item["title"] + ":" + item["snippet"]
        )

        if key not in indexed:
            indexed[key] = {
                "title": item["title"],
                "url": item["url"],
                "snippet": item["snippet"],
                "queries": [],
            }
            unique.append(indexed[key])

        existing = indexed[key]

        if item["query"] and item["query"] not in existing["queries"]:
            existing["queries"].append(item["query"])

        if len(item["snippet"]) > len(existing["snippet"]):
            existing["snippet"] = item["snippet"]

    return unique


def rank_sources(sources, official_domain=""):
    """Prioritize quality scores, then informative snippets."""
    return sorted(
        sources,
        key=lambda s: (
            source_score(s["url"], official_domain),
            bool(s["snippet"]),
            len(s["snippet"]),
        ),
        reverse=True,
    )


# ---------------------------------------------------------------------
# Revenue extraction
# ---------------------------------------------------------------------

def employee_spans(text):
    spans = [m.span() for m in EMP_RANGE_RE.finditer(text)]
    spans += [m.span() for m in EMP_SINGLE_RE.finditer(text)]
    return spans


def overlaps(start, end, spans):
    return any(start < b and end > a for a, b in spans)


def revenue_candidate(company, source, rank, official_domain,
                      matched, low, high, kind, context):
    currency_match = re.search(CURRENCY, matched, re.I)
    currency = detect_currency(
        context,
        currency_match.group(0) if currency_match else "",
    )

    period = detect_period(context)
    year = detect_year(context)

    # Heuristic score to help rank candidates.
    score = source_score(source["url"], official_domain)
    if period == "annual":
        score += 10
    if year:
        score += 5
    if currency not in ("UNKNOWN", "UNKNOWN_DOLLAR_CURRENCY"):
        score += 5
    if kind == "single":
        score += 3

    return {
        "company_name": company,
        "source_rank": rank,
        "source_score": source_score(source["url"], official_domain),
        "candidate_score": score,
        "source_title": source["title"],
        "source_url": source["url"],
        "source_queries": " || ".join(source["queries"]),
        "matched_text": matched.strip(),
        "amount_low": round(low, 2),
        "amount_high": round(high, 2),
        "amount_type": kind,
        "currency": currency,
        "reporting_year": year,
        "period": period,
        "status": "CANDIDATE_NEEDS_VERIFICATION",
        "evidence_snippet": source["snippet"],
    }


def parse_revenue(company, source, rank, official_domain=""):
    text = source["title"] + ". " + source["snippet"]

    if not REVENUE_RE.search(text):
        return []

    found = []
    seen = set()
    employee_ranges = employee_spans(text)

    for keyword in REVENUE_RE.finditer(text):
        left = max(0, keyword.start() - 120)
        right = min(len(text), keyword.end() + 150)
        context = text[left:right]

        if BAD_RE.search(context) or QUARTER_RE.search(context):
            continue

        ranges = list(RANGE_RE.finditer(context))
        range_spans = []

        for match in ranges:
            a, b = left + match.start(), left + match.end()

            if overlaps(a, b, employee_ranges):
                continue

            low = parse_number(match.group("n1"), match.group("m1"))
            high = parse_number(
                match.group("n2"),
                match.group("m2") or match.group("m1"),
            )

            if low is None or high is None or max(low, high) < 1000:
                continue

            c1, c2 = match.group("cur1") or "", match.group("cur2") or ""
            if c1 and c2 and detect_currency(context, c1) != detect_currency(
                context, c2
            ):
                continue

            candidate = revenue_candidate(
                company, source, rank, official_domain,
                match.group(0), min(low, high), max(low, high),
                "range", context,
            )

            key = (
                candidate["source_url"],
                candidate["amount_low"],
                candidate["amount_high"],
                candidate["amount_type"],
            )

            if key not in seen:
                found.append(candidate)
                seen.add(key)

            range_spans.append(match.span())

        for match in SINGLE_RE.finditer(context):
            a, b = left + match.start(), left + match.end()

            if overlaps(a, b, employee_ranges):
                continue
            if overlaps(match.start(), match.end(), range_spans):
                continue

            value = parse_number(match.group("num"), match.group("mag"))
            if value is None or value < 1000:
                continue

            # Keep values reasonably close to a revenue keyword.
            distance = min(
                abs(a - keyword.start()),
                abs(b - keyword.end()),
            )
            if distance > 120:
                continue

            candidate = revenue_candidate(
                company, source, rank, official_domain,
                match.group(0), value, value, "single", context,
            )

            key = (
                candidate["source_url"],
                candidate["amount_low"],
                candidate["amount_high"],
                candidate["amount_type"],
            )

            if key not in seen:
                found.append(candidate)
                seen.add(key)

    return found


# ---------------------------------------------------------------------
# Employee extraction
# ---------------------------------------------------------------------

def parse_employees(company, source, rank, official_domain=""):
    text = source["title"] + ". " + source["snippet"]
    results = []
    range_spans = []

    def add(match_text, low, high, kind, location):
        results.append({
            "company_name": company,
            "source_rank": rank,
            "source_score": source_score(
                source["url"], official_domain
            ),
            "source_title": source["title"],
            "source_url": source["url"],
            "source_queries": " || ".join(source["queries"]),
            "matched_text": match_text.strip(),
            "employee_count_low": int(low),
            "employee_count_high": int(high),
            "employee_count_type": kind,
            "location_type": location,
            "status": "CANDIDATE_NEEDS_VERIFICATION",
            "evidence_snippet": source["snippet"],
        })

    for match in EMP_RANGE_RE.finditer(text):
        low = int(match.group("low").replace(",", ""))
        high = int(match.group("high").replace(",", ""))

        if low > high:
            low, high = high, low

        after = text[match.end():match.end() + 120]
        location = (
            "location"
            if re.search(r"\bat this location\b", after, re.I)
            else "unspecified"
        )

        add(match.group(0), low, high, "range", location)
        range_spans.append(match.span())

    for match in EMP_SINGLE_RE.finditer(text):
        if overlaps(*match.span(), range_spans):
            continue

        count = int(match.group("count").replace(",", ""))
        after = text[match.end():match.end() + 140]
        label = match.group("label").lower()

        if "total employee" in label or re.search(
            r"\bat all locations\b", after, re.I
        ):
            location = "company_total"
        elif re.search(r"\bat this location\b", after, re.I):
            location = "location"
        else:
            location = "unspecified"

        add(match.group(0), count, count, "single", location)

    unique = []
    seen = set()

    for item in results:
        key = (
            item["source_url"],
            item["employee_count_low"],
            item["employee_count_high"],
            item["location_type"],
        )
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


# ---------------------------------------------------------------------
# Candidate selection and summaries
# ---------------------------------------------------------------------

def select_revenue(candidates):
    """Select the best-supported candidate; retain all alternatives."""
    if not candidates:
        return {
            "selected_revenue_low": "",
            "selected_revenue_high": "",
            "selected_revenue_currency": "",
            "selected_revenue_year": "",
            "selected_revenue_period": "",
            "selected_revenue_source": "",
            "selected_revenue_reason": "No revenue candidate found",
        }

    ranked = sorted(
        candidates,
        key=lambda c: (
            c["candidate_score"],
            bool(c["reporting_year"]),
            c["source_score"],
        ),
        reverse=True,
    )
    best = ranked[0]

    reasons = [
        f"source score {best['source_score']}",
        f"candidate score {best['candidate_score']}",
    ]
    if best["period"] == "annual":
        reasons.append("annual wording detected")
    if best["reporting_year"]:
        reasons.append("reporting year detected")

    return {
        "selected_revenue_low": best["amount_low"],
        "selected_revenue_high": best["amount_high"],
        "selected_revenue_currency": best["currency"],
        "selected_revenue_year": best["reporting_year"],
        "selected_revenue_period": best["period"],
        "selected_revenue_source": best["source_url"],
        "selected_revenue_reason": "; ".join(reasons)
            + "; verify entity and reporting period",
    }


def select_employees(candidates):
    """Prefer company-wide counts, then location-specific counts."""
    if not candidates:
        return {
            "selected_employee_low": "",
            "selected_employee_high": "",
            "selected_employee_scope": "",
            "selected_employee_source": "",
        }

    scope_priority = {
        "company_total": 3,
        "unspecified": 2,
        "location": 1,
    }

    ranked = sorted(
        candidates,
        key=lambda c: (
            scope_priority.get(c["location_type"], 0),
            c["source_score"],
            c["employee_count_low"] == c["employee_count_high"],
        ),
        reverse=True,
    )
    best = ranked[0]

    return {
        "selected_employee_low": best["employee_count_low"],
        "selected_employee_high": best["employee_count_high"],
        "selected_employee_scope": best["location_type"],
        "selected_employee_source": best["source_url"],
    }


def extract_company(company, sources, official_domain=""):
    ranked = rank_sources(sources, official_domain)

    if MAX_SOURCES is not None:
        ranked = ranked[:MAX_SOURCES]

    revenue = []
    employees = []
    audited_sources = []

    for rank, source in enumerate(ranked, start=1):
        revenue.extend(
            parse_revenue(company, source, rank, official_domain)
        )
        employees.extend(
            parse_employees(company, source, rank, official_domain)
        )
        audited_sources.append({
            **source,
            "source_rank": rank,
            "source_score": source_score(
                source["url"], official_domain
            ),
        })

    return revenue, employees, audited_sources


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    if not CACHE_DIR.exists():
        print(f"Cache folder not found: {CACHE_DIR}")
        print("Place this script beside revenue_search_output.")
        return 2

    files = sorted(CACHE_DIR.rglob("*.json"))
    if not files:
        print(f"No JSON files found under {CACHE_DIR}")
        return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_revenue = []
    all_employees = []
    summaries = []
    audits = []

    for path in files:
        data = read_json(path)
        if not isinstance(data, dict):
            continue

        company = str(
            data.get("company_name")
            or data.get("company")
            or path.stem
        )
        company_key = str(
            data.get("company_key") or company.lower()
        )
        official_domain = str(data.get("company_website") or "")

        sources = get_sources(data)

        revenue, employees, audited = extract_company(
            company, sources, official_domain
        )

        relative_path = str(path.relative_to(BASE_DIR))

        for candidate in revenue:
            candidate["company_key"] = company_key
            candidate["cache_file"] = relative_path
            all_revenue.append(candidate)

        for candidate in employees:
            candidate["company_key"] = company_key
            candidate["cache_file"] = relative_path
            all_employees.append(candidate)

        for source in audited:
            audits.append({
                "company_key": company_key,
                "company_name": company,
                "source_rank": source["source_rank"],
                "source_score": source["source_score"],
                "source_title": source["title"],
                "source_url": source["url"],
                "source_queries": " || ".join(source["queries"]),
                "evidence_snippet": source["snippet"],
                "cache_file": relative_path,
            })

        searches = data.get("searches", [])
        statuses = [
            str(s.get("status", ""))
            for s in searches if isinstance(s, dict)
        ] if isinstance(searches, list) else []

        summary_status = (
            "NO_SEARCH_RESULTS" if not sources
            else "SEARCH_RESULTS_NO_REVENUE_CANDIDATE" if not revenue
            else "CANDIDATES_NEED_VERIFICATION"
        )

        summaries.append({
            "company_key": company_key,
            "company_name": company,
            "company_website": official_domain,
            **select_revenue(revenue),
            **select_employees(employees),
            "revenue_candidate_count": len(revenue),
            "employee_candidate_count": len(employees),
            "unique_cached_source_count": len(sources),
            "search_status": " | ".join(sorted(set(statuses))),
            "status": summary_status,
            "cache_file": relative_path,
        })

    revenue_fields = [
        "company_key", "company_name", "source_rank", "source_score",
        "candidate_score", "source_title", "source_url",
        "source_queries", "matched_text", "amount_low", "amount_high",
        "amount_type", "currency", "reporting_year", "period",
        "status", "evidence_snippet", "cache_file",
    ]

    employee_fields = [
        "company_key", "company_name", "source_rank", "source_score",
        "source_title", "source_url", "source_queries", "matched_text",
        "employee_count_low", "employee_count_high",
        "employee_count_type", "location_type", "status",
        "evidence_snippet", "cache_file",
    ]

    summary_fields = [
        "company_key", "company_name", "company_website",
        "selected_revenue_low", "selected_revenue_high",
        "selected_revenue_currency", "selected_revenue_year",
        "selected_revenue_period", "selected_revenue_source",
        "selected_revenue_reason", "selected_employee_low",
        "selected_employee_high", "selected_employee_scope",
        "selected_employee_source", "revenue_candidate_count",
        "employee_candidate_count", "unique_cached_source_count",
        "search_status", "status", "cache_file",
    ]

    audit_fields = [
        "company_key", "company_name", "source_rank", "source_score",
        "source_title", "source_url", "source_queries",
        "evidence_snippet", "cache_file",
    ]

    write_csv(
        OUTPUT_DIR / "revenue_candidates.csv",
        all_revenue, revenue_fields,
    )
    write_csv(
        OUTPUT_DIR / "employee_candidates.csv",
        all_employees, employee_fields,
    )
    write_csv(
        OUTPUT_DIR / "revenue_summary.csv",
        summaries, summary_fields,
    )
    write_csv(
        OUTPUT_DIR / "sources_checked.csv",
        audits, audit_fields,
    )

    write_json(
        OUTPUT_DIR / "revenue_candidates.json", all_revenue
    )
    write_json(
        OUTPUT_DIR / "employee_candidates.json", all_employees
    )
    write_json(
        OUTPUT_DIR / "revenue_summary.json", summaries
    )

    print(f"Processed {len(files)} cache JSON files.")
    print(f"Companies summarized: {len(summaries)}")
    print(f"Revenue candidates: {len(all_revenue)}")
    print(f"Employee candidates: {len(all_employees)}")
    print(f"Sources evaluated: {len(audits)}")
    print(f"Output folder: {OUTPUT_DIR}")
    print("Cached input files were not modified.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
