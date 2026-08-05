#!/usr/bin/env python3
"""Robust entrypoint for updating the labour-market dashboard."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import build_dashboard as builder


MONTH_LIMIT = 24
REQUEST_TIMEOUT = 50
MAX_ATTEMPTS = 2


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
            "User-Agent": "Danske-A-kasser-dashboard/2.0",
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

    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    state = "ok" if not failures else ("partial" if successes else "stale")
    status = {
        "state": state,
        "successful": successes,
        "failed": failures,
    }
    if state != "ok":
        status["checkedAt"] = now.isoformat(timespec="seconds")
    data.setdefault("meta", {})["updateStatus"] = status

    if successes:
        months = [
            "januar", "februar", "marts", "april", "maj", "juni",
            "juli", "august", "september", "oktober", "november", "december",
        ]
        data["meta"]["updated"] = f"{now.day}. {months[now.month - 1]} {now.year}"
        data["meta"]["retrievedAt"] = now.date().isoformat()

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

    return data


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
            "Seneste Jobindsats-opdatering fejlede. "
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


def main() -> int:
    builder.jobindsats_get = robust_jobindsats_get
    builder.refresh_jobindsats = refresh_jobindsats_resilient

    builder.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data = builder.build_data()
    html = add_visible_status(builder.build_html(data), data)
    builder.validate(data, html)
    builder.OUTPUT.write_text(html, encoding="utf-8")
    builder.DATA_OUTPUT.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {builder.OUTPUT} ({builder.OUTPUT.stat().st_size:,} bytes)")

    if data.get("meta", {}).get("updateStatus", {}).get("state") == "stale":
        print("Alle Jobindsats-kald fejlede. Seneste gyldige data er bevaret.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
