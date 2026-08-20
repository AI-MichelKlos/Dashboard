#!/usr/bin/env python3
"""Robust daily update of the labour-market dashboard."""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import build_dashboard as builder


MONTH_LIMIT = 24
REQUEST_TIMEOUT = 60
MAX_ATTEMPTS = 3
STATBANK_API_URL = "https://api.statbank.dk/v1/data"


def compact_period_request(path: str) -> str:
    return re.sub(
        r"period\.M=latest:\d+",
        f"period.M=latest:{MONTH_LIMIT}",
        path,
        count=1,
    )


def retry_pause(attempt: int, label: str, error: Exception) -> None:
    pause = attempt * 15
    print(
        f"{label} fejlede, forsøg {attempt} af {MAX_ATTEMPTS}. "
        f"Prøver igen om {pause} sekunder: {error}",
        flush=True,
    )
    time.sleep(pause)


def robust_jobindsats_get(path: str):
    token = os.environ.get("JOBINDSATS_API_TOKEN")
    if not token:
        raise RuntimeError("JOBINDSATS_API_TOKEN mangler")

    request_path = compact_period_request(path)
    request = urllib.request.Request(
        f"{builder.JOBINDSATS_API_ROOT}/{request_path.lstrip('/')}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Danske-A-kasser-dashboard/3.1",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error = RuntimeError(f"Jobindsats returnerede HTTP {exc.code}: {detail[:300]}")
            if exc.code not in {429, 500, 502, 503, 504}:
                raise error from exc
            last_error = error
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            last_error = exc

        if attempt < MAX_ATTEMPTS:
            retry_pause(attempt, "Jobindsats-kaldet", last_error)

    raise RuntimeError(
        f"Jobindsats-kaldet fejlede efter {MAX_ATTEMPTS} forsøg: "
        f"{request_path}: {last_error}"
    ) from last_error


def statbank_get(table: str, variables: dict[str, list[str]]):
    body = {
        "table": table,
        "format": "JSONSTAT",
        "lang": "da",
        "variables": [
            {"code": code, "values": values}
            for code, values in variables.items()
        ],
    }
    request = urllib.request.Request(
        STATBANK_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Danske-A-kasser-dashboard/3.1",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8-sig"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error = RuntimeError(
                f"Statistikbanken returnerede HTTP {exc.code} for {table}: {detail[:500]}"
            )
            if exc.code not in {429, 500, 502, 503, 504}:
                raise error from exc
            last_error = error
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            last_error = exc

        if attempt < MAX_ATTEMPTS:
            retry_pause(attempt, f"Statistikbank-kaldet til {table}", last_error)

    raise RuntimeError(
        f"Statistikbank-kaldet til {table} fejlede efter {MAX_ATTEMPTS} forsøg: "
        f"{last_error}"
    ) from last_error


def category_positions(category: dict) -> dict[str, int]:
    index = category.get("index")
    if isinstance(index, dict):
        return {str(code): int(position) for code, position in index.items()}
    if isinstance(index, list):
        return {str(code): position for position, code in enumerate(index)}
    labels = category.get("label")
    if isinstance(labels, dict):
        return {str(code): position for position, code in enumerate(labels)}
    raise RuntimeError("JSON-stat-kategorien mangler et anvendeligt indeks")


def jsonstat_number(values, index: int):
    if isinstance(values, list):
        value = values[index] if index < len(values) else None
    elif isinstance(values, dict):
        value = values.get(str(index), values.get(index))
    else:
        raise RuntimeError("JSON-stat-svaret mangler en anvendelig value-samling")

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace("\xa0", "").replace(" ", "")
        if text.lower() in {"", "-", ".", "..", "null", "none", "nan"}:
            return None
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        value = float(text)
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else round(number, 4)


def jsonstat_series(payload, dimension: str, selections: list[str]):
    """Return selected series from both JSON-stat 1 and JSON-stat 2 layouts."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"Uventet JSON-stat-svartype: {type(payload).__name__}")

    dataset = payload.get("dataset", payload)
    dimensions = dataset.get("dimension")
    if not isinstance(dimensions, dict):
        raise RuntimeError(
            f"JSON-stat-svaret mangler dimension. Nøgler: {sorted(dataset)[:20]}"
        )

    # Statistikbankens JSONSTAT-format placerer id og size under dimension,
    # mens JSON-stat 2 normalt placerer dem på datasættets øverste niveau.
    ids = dataset.get("id") or dimensions.get("id")
    sizes = dataset.get("size") or dimensions.get("size")
    values = dataset.get("value")
    if not isinstance(ids, list) or not isinstance(sizes, list):
        raise RuntimeError(
            f"JSON-stat-svaret mangler id/size. Dataset-nøgler: {sorted(dataset)[:20]}; "
            f"dimension-nøgler: {sorted(dimensions)[:20]}"
        )

    time_dimension = next((item for item in ids if str(item).lower() == "tid"), None)
    selected_dimension = next(
        (item for item in ids if str(item).lower() == dimension.lower()),
        None,
    )
    if time_dimension is None or selected_dimension is None:
        raise RuntimeError(
            f"JSON-stat-svaret mangler dimensionerne Tid og {dimension}: {ids}"
        )

    time_positions = category_positions(dimensions[time_dimension]["category"])
    selection_positions = category_positions(dimensions[selected_dimension]["category"])
    labels = [code for code, _ in sorted(time_positions.items(), key=lambda item: item[1])]

    def flat_index(coordinates: list[int]) -> int:
        index = 0
        for coordinate, size in zip(coordinates, sizes):
            index = index * int(size) + coordinate
        return index

    result = {}
    for selection in selections:
        if selection not in selection_positions:
            raise RuntimeError(
                f"Statistikbanken returnerede ikke kode {selection} i {dimension}. "
                f"Tilgængelige koder: {list(selection_positions)[:30]}"
            )
        series = []
        for period in labels:
            coordinates = []
            for item in ids:
                if item == selected_dimension:
                    coordinates.append(selection_positions[selection])
                elif item == time_dimension:
                    coordinates.append(time_positions[period])
                else:
                    coordinates.append(0)
            series.append(jsonstat_number(values, flat_index(coordinates)))
        result[selection] = series
    return labels, result, dataset


def refresh_statbank(data):
    successes: list[str] = []
    failures: list[str] = []
    official = data.setdefault("meta", {}).setdefault("officialApi", {})
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))

    specs = [
        {
            "label": "Danmarks Statistik - lønmodtagere (LBESK104)",
            "table": "LBESK104",
            "dimension": "SEKTOR",
            "selections": ["1000", "1032", "1046"],
            "target": "wages",
            "mapping": {"total": "1000", "public": "1032", "private": "1046"},
            "meta": {
                "unit": "personer",
                "seasonalAdjustment": "sæsonkorrigeret",
            },
        },
        {
            "label": "Danmarks Statistik - konkurser (KONK3)",
            "table": "KONK3",
            "dimension": "BNØGLE",
            "selections": ["A", "A1", "A2"],
            "target": "bankruptcies",
            "mapping": {"bankruptcies": "A", "seasonal": "A1", "lostJobs": "A2"},
            "meta": {
                "unit": "antal og tabte job",
                "seasonalAdjustment": "kun serie A1",
            },
        },
        {
            "label": "Danmarks Statistik - forbrugertillid (FORV1)",
            "table": "FORV1",
            "dimension": "INDIKATOR",
            "selections": ["F1"],
            "target": "confidence",
            "mapping": {"value": "F1"},
            "meta": {
                "unit": "nettotal",
                "seasonalAdjustment": "ikke oplyst",
            },
        },
    ]

    for spec in specs:
        try:
            payload = statbank_get(
                spec["table"],
                {
                    spec["dimension"]: spec["selections"],
                    "Tid": ["*"],
                },
            )
            labels, series, dataset = jsonstat_series(
                payload,
                spec["dimension"],
                spec["selections"],
            )
            if not labels:
                raise RuntimeError("Statistikbanken returnerede ingen perioder")
            data[spec["target"]] = {
                "labels": labels,
                **{
                    target_key: series[source_key]
                    for target_key, source_key in spec["mapping"].items()
                },
            }
            official_key = {
                "wages": "wages",
                "bankruptcies": "bankruptcies",
                "confidence": "consumerConfidence",
            }[spec["target"]]
            official[official_key] = {
                "source": "Danmarks Statistik",
                "dataset": spec["table"],
                "filters": {spec["dimension"]: spec["selections"]},
                **spec["meta"],
                "sourceUpdated": dataset.get("updated"),
                "latestPeriod": labels[-1],
                "fetchedAt": now.isoformat(timespec="seconds"),
            }
            successes.append(spec["label"])
            print(
                f"Færdig: {spec['label']}. Seneste periode: {labels[-1]}. "
                f"Kilden er opdateret: {dataset.get('updated', 'ikke oplyst')}.",
                flush=True,
            )
        except Exception as exc:
            failures.append(f"{spec['label']}: {exc}")
            print(f"ADVARSEL: {spec['label']} blev ikke opdateret: {exc}", flush=True)

    return successes, failures


def normalized_text(value) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(character for character in text if not unicodedata.combining(character)).lower()
    return text.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def discover_recruitment_table():
    catalog = builder.jobindsats_get("tables?format=json")
    needle = "virksomhedernes forgaeves rekrutteringer"
    for item in walk_dicts(catalog):
        searchable = " ".join(
            normalized_text(value)
            for value in item.values()
            if isinstance(value, (str, int, float))
        )
        if needle not in searchable:
            continue
        candidates = [
            str(value)
            for value in item.values()
            if isinstance(value, str) and re.fullmatch(r"y[0-9a-z]+", value.lower())
        ]
        if candidates:
            table_id = candidates[0]
            return table_id, builder.jobindsats_get(f"table/{table_id}?format=json")
    raise RuntimeError(
        "Jobindsats-tabelkataloget indeholder ikke målingen "
        "Virksomhedernes forgæves rekrutteringer"
    )


def hierarchy_identifiers(metadata) -> set[str]:
    identifiers: set[str] = set()
    for item in walk_dicts(metadata):
        for key, value in item.items():
            for candidate in (key, value):
                if isinstance(candidate, str) and re.fullmatch(r"_[A-Za-z0-9_]+", candidate):
                    identifiers.add(candidate)
    return identifiers


def occupation_hierarchy(metadata, identifiers: set[str]) -> str:
    for item in walk_dicts(metadata):
        searchable = normalized_text(json.dumps(item, ensure_ascii=False))
        if "stillingsbetegn" not in searchable:
            continue
        local = {
            str(candidate)
            for key, value in item.items()
            for candidate in (key, value)
            if isinstance(candidate, str) and re.fullmatch(r"_[A-Za-z0-9_]+", candidate)
        }
        preferred = sorted(local, key=lambda value: ("stilling" not in value.lower(), len(value)))
        if preferred:
            return preferred[0]
    preferred = sorted(
        (value for value in identifiers if "stilling" in value.lower()),
        key=len,
    )
    if preferred:
        return preferred[0]
    raise RuntimeError("Kunne ikke finde hierarkiet for stillingsbetegnelser i tabelmetadata")


def recruitment_data_paths(table_id: str, metadata) -> dict[str, str]:
    identifiers = hierarchy_identifiers(metadata)
    area = next(
        (candidate for candidate in ("_nykom", "_reko", "_region") if candidate in identifiers),
        None,
    )
    if area is None:
        raise RuntimeError(
            "Kunne ikke finde et områdehierarki i tabelmetadata. "
            f"Fundne hierarkier: {sorted(identifiers)}"
        )
    occupation = occupation_hierarchy(metadata, identifiers)
    base = f"data/{table_id}?mgroup.*=*&period.M=latest:120&hierarchy.{area}=/"
    return {
        "national": f"{base}&format=json",
        "occupations": f"{base}&hierarchy.{occupation}=*&format=json",
    }


def find_record_column(records: list[dict], include: tuple[str, ...], exclude: tuple[str, ...] = ()) -> str:
    columns = list(records[0]) if records else []
    for column in columns:
        normalized = normalized_text(column)
        if all(term in normalized for term in include) and not any(term in normalized for term in exclude):
            return column
    raise RuntimeError(
        f"Kunne ikke finde kolonnen med {include}. Tilgængelige kolonner: {columns}"
    )


def merge_work_sharing(target: dict, records: list[dict]) -> None:
    type_column = find_record_column(records, ("arbejdsfordelingstype",))
    value_column = find_record_column(records, ("antal", "person"))
    combined = {
        str(period): {
            "under13Weeks": target.get("under13Weeks", [None] * len(target["labels"]))[index],
            "over13Weeks": target.get("over13Weeks", [None] * len(target["labels"]))[index],
        }
        for index, period in enumerate(target["labels"])
    }
    for record in records:
        period = str(record["Periode"])
        category = normalized_text(record.get(type_column, ""))
        if "op til 13" in category or "under 13" in category:
            key = "under13Weeks"
        elif "over 13" in category:
            key = "over13Weeks"
        else:
            raise RuntimeError(f"Ukendt arbejdsfordelingstype: {record.get(type_column)!r}")
        combined.setdefault(period, {})[key] = builder.api_number(record.get(value_column))

    labels = sorted(combined)
    target["labels"] = labels
    for key in ("under13Weeks", "over13Weeks"):
        target[key] = [combined[period].get(key) for period in labels]
    target["total"] = [
        sum(value for value in values if value is not None) if any(value is not None for value in values) else None
        for values in zip(target["under13Weeks"], target["over13Weeks"])
    ]
    target.pop("kpi", None)


def merge_expired_benefits(target: dict, records: list[dict]) -> None:
    category_column = find_record_column(records, ("dimittend",))
    value_column = find_record_column(
        records,
        ("antal", "person", "opbrugt", "dagpengeret"),
    )
    category_keys = {
        "ledige i alt": "total",
        "1 ars dimittendret": "oneYearGraduate",
        "oevrige dimittender": "otherGraduates",
        "oevrige ledige": "otherUnemployed",
    }
    series_keys = tuple(category_keys.values())
    combined = {
        str(period): {
            key: target.get(key, [None] * len(target["labels"]))[index]
            for key in series_keys
        }
        for index, period in enumerate(target["labels"])
    }
    received = set()
    for record in records:
        category = normalized_text(record.get(category_column, "")).strip()
        key = category_keys.get(category)
        if key is None:
            raise RuntimeError(
                f"Ukendt dimittendkategori for opbrugt dagpengeret: "
                f"{record.get(category_column)!r}"
            )
        received.add(key)
        period = str(record["Periode"])
        combined.setdefault(period, {})[key] = builder.api_number(record.get(value_column))

    missing = set(series_keys) - received
    if missing:
        raise RuntimeError(
            "Jobindsats returnerede ikke alle forventede dimittendkategorier: "
            + ", ".join(sorted(missing))
        )

    labels = sorted(combined)
    target["labels"] = labels
    for key in series_keys:
        target[key] = [combined[period].get(key) for period in labels]
    target.pop("kpi", None)


def update_failed_recruitment(
    target: dict,
    national_records: list[dict],
    occupation_records: list[dict],
) -> None:
    attempts_column = find_record_column(
        national_records,
        ("forgaeves", "rekrutteringsforsoeg"),
        ("rate",),
    )
    rate_column = find_record_column(
        national_records,
        ("forgaeves", "rekrutteringsrate"),
    )
    previous_attempts = target.get("attempts", target.get("notFilled", []))
    previous_rate = target.get("rate", target.get("frr", []))
    combined = {
        str(period): {
            "attempts": previous_attempts[index] if index < len(previous_attempts) else None,
            "rate": previous_rate[index] if index < len(previous_rate) else None,
        }
        for index, period in enumerate(target.get("labels", []))
    }
    for record in national_records:
        period = str(record["Periode"])
        rate_value = builder.api_number(record.get(rate_column))
        if rate_value is not None and abs(rate_value) <= 1:
            rate_value = round(rate_value * 100, 4)
        combined[period] = {
            "attempts": builder.api_number(record.get(attempts_column)),
            "rate": rate_value,
        }

    labels = sorted(combined)
    target["labels"] = labels
    target["attempts"] = [combined[period].get("attempts") for period in labels]
    target["rate"] = [combined[period].get("rate") for period in labels]
    target.pop("notFilled", None)
    target.pop("frr", None)
    target.pop("kpi", None)

    occupation_column = find_record_column(occupation_records, ("stillingsbetegn",))
    occupation_attempts_column = find_record_column(
        occupation_records,
        ("forgaeves", "rekrutteringsforsoeg"),
        ("rate",),
    )
    latest_period = max(str(record["Periode"]) for record in occupation_records)
    occupations = []
    for record in occupation_records:
        if str(record["Periode"]) != latest_period:
            continue
        raw_name = str(record.get(occupation_column, "")).strip()
        match = re.fullmatch(r"\(\d{4}(?:\.\d+)?\)\s*(.+)", raw_name)
        if not match:
            continue
        occupations.append(
            {
                "name": match.group(1).strip(),
                "value": builder.api_number(record.get(occupation_attempts_column)),
            }
        )
    ranked = sorted(
        occupations,
        key=lambda item: item["value"] if item["value"] is not None else -1,
        reverse=True,
    )
    ranked = [item for item in ranked if item["name"] and item["value"] is not None][:15]
    if len(ranked) < 15:
        raise RuntimeError(f"Jobindsats returnerede kun {len(ranked)} anvendelige stillinger")
    target["topPeriod"] = latest_period
    target["topOccupations"] = ranked


def refresh_jobindsats(data):
    successes: list[str] = []
    failures: list[str] = []

    def fetch(label: str, path: str):
        try:
            records = builder.jobindsats_records(path)
        except Exception as exc:
            failures.append(f"{label}: {exc}")
            print(f"ADVARSEL: {label} blev ikke opdateret: {exc}", flush=True)
            return None
        successes.append(label)
        return records

    common = "mgroup.*=*&period.M=latest:120"
    for key, label, group in (
        ("total", "Bruttoledighed i alt", "/"),
        ("benefit", "A-dagpenge", "/2/"),
        ("assistance", "Kontanthjælp", "/3/"),
    ):
        records = fetch(
            label,
            f"data/y25i03?{common}&hierarchy._hele_landet=/"
            f"&hierarchy._ygrpi09={group}&format=json",
        )
        if records is not None:
            mapping = {key: "Sæsonkorrigeret antal ledige fuldtidspersoner"}
            if key == "total":
                mapping["rate"] = "Sæsonkorrigeret fuldtidspersoner i pct. af arbejdsstyrken"
            builder.merge_series(data["unemployment"], records, mapping)

    records = fetch(
        "Nyopslåede stillinger",
        f"data/y25i07?{common}&hierarchy._nykom=/&format=json",
    )
    if records is not None:
        builder.merge_series(data["vacancies"], records, {"values": "Antal nyopslåede stillinger"})

    for key, label, group in (
        ("total", "Langtidsledige i alt", "/"),
        ("benefit", "Langtidsledige på A-dagpenge", "/2/"),
        ("assistance", "Langtidsledige på kontanthjælp", "/3/"),
    ):
        records = fetch(
            label,
            f"data/y25i09?{common}&hierarchy._nykom=/"
            f"&hierarchy._ygrpi09={group}&format=json",
        )
        if records is not None:
            builder.merge_series(
                data["longterm"],
                records,
                {key: "Antal langtidsledige fuldtidspersoner"},
            )

    records = fetch(
        "Varslede afskedigelser",
        f"data/y25i05?{common}&hierarchy._nykom=/&format=json",
    )
    if records is not None:
        builder.merge_series(
            data["notices"],
            records,
            {
                "people": "Varslinger, antal personer",
                "companies": "Varslinger, antal virksomheder",
            },
        )

    records = fetch(
        "Arbejdsfordelinger",
        f"data/y25i06?{common}&hierarchy._nykom=/"
        "&hierarchy._var13uger=*&format=json",
    )
    if records is not None:
        merge_work_sharing(data["workSharing"], records)

    records = fetch(
        "Opbrugt dagpengeret",
        f"data/y01ud01di?{common}&hierarchy._hele_landet=/"
        "&hierarchy._akassedp=/&hierarchy._dimittend=*"
        "&hierarchy._forlang=/&format=json",
    )
    if records is not None:
        merge_expired_benefits(data["expiredBenefits"], records)

    recruitment_table_id = None
    try:
        recruitment_table_id, recruitment_metadata = discover_recruitment_table()
        recruitment_paths = recruitment_data_paths(recruitment_table_id, recruitment_metadata)
        national_records = builder.jobindsats_records(recruitment_paths["national"])
        occupation_records = builder.jobindsats_records(recruitment_paths["occupations"])
        update_failed_recruitment(
            data["failedRecruitment"],
            national_records,
            occupation_records,
        )
        successes.append("Forgæves rekrutteringsforsøg")
        print(
            "Færdig: Forgæves rekrutteringsforsøg. "
            f"Jobindsats-tabel: {recruitment_table_id}.",
            flush=True,
        )
    except Exception as exc:
        failures.append(f"Forgæves rekrutteringsforsøg: {exc}")
        print(
            f"ADVARSEL: Forgæves rekrutteringsforsøg blev ikke opdateret: {exc}",
            flush=True,
        )

    data.setdefault("meta", {}).setdefault("officialApi", {})["jobindsats"] = {
        "source": "Jobindsats",
        "version": "v3",
        "periodsFetchedPerRun": MONTH_LIMIT,
        "tables": {
            "unemployment": "y25i03",
            "vacancies": "y25i07",
            "longTermUnemployment": "y25i09",
            "notices": "y25i05",
            "workSharing": "y25i06",
            "expiredBenefits": "y01ud01di",
            "failedRecruitment": recruitment_table_id or "automatisk opslag",
        },
        "area": "Hele landet",
        "benefitGroups": {
            "total": "/",
            "A-dagpenge": "/2/",
            "Kontanthjælp": "/3/",
        },
    }

    source_keys = {
        "Bruttoledighed i alt": "unemployment",
        "Nyopslåede stillinger": "vacancies",
        "Langtidsledige i alt": "longTermUnemployment",
        "Varslede afskedigelser": "notices",
        "Arbejdsfordelinger": "workSharing",
        "Opbrugt dagpengeret": "expiredBenefits",
        "Forgæves rekrutteringsforsøg": "failedRecruitment",
    }
    for label, key in source_keys.items():
        if label in successes and key in data["meta"].get("sourceRegister", {}):
            data["meta"]["sourceRegister"][key]["source"] = "Jobindsats API v3"

    now = datetime.now(ZoneInfo("Europe/Copenhagen")).isoformat(timespec="seconds")
    source_status = data.setdefault("meta", {}).setdefault("sourceStatus", {})
    for label, key, dataset, target in (
        ("Arbejdsfordelinger", "workSharing", "y25i06", data["workSharing"]),
        (
            "Opbrugt dagpengeret",
            "expiredBenefits",
            "y01ud01di",
            data["expiredBenefits"],
        ),
        (
            "Forgæves rekrutteringsforsøg",
            "failedRecruitment",
            recruitment_table_id or "automatisk opslag",
            data["failedRecruitment"],
        ),
    ):
        latest_period = target.get("topPeriod") or target.get("labels", [None])[-1]
        source_status[key] = {
            "state": "ok" if label in successes else "stale",
            "latestPeriod": latest_period,
            "checkedAt": now,
            "source": "Jobindsats API v3",
            "dataset": dataset,
        }
    data["meta"]["jobindsatsUpdateStatus"] = {
        "successful": successes,
        "failed": failures,
    }
    return successes, failures


def set_status(data, successes: list[str], failures: list[str]) -> None:
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    state = "ok" if not failures else ("partial" if successes else "stale")
    data.setdefault("meta", {})["updateStatus"] = {
        "state": state,
        "successful": successes,
        "failed": failures,
        "checkedAt": now.isoformat(timespec="seconds"),
    }
    if successes:
        months = [
            "januar", "februar", "marts", "april", "maj", "juni",
            "juli", "august", "september", "oktober", "november", "december",
        ]
        data["meta"]["updated"] = f"{now.day}. {months[now.month - 1]} {now.year}"
        data["meta"]["retrievedAt"] = now.date().isoformat()


def add_visible_status(html: str, data: dict) -> str:
    status = data.get("meta", {}).get("updateStatus", {})
    state = status.get("state")
    if state == "ok":
        return html

    if state == "partial":
        failed_labels = [item.split(":", 1)[0] for item in status.get("failed", [])]
        message = "Delvist opdateret. Viser seneste gyldige data for: " + ", ".join(failed_labels) + "."
        background, border = "#E4EDE6", "#6B9E78"
    else:
        message = "Seneste dataopdatering fejlede. Dashboardet viser seneste gyldige data."
        background, border = "#E4EDE6", "#3D6B47"

    banner = (
        f'<div role="status" style="margin:0 0 24px;padding:12px 14px;'
        f'background:{background};border-left:4px solid {border};'
        f'font-size:13px;line-height:1.45">{message}</div>'
    )
    marker = "  </div>\n\n  <h2>Ledighed og beskæftigelse</h2>"
    if marker not in html:
        raise RuntimeError("Kunne ikke placere opdateringsstatus i dashboardets HTML")
    return html.replace(
        marker,
        f"  </div>\n  {banner}\n\n  <h2>Ledighed og beskæftigelse</h2>",
        1,
    )


def main() -> int:
    builder.jobindsats_get = robust_jobindsats_get
    if not builder.DATA_OUTPUT.exists():
        raise RuntimeError("data/dashboard-data.json mangler")

    data = json.loads(builder.DATA_OUTPUT.read_text(encoding="utf-8"))
    print("Starter opdatering fra Danmarks Statistik.", flush=True)
    stat_successes, stat_failures = refresh_statbank(data)
    print("Starter opdatering fra Jobindsats.", flush=True)
    job_successes, job_failures = refresh_jobindsats(data)
    set_status(data, stat_successes + job_successes, stat_failures + job_failures)

    html = add_visible_status(builder.build_html(data), data)
    builder.validate(data, html)
    builder.OUTPUT.write_text(html, encoding="utf-8")
    builder.DATA_OUTPUT.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {builder.OUTPUT} ({builder.OUTPUT.stat().st_size:,} bytes)")

    if data.get("meta", {}).get("updateStatus", {}).get("state") == "stale":
        print("Alle eksterne datakald fejlede. Seneste gyldige data er bevaret.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
