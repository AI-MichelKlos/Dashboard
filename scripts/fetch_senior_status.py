#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import build_dashboard as api


BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "data" / "senior-status.json"
HISTORY_MONTHS = 72


def norm(value):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "")
        .lower()
        .replace("æ", "ae")
        .replace("ø", "oe")
        .replace("å", "aa"),
    ).strip()


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def blob(item):
    return norm(" ".join(f"{key} {value}" for key, value in item.items() if isinstance(value, (str, int, float))))


def records(payload):
    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise RuntimeError("Uventet Jobindsats-format")
    return [dict(zip(columns, row)) for row in rows]


def number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = str(value).strip().replace("\xa0", "").replace(" ", "")
        if not text or text in {"-", ".", ".."}:
            return None
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        try:
            result = float(text)
        except ValueError:
            return None
    return result if math.isfinite(result) else None


def percentage(value):
    result = number(value)
    if result is None:
        return None
    if abs(result) <= 1:
        result *= 100
    return round(result, 2)


def find_table(payload):
    candidates = []
    for item in walk(payload):
        table_id = item.get("table_id")
        if not table_id:
            continue
        text = blob(item)
        score = 1000 if "arbejdsmarkedsstatus for seniorer" in text else 0
        for word in ("arbejdsmarkedsstatus", "seniorer", "senior"):
            score += 100 if word in text else 0
        candidates.append((score, len(text), str(table_id), text))
    candidates.sort(reverse=True)
    if not candidates or candidates[0][0] < 300:
        raise RuntimeError("Kunne ikke identificere seniormålingen i Jobindsats")
    return candidates[0][2]


def hierarchies(spec):
    result = {}
    for item in walk(spec):
        hierarchy_id = item.get("hierarchy_id")
        if isinstance(hierarchy_id, str) and len(json.dumps(item, ensure_ascii=False)) > len(
            json.dumps(result.get(hierarchy_id, {}), ensure_ascii=False)
        ):
            result[hierarchy_id] = item
    return list(result.values())


def find_hierarchy(spec, words, preferred=()):
    choices = []
    for item in hierarchies(spec):
        hierarchy_id = str(item.get("hierarchy_id"))
        text = norm(json.dumps(item, ensure_ascii=False))
        score = 0
        if hierarchy_id in preferred:
            score += 1000 - preferred.index(hierarchy_id) * 20
        for word in words:
            candidate = norm(word)
            score += 200 if candidate in norm(hierarchy_id) else 0
            score += 60 if candidate in text else 0
        choices.append((score, len(text), item))
    choices.sort(reverse=True, key=lambda value: (value[0], value[1]))
    if not choices or choices[0][0] <= 0:
        raise RuntimeError(f"Hierarki ikke fundet: {words}")
    return choices[0][2]


def country_value(hierarchy):
    for item in walk(hierarchy):
        if isinstance(item.get("value_id"), str) and (
            "hele landet" in blob(item) or "hele danmark" in blob(item)
        ):
            return item["value_id"]
    return "/"


def deepest_level(hierarchy):
    levels = {}
    for item in walk(hierarchy):
        level_id = item.get("level_id")
        if isinstance(level_id, str):
            levels.setdefault(level_id, set()).update(
                str(child.get("value_id"))
                for child in walk(item)
                if isinstance(child.get("value_id"), str)
            )
    return max(levels, key=lambda key: len(levels[key])) if levels else None


def best_col(rows, include, exclude=(), distinct=False):
    columns = list(rows[0])
    choices = []
    for column in columns:
        name = norm(column)
        score = sum(100 for word in include if norm(word) in name)
        score -= sum(150 for word in exclude if norm(word) in name)
        if score > 0:
            count = len({str(row.get(column)) for row in rows if row.get(column) not in (None, "")})
            choices.append((score + (min(count, 100) if distinct else 0), count, column))
    if not choices:
        raise RuntimeError(f"Kolonne ikke fundet: {include}. {columns}")
    choices.sort(reverse=True)
    return choices[0][2]


def exact_col(rows, name):
    target = norm(name)
    for column in rows[0]:
        if norm(column) == target:
            return column
    raise RuntimeError(f"Dimensionskolonnen {name!r} mangler. Kolonner: {list(rows[0])}")


def period_key(period):
    match = re.fullmatch(r"(\d{4})M(\d{2})", str(period))
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def age_value(label):
    match = re.search(r"(?<!\d)(\d{2})(?!\d)", str(label or "").strip())
    if not match:
        return None
    age = int(match.group(1))
    return age if age >= 55 else None


def data_path(table_id, geo, age, status, age_level, months, sex=None):
    parts = [
        "mgroup.*=*",
        f"period.M=latest:{months}",
        f"hierarchy.{geo['hierarchy_id']}={country_value(geo)}",
        f"hierarchy.{age['hierarchy_id']}=" + (f"level:{age_level}" if age_level else "*"),
        f"hierarchy.{status['hierarchy_id']}=*",
    ]
    if sex is not None:
        parts.append(f"hierarchy.{sex['hierarchy_id']}=*")
    parts.append("format=json")
    return f"data/{table_id}?" + "&".join(parts)


def main():
    table_id = find_table(api.jobindsats_get("tables?format=json"))
    spec = api.jobindsats_get(f"table/{table_id}?format=json")
    geo = find_hierarchy(spec, ["område", "geografi", "kommune"], ("_hele_landet", "_nykom"))
    age = find_hierarchy(spec, ["alder"])
    status = find_hierarchy(spec, ["arbejdsmarkedsstatus", "status"])
    sex = find_hierarchy(spec, ["køn", "koen", "sex"])
    age_level = deepest_level(age)

    rows = records(api.jobindsats_get(data_path(table_id, geo, age, status, age_level, HISTORY_MONTHS)))
    period_col = exact_col(rows, "Periode")
    age_col = exact_col(rows, "Alder")
    status_col = exact_col(rows, "Arbejdsmarkedsstatus")
    percent_col = best_col(rows, ["andel", "procent", "pct"], exclude=["grad"])
    count_col = best_col(rows, ["antal"], exclude=["andel", "procent", "pct", "grad"])
    degree_col = best_col(rows, ["grad"], exclude=["andel", "antal"])

    periods = sorted({str(row.get(period_col)) for row in rows}, key=period_key)
    latest_period = periods[-1]
    employment_status = "loenmodtagerbeskaeftigelse i alt"
    public_status = "tilbagetraeknings og oevrig off ydelse i alt"
    private_status = "egen pension og selvforsoergelse i alt"
    work_pension_status = "loenmodt beskaeft og folkepension"
    available_statuses = {norm(row.get(status_col)) for row in rows}
    required_statuses = {employment_status, public_status, private_status, work_pension_status}
    missing = sorted(required_statuses - available_statuses)
    if missing:
        raise RuntimeError(f"Påkrævede seniorstatusser mangler: {missing}. Tilgængelige statusser: {sorted(available_statuses)}")

    latest = [row for row in rows if str(row.get(period_col)) == latest_period]
    by_status_age = {}
    for row in latest:
        parsed_age = age_value(row.get(age_col))
        if parsed_age is not None:
            by_status_age[(norm(row.get(status_col)), parsed_age)] = row

    items = []
    status_by_age = []
    work_with_pension = []
    employment_degree = []
    ages = sorted({age_value(row.get(age_col)) for row in latest if age_value(row.get(age_col)) is not None})
    for parsed_age in ages:
        employment_row = by_status_age.get((employment_status, parsed_age))
        if employment_row is not None:
            share = percentage(employment_row.get(percent_col))
            degree = percentage(employment_row.get(degree_col))
            if share is not None:
                items.append({"age": parsed_age, "label": str(employment_row.get(age_col)).strip(), "employmentShare": share})
            if degree is not None:
                employment_degree.append({"age": parsed_age, "label": str(employment_row.get(age_col)).strip(), "value": degree})

        status_values = {}
        for key, output_key in (
            (employment_status, "employment"),
            (public_status, "publicSupport"),
            (private_status, "privatePensionOrSelfSupport"),
        ):
            row = by_status_age.get((key, parsed_age))
            status_values[output_key] = percentage(row.get(percent_col)) if row is not None else None
        if all(value is not None for value in status_values.values()):
            status_by_age.append({"age": parsed_age, "label": f"{parsed_age} år" if parsed_age < 75 else "75+ år", **status_values})

        pension_row = by_status_age.get((work_pension_status, parsed_age))
        if pension_row is not None:
            share = percentage(pension_row.get(percent_col))
            count = number(pension_row.get(count_col))
            if share is not None and count is not None:
                work_with_pension.append({"age": parsed_age, "label": str(pension_row.get(age_col)).strip(), "share": share, "count": round(count)})

    if len(items) < 8:
        raise RuntimeError(f"Kun {len(items)} etårige aldersgrupper fundet for lønmodtagerbeskæftigelse")

    history_ages = (60, 65, 67, 70)
    history_map = {age_number: {} for age_number in history_ages}
    for row in rows:
        parsed_age = age_value(row.get(age_col))
        if parsed_age not in history_map or norm(row.get(status_col)) != employment_status:
            continue
        value = percentage(row.get(percent_col))
        if value is not None:
            history_map[parsed_age][str(row.get(period_col))] = value
    history_labels = sorted(set().union(*(values.keys() for values in history_map.values())), key=period_key)
    history = {
        "labels": history_labels,
        "series": [
            {"age": age_number, "label": f"{age_number} år", "values": [history_map[age_number].get(period) for period in history_labels]}
            for age_number in history_ages
        ],
    }

    gender_rows = records(api.jobindsats_get(data_path(table_id, geo, age, status, age_level, 1, sex=sex)))
    gender_age_col = exact_col(gender_rows, "Alder")
    gender_status_col = exact_col(gender_rows, "Arbejdsmarkedsstatus")
    gender_col = best_col(gender_rows, ["køn", "koen", "sex"], distinct=True)
    gender_percent_col = best_col(gender_rows, ["andel", "procent", "pct"], exclude=["grad"])
    gender_map = {}
    for row in gender_rows:
        parsed_age = age_value(row.get(gender_age_col))
        if parsed_age is None or norm(row.get(gender_status_col)) != employment_status:
            continue
        gender_name = norm(row.get(gender_col))
        key = "men" if gender_name in {"maend", "mand", "men"} else "women" if gender_name in {"kvinder", "kvinde", "women"} else None
        value = percentage(row.get(gender_percent_col))
        if key and value is not None:
            gender_map.setdefault(parsed_age, {})[key] = value
    gender_items = [
        {"age": age_number, "label": f"{age_number} år" if age_number < 75 else "75+ år", **values}
        for age_number, values in sorted(gender_map.items())
        if "men" in values and "women" in values
    ]

    payload = {
        "meta": {
            "source": "Jobindsats.dk / STAR",
            "dataset": table_id,
            "latestPeriod": latest_period,
            "checkedAt": datetime.now(ZoneInfo("Europe/Copenhagen")).isoformat(timespec="seconds"),
            "measure": "Arbejdsmarkedsstatus for seniorer",
            "status": "Lønmodtagerbeskæftigelse i alt",
            "unit": "pct.",
            "historyMonthsRequested": HISTORY_MONTHS,
        },
        "period": latest_period,
        "items": items,
        "statusByAge": status_by_age,
        "history": history,
        "workWithPension": {
            "items": work_with_pension,
            "totalCount67Plus": sum(item["count"] for item in work_with_pension if item["age"] >= 67),
        },
        "employmentDegree": employment_degree,
        "gender": gender_items,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Seniorfeed", table_id, latest_period, len(items), "aldre,", len(history_labels), "måneder,", len(gender_items), "kønsobservationer")


if __name__ == "__main__":
    main()
