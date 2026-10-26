
import csv
import json
import re
import time
from pathlib import Path

import requests

# ---------- SETTINGS ----------
INPUT_CSV = Path("companies_tier1.csv")
OUTPUT_DIR = Path("purili_test_output")

TEST_COMPANIES = 50
RESULTS_PER_COMPANY = 1       # Search page 1 only for this initial test
DELAY_SECONDS = 1.5           # Be considerate of the service
REQUEST_TIMEOUT = 30
# ------------------------------

OUTPUT_DIR.mkdir(exist_ok=True)

RESULTS_CSV = OUTPUT_DIR / "search_results.csv"
RESULTS_JSONL = OUTPUT_DIR / "search_results.jsonl"
CANDIDATES_CSV = OUTPUT_DIR / "revenue_candidates.csv"
PROGRESS_FILE = OUTPUT_DIR / "progress.json"

API_URL = "https://puri.li/api/search"

session = requests.Session()
session.headers.update({
    "User-Agent": "RevenueResearchTest/1.0"
})

# Heuristic only: flags possible revenue mentions, not verified figures.
REVENUE_WORDS = re.compile(
    r"\b(revenue|revenues|turnover|sales|annual income|net sales)\b",
    re.IGNORECASE,
)

AMOUNT_PATTERN = re.compile(
    r"""(?ix)
    (?:
        (?:US\$|USD\s*|[$€£₹]\s*)?
        \d[\d,]*(?:\.\d+)?
        \s*
        (?:billion|million|trillion|bn|mn|m|k|crore|lakh)?
        \s*
        (?:USD|INR|EUR|GBP|dollars?|rupees?)?
    )
    """
)

RESULT_FIELDS = [
    "company_name",
    "search_query",
    "title",
    "snippet",
    "url",
    "possible_revenue_candidate",
]


def read_companies():
    with INPUT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "company_name" not in reader.fieldnames:
            raise ValueError(
                "Input CSV must contain a column named 'company_name'."
            )

        names = []
        for row in reader:
            name = (row.get("company_name") or "").strip()
            if name:
                names.append(name)

    # Keep original order and remove duplicate company names.
    return list(dict.fromkeys(names))[:TEST_COMPANIES]


def load_progress():
    if not PROGRESS_FILE.exists():
        return {"completed_companies": []}

    try:
        with PROGRESS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print("Progress file could not be read; starting fresh.")
        return {"completed_companies": []}


def save_progress(completed):
    temp_file = PROGRESS_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(
            {"completed_companies": sorted(completed)},
            f,
            ensure_ascii=False,
            indent=2,
        )
    temp_file.replace(PROGRESS_FILE)


def is_possible_candidate(title, snippet):
    text = f"{title} {snippet}"

    if not REVENUE_WORDS.search(text):
        return False

    # Require a number and a revenue-related word in the same result.
    # This intentionally errs on the side of catching possible candidates.
    return bool(AMOUNT_PATTERN.search(text))


def append_csv(path, row, fields):
    file_exists = path.exists() and path.stat().st_size > 0

    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def append_jsonl(row):
    with RESULTS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def search_company(company):
    query = f'"{company}" revenue'

    response = session.get(
        API_URL,
        params={
            "q": query,
            "page": 1,
            "exact": 1,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict):
        raise ValueError("Unexpected API response format.")

    results = data.get("results", [])
    if not isinstance(results, list):
        return query, []

    rows = []

    for item in results:
        if not isinstance(item, dict):
            continue

        title = str(item.get("title") or "").strip()
        snippet = str(item.get("description") or "").strip()
        url = str(item.get("url") or "").strip()

        candidate = is_possible_candidate(title, snippet)

        rows.append({
            "company_name": company,
            "search_query": query,
            "title": title,
            "snippet": snippet,
            "url": url,
            "possible_revenue_candidate": candidate,
        })

    return query, rows


def main():
    companies = read_companies()
    if not companies:
        print("No company names found in the input CSV.")
        return

    progress = load_progress()
    completed = set(progress.get("completed_companies", []))

    todo = [name for name in companies if name not in completed]

    print(f"Companies in this test: {len(companies)}")
    print(f"Already completed: {len(completed)}")
    print(f"Remaining: {len(todo)}")
    print(f"Output folder: {OUTPUT_DIR.resolve()}")

    for index, company in enumerate(todo, start=1):
        print(f"[{index}/{len(todo)}] Searching: {company}")

        try:
            query, rows = search_company(company)

            for row in rows:
                append_csv(RESULTS_CSV, row, RESULT_FIELDS)
                append_jsonl(row)

                if row["possible_revenue_candidate"]:
                    append_csv(
                        CANDIDATES_CSV,
                        row,
                        RESULT_FIELDS,
                    )

            # Mark a company complete only after a successful API response.
            completed.add(company)
            save_progress(completed)

            candidate_count = sum(
                1 for row in rows
                if row["possible_revenue_candidate"]
            )
            print(
                f"  Results: {len(rows)} | "
                f"Possible candidates: {candidate_count}"
            )

        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response else "unknown"
            print(f"  HTTP error ({status}); stopping the test.")
            print("  You can rerun the script later to resume.")
            break

        except (requests.RequestException, ValueError) as exc:
            print(f"  Request/data error: {exc}")
            print("  Stopping to avoid repeated failed requests.")
            break

        except KeyboardInterrupt:
            print("\nInterrupted. Saved progress can be resumed.")
            break

        time.sleep(DELAY_SECONDS)

    print("\nTest finished or paused.")
    print(f"Search results: {RESULTS_CSV}")
    print(f"JSONL results:  {RESULTS_JSONL}")
    print(f"Candidates:     {CANDIDATES_CSV}")
    print(f"Progress:       {PROGRESS_FILE}")
    print(
        "\nCandidate matches are heuristic flags only; "
        "they are not verified revenue figures."
    )


if __name__ == "__main__":
    main()
