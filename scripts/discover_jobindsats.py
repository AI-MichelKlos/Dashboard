#!/usr/bin/env python3
"""Discover Jobindsats v3 measurements relevant to the labour-market dashboard."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_ROOT = "https://api.jobindsats.dk/v3"
OUTPUT = Path("data/jobindsats-discovery.json")
KEYWORDS = {
    "unemployment": [r"ledig", r"fuldtidsperson", r"arbejdsstyrk"],
    "vacancies": [r"ledig.{0,20}stilling", r"jobnet"],
    "long_term_unemployment": [r"langtidsledig"],
    "notices": [r"varsl", r"afskedig"],
}
PROBE_QUERIES = {
    "unemployment": (
        "data/y25i03?mgroup.*=*&period.M=latest:14"
        "&hierarchy._hele_landet=/&hierarchy._ygrpi09=/&format=json"
    ),
    "vacancies": (
        "data/y25i07?mgroup.*=*&period.M=latest:14"
        "&hierarchy._nykom=/&format=json"
    ),
    "long_term_unemployment": (
        "data/y25i09?mgroup.*=*&period.M=latest:14"
        "&hierarchy._nykom=/&hierarchy._ygrpi09=/&format=json"
    ),
    "notices": (
        "data/y25i05?mgroup.*=*&period.M=latest:14"
        "&hierarchy._nykom=/&format=json"
    ),
}


def api_get(path: str):
    token = os.environ.get("JOBINDSATS_API_TOKEN")
    if not token:
        raise RuntimeError("JOBINDSATS_API_TOKEN mangler")
    request = urllib.request.Request(
        f"{API_ROOT}/{path.lstrip('/')}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Danske-A-kasser-dashboard/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Jobindsats returnerede HTTP {exc.code}: {detail[:500]}") from exc


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def first_text(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def table_id(item: dict) -> str:
    return first_text(item, ("table_id", "tableId", "id"))


def table_title(item: dict) -> str:
    return first_text(item, ("table_name", "tableName", "name", "title", "text"))


def schema(value, depth=0):
    """Describe response structure without printing data values."""
    if depth >= 5:
        return type(value).__name__
    if isinstance(value, dict):
        return {key: schema(child, depth + 1) for key, child in value.items()}
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "item": schema(value[0], depth + 1) if value else None,
        }
    return type(value).__name__


def main():
    catalog = api_get("tables?format=json")
    candidates = {}
    seen = set()

    for item in walk(catalog):
        identifier = table_id(item)
        title = table_title(item)
        if not identifier or not title or identifier in seen:
            continue
        haystack = json.dumps(item, ensure_ascii=False).lower()
        matched_groups = [
            group
            for group, patterns in KEYWORDS.items()
            if any(re.search(pattern, haystack, flags=re.IGNORECASE) for pattern in patterns)
        ]
        if not matched_groups:
            continue
        seen.add(identifier)
        candidates[identifier] = {
            "table_id": identifier,
            "title": title,
            "candidate_for": matched_groups,
            "catalog_entry": item,
        }

    for identifier, candidate in candidates.items():
        candidate["metadata"] = api_get(f"table/{identifier}?format=json")

    payload = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "candidates": sorted(candidates.values(), key=lambda item: item["table_id"]),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, path in PROBE_QUERIES.items():
        response = api_get(path)
        print(f"COLUMNS {name}: {response.get('columns', [])}")
        print(f"SCHEMA {name}:")
        print(json.dumps(schema(response), ensure_ascii=False, indent=2))
    print(f"Fandt {len(candidates)} relevante Jobindsats-kandidater")


if __name__ == "__main__":
    main()
