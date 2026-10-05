#!/usr/bin/env python3
"""Opt-in live audit of verified animal-rescue discovery across Indian cities.

This intentionally does not assert any organisation name. It checks whether a
city-level source search completes safely and whether every returned option has
the minimum verified structure. No NGO-search cache rows are read or written.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

os.environ["DOG_WEB_SEARCH_CACHE_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import web_search  # noqa: E402

# config intentionally gives .env values precedence over process variables.
# Override the already-loaded runtime value as well so this audit can never
# read or write NGO cache rows, regardless of the deployment's .env file.
web_search.config.DOG_WEB_SEARCH_CACHE_ENABLED = False


CITIES = (
    ("Manali", "Himachal Pradesh", 32.2396, 77.1887),
    ("Dharamshala", "Himachal Pradesh", 32.2196, 76.3234),
    ("Srinagar", "Jammu and Kashmir", 34.0837, 74.7973),
    ("Shimla", "Himachal Pradesh", 31.1048, 77.1734),
    ("Chandigarh", "Chandigarh", 30.7333, 76.7794),
    ("New Delhi", "Delhi", 28.6139, 77.2090),
    ("Jaipur", "Rajasthan", 26.9124, 75.7873),
    ("Lucknow", "Uttar Pradesh", 26.8467, 80.9462),
    ("Patna", "Bihar", 25.5941, 85.1376),
    ("Guwahati", "Assam", 26.1445, 91.7362),
    ("Kolkata", "West Bengal", 22.5726, 88.3639),
    ("Ranchi", "Jharkhand", 23.3441, 85.3096),
    ("Bhubaneswar", "Odisha", 20.2961, 85.8245),
    ("Mumbai", "Maharashtra", 19.0760, 72.8777),
    ("Pune", "Maharashtra", 18.5204, 73.8567),
    ("Ahmedabad", "Gujarat", 23.0225, 72.5714),
    ("Bhopal", "Madhya Pradesh", 23.2599, 77.4126),
    ("Hyderabad", "Telangana", 17.3850, 78.4867),
    ("Bengaluru", "Karnataka", 12.9716, 77.5946),
    ("Chennai", "Tamil Nadu", 13.0827, 80.2707),
    ("Kochi", "Kerala", 9.9312, 76.2673),
    ("Thiruvananthapuram", "Kerala", 8.5241, 76.9366),
)


def _normalise_host(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    return host.rstrip(".")


def audit_city(city_data: tuple[str, str, float, float]) -> dict:
    city, region, lat, lng = city_data
    started = time.monotonic()
    try:
        result = web_search.search_verified_india_local_help(
            f"{city}, {region}, India",
            lat=lat,
            lng=lng,
            city=city,
            region=region,
            country_code="IN",
            # This deliberately contains no NGO, rescue, or organisation name.
            situation=f"A dog in {city} was hit by a car. Please help.",
            named_location_verified=True,
        )
        organizations = []
        seen_sites: set[str] = set()
        validation_errors: list[str] = []
        if result.cached:
            validation_errors.append("unexpected_cached_result")
        for option in result.organizations:
            name = str(option.get("name") or "").strip()
            url = str(option.get("official_url") or "").strip()
            host = _normalise_host(url)
            site_key = web_search._website_identity_key(url)
            if not name:
                validation_errors.append("missing_name")
            if not host or urlparse(url).scheme not in {"http", "https"}:
                validation_errors.append(f"invalid_url:{url}")
            if site_key and site_key in seen_sites:
                validation_errors.append(f"duplicate_site:{site_key}")
            if web_search._is_government_or_directory_domain(host):
                validation_errors.append(f"directory_or_government_domain:{host}")
            if web_search._validate_cached_ngo_organization(
                option,
                required_city=city,
                required_region=region,
            ) is None:
                validation_errors.append(f"invalid_verified_snapshot:{name or host}")
            seen_sites.add(site_key)
            organizations.append(
                {
                    "name": name,
                    "official_url": url,
                    "phone": str(option.get("phone") or ""),
                    "coverage_scope": str(option.get("coverage_scope") or "city"),
                }
            )
        if len(organizations) > web_search.config.DOG_WEB_SEARCH_MAX_RESULTS:
            validation_errors.append("too_many_results")

        if validation_errors:
            status = "invalid"
        elif result.searched and result.result_kind == "verified_options" and organizations:
            status = "verified"
        elif (
            result.searched
            and result.result_kind == "no_results"
            and not organizations
            and result.candidate_discovery_complete is True
        ):
            status = "no_verified_options"
        else:
            status = "unavailable"
        return {
            "city": city,
            "region": region,
            "status": status,
            "searched": result.searched,
            "result_kind": result.result_kind,
            "candidate_discovery_complete": result.candidate_discovery_complete,
            "organizations": organizations,
            "validation_errors": validation_errors,
            "seconds": round(time.monotonic() - started, 2),
        }
    except Exception as exc:  # pragma: no cover - live diagnostic boundary
        return {
            "city": city,
            "region": region,
            "status": "error",
            "error_type": type(exc).__name__,
            "seconds": round(time.monotonic() - started, 2),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=len(CITIES))
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    parser.add_argument(
        "--batch-timeout-seconds",
        type=float,
        default=0,
        help="Overall audit timeout; 0 derives a bounded value from runtime settings.",
    )
    args = parser.parse_args()
    selected = CITIES[args.start : args.start + args.limit]
    if args.start < 0 or args.limit <= 0 or not selected:
        parser.error("--start/--limit must select at least one configured city")
    batch_timeout = args.batch_timeout_seconds or (
        (len(selected) + args.workers - 1)
        // args.workers
        * (web_search.config.DOG_WEB_SEARCH_TOTAL_TIMEOUT_SECONDS + 30.0)
    )
    if batch_timeout <= 0:
        parser.error("--batch-timeout-seconds must be positive")
    results: list[dict] = []
    executor = ThreadPoolExecutor(max_workers=args.workers)
    futures = {executor.submit(audit_city, item): item[0] for item in selected}
    done, pending = wait(futures, timeout=batch_timeout)
    for future in done:
        result = future.result()
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    for future in pending:
        future.cancel()
        result = {
            "city": futures[future],
            "status": "error",
            "error_type": "AuditTimeout",
            "seconds": round(batch_timeout, 2),
        }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    executor.shutdown(wait=False, cancel_futures=True)

    ordered = sorted(results, key=lambda item: [city[0] for city in selected].index(item["city"]))
    counts: dict[str, int] = {}
    for result in ordered:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    print(
        json.dumps(
            {"summary": {"cities": len(ordered), "status_counts": counts}},
            ensure_ascii=False,
        )
    )
    return 1 if any(item["status"] in {"invalid", "error", "unavailable"} for item in ordered) else 0


if __name__ == "__main__":
    raise SystemExit(main())
