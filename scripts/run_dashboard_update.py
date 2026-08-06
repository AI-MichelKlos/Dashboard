#!/usr/bin/env python3
"""Robust entrypoint for updating the labour-market dashboard."""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import build_dashboard as builder


MONTH_LIMIT = 24
REQUEST_TIMEOUT = 60
MAX_ATTEMPTS = 3
STATBANK_API_URL = "https://api.statbank.dk/v1/data"


def compact_period_request(path: str) -> str:
    """Fetch only recent periods; merge_series keeps the local history."""
    return re.sub(
        r"period\.M=latest:\d+",
        f"period.M=latest:{MONTH_LIMIT}",
        path,
        count=1,
    )


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
            "User-Agent": "Danske-A-kasser-dashboard/3.0",
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
            pause = attempt * 15
            print(
                f"Jobindsats-kald fejlede, forsøg {attempt} af {MAX_ATTEMPTS}. "
                f"Prøver igen om {pause} sekunder: {request_path}",
                flush=True,
            )
            time.sleep(pause)

    raise RuntimeError(
        f"Jobindsats-kald fejlede efter {MAX_ATTEMPTS} forsøg: {request_path}: {last_error}"
    ) from last_error


def statbank_get(table: str, variables: dict[str, list[str]]):
    """Fetch a complete official Statbank extract as JSON-stat."""
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
            "User-Agent": "Danske-A-kasser-dashboard/3.0",
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
            pause = attempt * 15
            print(
                f"Statistikbank-kald til {table} fejlede, forsøg {attempt} af "
                f"{MAX_ATTEMPTS}. Prøver igen om {pause} sekunder: {last_error}",
                flush=True,
            )
            time.sleep(pause)

    raise RuntimeError(
        f"Statistikbank-kald til {table} fejlede efter {MAX_ATTEMPTS} forsøg: {last_error}"
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


def jsonstat_value(values, index: int):
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
    """Convert selected JSON-stat series into aligned period lists."""
    dataset = payload.get("dataset", payload)
    dimensions = dataset.get("dimension")
    ids = dataset.get("id")
    sizes = dataset.get("size")
    values = dataset.get("value")
    if not isinstance(dimensions, dict) or not isinstance(ids, list) or not isinstance(sizes, list):
        raise RuntimeError("Statistikbanken svarede ikke med forventet JSON-stat-struktur")

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
                f"Statistikbanken returnerede ikke den valgte kode {selection} i "
                f"dimension {dimension}"
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
            series.append(jsonstat_value(values, flat_index(coordinates)))
        result[selection] = series
    return labels, result, dataset


def refresh_statbank_resilient(data):
    successes: list[str] = []
    failures: list[str] = []
    official = data.setdefault("meta", {}).setdefault("officialApi", {})
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))

    def fetch(label: str, table: str, dimension: str, selections: list[str]):
        try:
            payload = statbank_get(
                table,
                {
                    dimension: selections,
                    "Tid": ["*"],
                },
            )
            labels, series, dataset = jsonstat_series(payload, dimension, selections)
            if not labels:
                raise RuntimeError("Statistikbanken returnerede ingen perioder")
        except Exception as exc:
            failures.append(f"{label}: {exc}")
            print(f"ADVARSEL: {label} blev ikke opdateret: {exc}", flush=True)
            return None

        successes.append(label)
        print(
            f"Færdig: {label}. Seneste periode: {labels[-1]}. "
            f"Kilden er opdateret: {dataset.get('updated', 'ikke oplyst')}.",
            flush=True,
        )
        return labels, series, dataset

    result = fetch(
        "Danmarks Statistik - lønmodtagere (LBESK104)",
        "LBESK104",
        "SEKTOR",
        ["1000", "1032", "1046"],
    )
    if result is not None:
        labels, series, dataset = result
        data["wages"] = {
            "labels": labels,
            "total": series["1000"],
            "public": series["1032"],
            "private": series["1046"],
        }
        official["wages"] = {
            "source": "Danmarks Statistik",
            "dataset": "LBESK104",
            "filters": {"SEKTOR": ["1000", "1032", "1046"]},
            "unit": "personer",
            "seasonalAdjustment": "sæsonkorrigeret",
            "sourceUpdated": dataset.get("updated"),
            "latestPeriod": labels[-1],
            "fetchedAt": now.isoformat(timespec="seconds"),
        }

    result = fetch(
        "Danmarks Statistik - konkurser (KONK3)",
        "KONK3",
        "BNØGLE",
        ["A", "A1", "A2"],
    )
    if result is not None:
        labels, series, dataset = result
        data["bankruptcies"] = {
            "labels": labels,
            "bankruptcies": series["A"],
            "seasonal": series["A1"],
            "lostJobs": series["A2"],
        }
        official["bankruptcies"] = {
            "source": "Danmarks Statistik",
            "dataset": "KONK3",
            "filters": {"BNØGLE": ["A", "A1", "A2"]},
            "unit": "antal og tabte job",
            "seasonalAdjustment": "kun serie A1",
            "sourceUpdated": dataset.get("updated"),
            "latestPeriod": labels[-1],
            "fetchedAt": now.isoformat(timespec="seconds"),
        }

    result = fetch(
        "Danmarks Statistik - forbrugertillid (FORV1)",
        "FORV1",
        "INDIKATOR",
        ["F1"],
    )
    if result is not None:
        labels, series, dataset = result
        data["confidence"] = {
            "labels": labels,
            "value": series["F1"],
        }
        official["consumerConfidence"] = {
            "source": "Danmarks Statistik",
            "dataset": "FORV1",
            "filters": {"INDIKATOR": ["F1"]},
            "unit": "nettotal",
            "seasonalAdjustment": "ikke oplyst",
            "sourceUpdated": dataset.get("updated"),
            "latestPeriod": labels[-1],
            "fetchedAt": now.isoformat(timespec="seconds"),
        }

    return successes, failures


def refresh_jobindsats_resilient(data):
    failures: list[str] = []
    successes: list[str] = []

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
    unemployment = {
        "total": ("Bruttoledighed i alt", "/"),
        "benefit": ("A-dagpenge", "/2/"),
        "assistance": ("Kontanthjælp", "/3/"),
    }
    for key, (label, group) in unemployment.items():
        records = fetch(
            label,
            f"data/y25i03?{common}&hierarchy._hele_landet=/"
            f"&hierarchy._ygrpi09={group}&format=json",
        )
        if records is not None:
            builder.merge_series(
                data["unemployment"],
                records,
                {
                    key: "Sæsonkorrigeret antal ledige fuldtidspersoner",
                    **(
                        {"rate": "Sæsonkorrigeret fuldtidspersoner i pct. af arbejdsstyrken"}
                        if key == "total"
                        else {}
                    ),
                },
            )

    records = fetch(
        "Nyopslåede stillinger",
        f"data/y25i07?{common}&hierarchy._nykom=/&format=json",
    )
    if records is not None:
        builder.merge_series(data["vacancies"], records, {"values": "Antal nyopslåede stillinger"})

    longterm = {
        "total": ("Langtidsledige i alt", "/"),
        "benefit": ("Langtidsledige på A-dagpenge", "/2/"),
        "assistance": ("Langtidsledige på kontanthjælp", "/3/"),
    }
    for key, (label, group) in longterm.items():
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

    data.setdefault("meta", {})["jobindsatsUpdateStatus"] = {
        "successful": successes,
        "failed": failures,
    }
    data["meta"].setdefault("officialApi", {})["jobindsats"] = {
        "source": "Jobindsats",
        "version": "v3",
        "periodsFetchedPerRun": MONTH_LIMIT,
        "tables": {
            "unemployment": "y25i03",
            "vacancies": "y25i07",
            "longTermUnemployment": "y25i09",
            "notices": "y25i05",
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
    }
    for label, key in source_keys.items():
        if label in successes and key in data["meta"].get("sourceRegister", {}):
            data["meta"]["sourceRegister"][key]["source"] = "Jobindsats API v3"

    return successes, failures


def set_combined_status(data, statbank_successes, statbank_failures, job_successes, job_failures):
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    successes = statbank_successes + job_successes
    failures = statbank_failures + job_failures
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
        message = (
            "Delvist opdateret. Viser seneste gyldige data for: "
            + ", ".join(failed_labels)
            + "."
        )
        background = "#fff4d6"
        border = "#ef8b2c"
    else:
        message = (
            "Seneste dataopdatering fejlede. "
            "Dashboardet viser seneste gyldige data."
        )
        background = "#fde9e8"
        border = "#e34a45"

    banner = (
        f'<div role="status" style="margin:0 0 24px;padding:12px 14px;'
        f'background:{background};border-left:4px solid {border};'
        'font-size:13px;line-height:1.45">'
        f"{message}</div>"
    )
    marker = "  </div>\n\n  <h2>Ledighed og beskæftigelse</h2>"
    if marker not in html:
        raise RuntimeError("Kunne ikke placere opdateringsstatus i dashboardets HTML")
    return html.replace(
        marker,
        f"  </div>\n  {banner}\n\n  <h2>Ledighed og beskæftigelse</h2>",
        1,
    )


def load_existing_data():
    if not builder.DATA_OUTPUT.exists():
        raise RuntimeError(
            "data/dashboard-data.json mangler. Den automatiske opdatering kræver en eksisterende "
            "valideret datafil som fallback."
        )
    return json.loads(builder.DATA_OUTPUT.read_text(encoding="utf-8"))


def main() -> int:
    builder.jobindsats_get = robust_jobindsats_get

    builder.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load_existing_data()

    print("Starter opdatering fra Danmarks Statistik.", flush=True)
    statbank_successes, statbank_failures = refresh_statbank_resilient(data)
    print("Starter opdatering fra Jobindsats.", flush=True)
    job_successes, job_failures = refresh_jobindsats_resilient(data)
    set_combined_status(
        data,
        statbank_successes,
        statbank_failures,
        job_successes,
        job_failures,
    )

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
