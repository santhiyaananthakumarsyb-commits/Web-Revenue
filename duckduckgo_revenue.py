
#!/usr/bin/env python3
"""Save DuckDuckGo revenue search results in a separate folder per company."""

import argparse
import csv
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

INPUT_CSV = "companies_tier1.csv"
OUTPUT_DIR = Path("revenue_search_output")
MAX_RESULTS_PER_COMPANY = 10
MIN_DELAY_SECONDS = 3.0
MAX_DELAY_SECONDS = 5.0

SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"


def now_utc():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:80] or "company"


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def normalize_website(value):
    value = (value or "").strip()
    if value and not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    return value


def load_companies(path):
    if not path.exists():
        raise FileNotFoundError(
            f"CSV not found: {path.resolve()}"
        )

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ValueError("CSV has no header row.")

        name_col = next(
            (
                c for c in reader.fieldnames
                if c and c.strip().lower()
                in {"company_name", "company", "name"}
            ),
            None,
        )

        website_col = next(
            (
                c for c in reader.fieldnames
                if c and c.strip().lower()
                in {"website", "company_website", "url", "domain"}
            ),
            None,
        )

        if not name_col:
            raise ValueError(
                "CSV needs a company_name column."
            )

        companies = []

        for rownum, row in enumerate(reader, start=2):
            name = (row.get(name_col) or "").strip()

            if not name:
                continue

            website_raw = (
                (row.get(website_col) or "").strip()
                if website_col else ""
            )
            website = normalize_website(website_raw)

            companies.append({
                "company_name": name,
                "company_website": website,
                "website_input": website_raw,
                "input_row": rownum,
                "company_key": f"{name.lower()}|{website.lower()}",
            })

        return companies


def make_queries(company_name):
    return [
        f'"{company_name}" revenue',
        f'"{company_name}" annual revenue estimate',
    ]


def unwrap_url(url):
    url = (url or "").strip()

    if url.startswith("//"):
        url = "https:" + url

    try:
        parsed = urlparse(url)

        if (parsed.hostname or "").lower().endswith(
            "duckduckgo.com"
        ):
            target = parse_qs(parsed.query).get("uddg", [None])[0]

            if target:
                return unquote(target)

    except Exception:
        pass

    return url


def is_search_engine_url(url):
    host = (urlparse(url).hostname or "").lower()
    return (
        host == "duckduckgo.com"
        or host.endswith(".duckduckgo.com")
    )


def safe_page_title(page):
    try:
        return page.title()
    except Exception:
        return ""


def detect_block(page):
    try:
        text = page.locator("body").inner_text(
            timeout=2000
        ).lower()
    except Exception:
        text = ""

    phrases = [
        "select all squares",
        "unusual traffic",
        "verify you are human",
        "captcha",
        "automated requests",
        "too many requests",
    ]

    if any(phrase in text for phrase in phrases):
        return "BLOCKED_OR_CAPTCHA"

    return None if text.strip() else "EMPTY_PAGE"


def extract_results(page, limit=10):
    """Extract titles, URLs and snippets from search-result cards."""
    results = []
    seen = set()

    selectors = [
        "div.result",
        "div.web-result",
        "div.results_links",
        "article[data-testid='result']",
        "[data-testid='result']",
    ]

    cards = []

    for selector in selectors:
        try:
            found = page.locator(selector).all()

            if found:
                cards = found
                break

        except Exception:
            pass

    for card in cards:
        if len(results) >= limit:
            break

        try:
            link = card.locator(
                "a.result__a, "
                "a[data-testid='result-title-a'], "
                "h2 a, a"
            ).first

            title = link.inner_text(timeout=1500).strip()
            url = unwrap_url(link.get_attribute("href") or "")

            if not title or not url.startswith(("http://", "https://")):
                continue

            if is_search_engine_url(url) or url in seen:
                continue

            snippet = ""

            for selector in [
                ".result__snippet",
                "[data-result='snippet']",
                "[data-testid='result-snippet']",
                ".result-snippet",
                "p",
            ]:
                try:
                    snippet = card.locator(
                        selector
                    ).first.inner_text(timeout=800).strip()

                    if snippet:
                        break

                except Exception:
                    pass

            seen.add(url)

            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
            })

        except Exception:
            pass

    # Fallback if DuckDuckGo changes its result-card structure.
    if not results:
        try:
            links = page.locator("a").all()
        except Exception:
            links = []

        for link in links:
            if len(results) >= limit:
                break

            try:
                title = link.inner_text(timeout=500).strip()
                url = unwrap_url(
                    link.get_attribute("href") or ""
                )

                if len(title) < 4:
                    continue

                if not url.startswith(("http://", "https://")):
                    continue

                if is_search_engine_url(url):
                    continue

                if url in seen or len(title) > 300:
                    continue

                seen.add(url)

                results.append({
                    "title": title,
                    "url": url,
                    "snippet": "",
                })

            except Exception:
                pass

    return results[:limit]


def search_once(page, query, timeout_seconds):
    url = SEARCH_URL.format(query=quote_plus(query))

    try:
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=timeout_seconds * 1000,
        )

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=5000,
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(500)

        blocked = detect_block(page)

        if blocked:
            status = blocked
            results = []
        else:
            results = extract_results(
                page,
                MAX_RESULTS_PER_COMPANY,
            )
            status = (
                "SUCCESS" if results
                else "NO_RESULTS_PARSED"
            )

        return {
            "query": query,
            "status": status,
            "http_status": response.status if response else None,
            "collected_at": now_utc(),
            "page_title": safe_page_title(page),
            "final_url": page.url,
            "results": results,
        }

    except PlaywrightTimeoutError as exc:
        return {
            "query": query,
            "status": "TIMEOUT",
            "collected_at": now_utc(),
            "results": [],
            "error": str(exc)[:500],
        }

    except Exception as exc:
        return {
            "query": query,
            "status": "SEARCH_ERROR",
            "collected_at": now_utc(),
            "results": [],
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }


def combine_results(searches):
    """Merge results from both queries, removing duplicate URLs."""
    combined = []
    seen = set()

    for search in searches:
        for result in search.get("results", []):
            url = result.get("url", "").strip()

            if not url or url in seen:
                continue

            seen.add(url)

            combined.append({
                **result,
                "query": search.get("query", ""),
                "search_collected_at": search.get(
                    "collected_at", ""
                ),
            })

            if len(combined) >= MAX_RESULTS_PER_COMPANY:
                return combined

    return combined


def is_finished(cache_path):
    cache = read_json(cache_path, {})

    if not isinstance(cache, dict):
        return False

    searches = cache.get("searches", [])

    terminal = {
        "SUCCESS",
        "NO_RESULTS_PARSED",
        "BLOCKED_OR_CAPTCHA",
    }

    return (
        len(searches) >= 2
        and all(
            item.get("status") in terminal
            for item in searches[:2]
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Cache DuckDuckGo results per company."
    )

    parser.add_argument("--input", default=INPUT_CSV)
    parser.add_argument("--output", default=str(OUTPUT_DIR))
    parser.add_argument("--max-companies", type=int, default=None)
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window.",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Retry queries even if cached.",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=MIN_DELAY_SECONDS,
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=MAX_DELAY_SECONDS,
    )
    parser.add_argument("--timeout", type=int, default=30)

    args = parser.parse_args()

    if args.min_delay < 0 or args.max_delay < args.min_delay:
        parser.error("Require 0 <= --min-delay <= --max-delay")

    output = Path(args.output)
    companies_root = output / "companies"
    companies_root.mkdir(parents=True, exist_ok=True)
    (output / "logs").mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                output / "logs" / "scraper.log",
                encoding="utf-8",
            ),
        ],
    )

    companies = load_companies(Path(args.input))

    if args.max_companies is not None:
        companies = companies[:args.max_companies]

    progress_path = output / "progress.json"
    progress = read_json(
        progress_path,
        {"companies": {}},
    )
    progress.setdefault("companies", {})

    logging.info("Companies loaded: %s", len(companies))
    logging.info("Output folder: %s", output.resolve())
    logging.info("Queries per company: 2")
    logging.info("Maximum unique results per company: 10")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not args.headed
        )
        context = browser.new_context(
            viewport={"width": 1365, "height": 900}
        )
        page = context.new_page()
        page.set_default_navigation_timeout(
            args.timeout * 1000
        )

        requests_made = 0

        try:
            for index, company in enumerate(companies, start=1):
                folder = (
                    f"{slugify(company['company_name'])}_"
                    f"{company['input_row']:05d}"
                )

                company_dir = companies_root / folder
                company_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                cache_path = company_dir / "search_cache.json"
                company_path = company_dir / "company.json"

                if (
                    cache_path.exists()
                    and not args.retry
                    and is_finished(cache_path)
                ):
                    logging.info(
                        "[%s/%s] Cached; skipping %s",
                        index,
                        len(companies),
                        company["company_name"],
                    )
                    continue

                write_json_atomic(company_path, company)

                cache = read_json(cache_path, {})

                if (
                    not isinstance(cache, dict)
                    or cache.get("company_key")
                    != company["company_key"]
                ):
                    cache = {
                        "company_key": company["company_key"],
                        "company_name": company["company_name"],
                        "company_website": company["company_website"],
                        "created_at": now_utc(),
                        "searches": [],
                        "results": [],
                    }

                queries = make_queries(company["company_name"])

                old = {
                    item.get("query"): item
                    for item in cache.get("searches", [])
                    if isinstance(item, dict)
                    and item.get("query")
                }

                blocked = False

                for query_index, query in enumerate(queries):
                    previous = old.get(query)

                    # Preserve successful results on resume.
                    if (
                        previous
                        and previous.get("status") == "SUCCESS"
                        and not args.retry
                    ):
                        continue

                    if (
                        previous
                        and previous.get("status")
                        == "BLOCKED_OR_CAPTCHA"
                        and not args.retry
                    ):
                        blocked = True
                        break

                    if requests_made:
                        time.sleep(
                            random.uniform(
                                args.min_delay,
                                args.max_delay,
                            )
                        )

                    logging.info(
                        "[%s/%s] Query %s/2: %s",
                        index,
                        len(companies),
                        query_index + 1,
                        query,
                    )

                    item = search_once(
                        page,
                        query,
                        args.timeout,
                    )
                    requests_made += 1
                    old[query] = item

                    searches = [
                        old[q] for q in queries if q in old
                    ]

                    cache["searches"] = searches
                    cache["results"] = combine_results(searches)
                    cache["result_count"] = len(cache["results"])
                    cache["updated_at"] = now_utc()
                    cache["company_status"] = item["status"]

                    # Save immediately after each query.
                    write_json_atomic(cache_path, cache)

                    progress["companies"][company["company_key"]] = {
                        "company_name": company["company_name"],
                        "folder": str(company_dir),
                        "status": item["status"],
                        "result_count": cache["result_count"],
                        "updated_at": now_utc(),
                    }
                    progress["updated_at"] = now_utc()
                    write_json_atomic(progress_path, progress)

                    logging.info(
                        "Status: %s | Query results: %s | "
                        "Company total: %s",
                        item["status"],
                        len(item.get("results", [])),
                        cache["result_count"],
                    )

                    if item["status"] == "BLOCKED_OR_CAPTCHA":
                        logging.warning(
                            "Access restriction detected. "
                            "Stopping rather than bypassing it."
                        )
                        blocked = True
                        break

                searches = [
                    old[q] for q in queries if q in old
                ]

                cache["searches"] = searches
                cache["results"] = combine_results(searches)
                cache["result_count"] = len(cache["results"])
                cache["updated_at"] = now_utc()

                statuses = [
                    item.get("status") for item in searches
                ]

                if "BLOCKED_OR_CAPTCHA" in statuses:
                    cache["company_status"] = "BLOCKED_OR_CAPTCHA"
                elif (
                    len(searches) == 2
                    and any(s == "SUCCESS" for s in statuses)
                ):
                    cache["company_status"] = "SUCCESS"
                elif (
                    len(searches) == 2
                    and all(
                        s == "NO_RESULTS_PARSED"
                        for s in statuses
                    )
                ):
                    cache["company_status"] = "NO_RESULTS_PARSED"
                else:
                    cache["company_status"] = "NEEDS_RETRY"

                write_json_atomic(cache_path, cache)

                progress["companies"][company["company_key"]] = {
                    "company_name": company["company_name"],
                    "folder": str(company_dir),
                    "status": cache["company_status"],
                    "result_count": cache["result_count"],
                    "updated_at": now_utc(),
                }
                progress["updated_at"] = now_utc()
                write_json_atomic(progress_path, progress)

                logging.info(
                    "[%s/%s] Finished: %s | %s results | %s",
                    index,
                    len(companies),
                    company["company_name"],
                    cache["result_count"],
                    cache["company_status"],
                )

                if blocked:
                    break

        except KeyboardInterrupt:
            logging.warning(
                "Interrupted. Saved cache files can be resumed."
            )

        finally:
            context.close()
            browser.close()

    logging.info(
        "Finished. Company files are under %s",
        companies_root.resolve(),
    )


if __name__ == "__main__":
    main()
