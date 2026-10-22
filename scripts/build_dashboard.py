from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


BASE = Path(__file__).resolve().parents[1]
INPUT = BASE / "Samlet overblik.xlsx"
OUTPUT_DIR = BASE
OUTPUT = BASE / "index.html"
DATA_OUTPUT = BASE / "data/dashboard-data.json"
DATA_DIR = BASE / "data"
JOBINDSATS_API_ROOT = "https://api.jobindsats.dk/v3"


def number(value):
    if value is None or pd.isna(value):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return int(value) if value.is_integer() else round(value, 4)


def json_safe(value):
    """Convert non-finite scalar values to JSON-safe nulls recursively."""
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def api_number(value):
    """Parse numbers returned by the Danish Jobindsats API."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return number(value)
    text = str(value).strip().replace("\xa0", "").replace(" ", "")
    if not text or text in {"-", "..", "null", "None"}:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", text):
        text = text.replace(".", "")
    try:
        return number(float(text))
    except ValueError as exc:
        raise ValueError(f"Uventet talformat fra Jobindsats: {value!r}") from exc


def jobindsats_get(path):
    token = os.environ.get("JOBINDSATS_API_TOKEN")
    if not token:
        raise RuntimeError("JOBINDSATS_API_TOKEN mangler")
    request = urllib.request.Request(
        f"{JOBINDSATS_API_ROOT}/{path.lstrip('/')}",
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
        raise RuntimeError(f"Jobindsats returnerede HTTP {exc.code}: {detail[:300]}") from exc


def jobindsats_records(path):
    response = jobindsats_get(path)
    columns = response.get("columns")
    rows = response.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise RuntimeError("Jobindsats svarede ikke med forventede columns og rows")
    return [dict(zip(columns, row)) for row in rows]


def merge_series(target, records, mapping):
    """Merge API records into an aligned dashboard series without losing history."""
    combined = {
        str(period): {
            key: values[index] if index < len(values) else None
            for key, values in target.items()
            if key != "labels"
        }
        for index, period in enumerate(target["labels"])
    }
    for record in records:
        period = str(record["Periode"])
        combined.setdefault(period, {})
        for key, column in mapping.items():
            combined[period][key] = api_number(record.get(column))
    labels = sorted(combined)
    target["labels"] = labels
    for key in mapping:
        target[key] = [combined[period].get(key) for period in labels]


def refresh_jobindsats(data):
    common = "mgroup.*=*&period.M=latest:120"
    unemployment = {
        "total": "/",
        "benefit": "/2/",
        "assistance": "/3/",
    }
    for key, group in unemployment.items():
        records = jobindsats_records(
            f"data/y25i03?{common}&hierarchy._hele_landet=/"
            f"&hierarchy._ygrpi09={group}&format=json"
        )
        merge_series(
            data["unemployment"],
            records,
            {
                key: "Sæsonkorrigeret antal ledige fuldtidspersoner",
                **({"rate": "Sæsonkorrigeret fuldtidspersoner i pct. af arbejdsstyrken"}
                   if key == "total" else {}),
            },
        )

    merge_series(
        data["vacancies"],
        jobindsats_records(
            f"data/y25i07?{common}&hierarchy._nykom=/&format=json"
        ),
        {"values": "Antal nyopslåede stillinger"},
    )

    longterm = {
        "total": "/",
        "benefit": "/2/",
        "assistance": "/3/",
    }
    for key, group in longterm.items():
        records = jobindsats_records(
            f"data/y25i09?{common}&hierarchy._nykom=/"
            f"&hierarchy._ygrpi09={group}&format=json"
        )
        merge_series(
            data["longterm"],
            records,
            {key: "Antal langtidsledige fuldtidspersoner"},
        )

    merge_series(
        data["notices"],
        jobindsats_records(
            f"data/y25i05?{common}&hierarchy._nykom=/&format=json"
        ),
        {
            "people": "Varslinger, antal personer",
            "companies": "Varslinger, antal virksomheder",
        },
    )

    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    months = [
        "januar", "februar", "marts", "april", "maj", "juni",
        "juli", "august", "september", "oktober", "november", "december",
    ]
    data["meta"]["updated"] = f"{now.day}. {months[now.month - 1]} {now.year}"
    data["meta"]["retrievedAt"] = now.date().isoformat()
    data["meta"]["officialApi"]["jobindsats"] = {
        "source": "Jobindsats",
        "version": "v3",
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
    for key in ("unemployment", "vacancies", "longTermUnemployment", "notices"):
        if key in data["meta"]["sourceRegister"]:
            data["meta"]["sourceRegister"][key]["source"] = "Jobindsats API v3"
    return data


def period_columns(raw: pd.DataFrame, row: int, pattern: str):
    return [
        (col, str(value))
        for col, value in raw.iloc[row].items()
        if re.fullmatch(pattern, str(value))
    ]


def long_sheet(sheet: str, period_col: str = "Periode"):
    df = pd.read_excel(INPUT, sheet_name=sheet)
    df[period_col] = df[period_col].astype(str)
    return df.sort_values(period_col)


def wide_sheet(sheet: str, rows: dict[str, int], period_row: int = 1):
    raw = pd.read_excel(INPUT, sheet_name=sheet, header=None)
    cols = period_columns(raw, period_row, r"\d{4}M\d{2}")
    result = {"labels": [p for _, p in cols]}
    for name, row in rows.items():
        result[name] = [number(raw.iat[row, col]) for col, _ in cols]
    return result


def jsonstat_series(path: Path, dimension: str, selections: list[str]):
    """Return selected JSON-stat series as period-aligned lists."""
    dataset = json.loads(path.read_text(encoding="utf-8"))["dataset"]
    dimensions = dataset["dimension"]
    ids = dimensions["id"]
    sizes = dimensions["size"]
    time_codes = list(dimensions["Tid"]["category"]["index"])
    values = dataset["value"]
    selected = {}

    def flat_index(coords):
        index = 0
        for position, size in zip(coords, sizes):
            index = index * size + position
        return index

    category_index = dimensions[dimension]["category"]["index"]
    for selection in selections:
        series = []
        for time_position, _ in enumerate(time_codes):
            coords = []
            for dim in ids:
                if dim == dimension:
                    coords.append(category_index[selection])
                elif dim == "Tid":
                    coords.append(time_position)
                else:
                    coords.append(0)
            series.append(number(values[flat_index(coords)]))
        selected[selection] = series
    return time_codes, selected, dataset


def latest_valid(labels, values):
    valid = [(str(p), number(v)) for p, v in zip(labels, values) if number(v) is not None]
    return valid[-1]


def year_ago_period(period: str):
    if re.fullmatch(r"\d{4}M\d{2}", period):
        return f"{int(period[:4]) - 1}{period[4:]}"
    if re.fullmatch(r"\d{4}-\d{2}", period):
        return f"{int(period[:4]) - 1}{period[4:]}"
    if re.fullmatch(r"\d{4}-Q[1-4]", period):
        return f"{int(period[:4]) - 1}{period[4:]}"
    return ""


def kpi(labels, values):
    valid = [(str(p), number(v)) for p, v in zip(labels, values) if number(v) is not None]
    period, value = valid[-1]
    lookup = dict(valid)
    previous_period, previous_value = valid[-2] if len(valid) > 1 else ("", None)
    annual_period = year_ago_period(period)
    return {
        "period": period,
        "value": value,
        "previousPeriod": previous_period,
        "previousValue": previous_value,
        "yearPeriod": annual_period,
        "yearValue": lookup.get(annual_period),
    }


def fmt_number(value, decimals=0):
    if value is None:
        return "Ikke oplyst"
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


MONTHS = [
    "jan.", "feb.", "mar.", "apr.", "maj", "jun.",
    "jul.", "aug.", "sep.", "okt.", "nov.", "dec.",
]


def fmt_period(period):
    if re.fullmatch(r"\d{4}M\d{2}", period):
        return f"{MONTHS[int(period[5:7]) - 1]} {period[:4]}"
    if re.fullmatch(r"\d{4}-\d{2}", period):
        return f"{MONTHS[int(period[5:7]) - 1]} {period[:4]}"
    if re.fullmatch(r"\d{4}-Q[1-4]", period):
        return f"{period[-2:]} {period[:4]}"
    return period


def change_html(current, comparison, comparison_period, decimals, good_if_up):
    if current is None or comparison is None:
        return ""
    delta = current - comparison
    if abs(delta) < 10 ** (-(decimals + 1)):
        cls, arrow, meaning = "delta-neutral", "●", "uændret"
    else:
        up = delta > 0
        cls = "delta-good" if up == good_if_up else "delta-bad"
        arrow = "▲" if up else "▼"
        meaning = "bedre" if cls == "delta-good" else "værre"
    return (
        f'<span class="{cls}">{arrow} {fmt_number(abs(delta), decimals)}'
        f'<span class="change-meaning">{meaning}</span></span>'
        f'<span class="compare-label"> siden {fmt_period(comparison_period)}</span>'
    )


def card(title, series, unit="", decimals=0, good_if_up=True, note="", priority=False):
    current = kpi(series["labels"], series["values"])
    suffix = f" {unit}" if unit else ""
    previous = change_html(
        current["value"], current["previousValue"], current["previousPeriod"], decimals, good_if_up
    )
    annual = change_html(
        current["value"], current["yearValue"], current["yearPeriod"], decimals, good_if_up
    )
    rows = []
    if previous:
        rows.append(f'<div class="delta-row">{previous}</div>')
    if annual:
        rows.append(f'<div class="delta-row">{annual}</div>')
    return f"""
      <article class="kpi-card{' priority' if priority else ''}">
        <div class="kpi-title">{title}</div>
        <div class="kpi-value">{fmt_number(current["value"], decimals)}<span>{suffix}</span></div>
        <div class="kpi-period">{fmt_period(current["period"])}</div>
        <div class="kpi-deltas">{''.join(rows)}</div>
        {f'<div class="kpi-note">{note}</div>' if note else ''}
      </article>"""


def insight_card(title, series, unit="", decimals=0, good_if_up=True):
    current = kpi(series["labels"], series["values"])
    comparison = current["yearValue"]
    comparison_period = current["yearPeriod"]
    if comparison is None:
        comparison = current["previousValue"]
        comparison_period = current["previousPeriod"]
    delta = current["value"] - comparison if comparison is not None else None
    if delta is None or abs(delta) < 10 ** (-(decimals + 1)):
        direction = "er uændret"
        tone = "neutral"
        change = ""
    else:
        up = delta > 0
        direction = "er steget" if up else "er faldet"
        tone = "good" if up == good_if_up else "bad"
        change = f" med {fmt_number(abs(delta), decimals)} {unit}".rstrip()
    period_text = f" siden {fmt_period(comparison_period)}" if comparison_period else ""
    value_text = fmt_number(current["value"], decimals)
    value_unit = f" {unit}" if unit else ""
    return f"""
      <article class="insight-card {tone}">
        <div class="insight-head">
          <div class="insight-kicker">{title}</div>
          <div class="insight-period">{fmt_period(current["period"])}</div>
        </div>
        <div class="insight-value">{value_text}<span>{value_unit}</span></div>
        <p>{title} {direction}{change}{period_text}.</p>
      </article>"""


def update_status_banner(data):
    status = data.get("meta", {}).get("updateStatus", {})
    state = status.get("state")
    if not state or state == "ok":
        return ""
    if state == "partial":
        failed_labels = [item.split(":", 1)[0] for item in status.get("failed", [])]
        message = "Delvist opdateret. Viser seneste gyldige data for: " + ", ".join(failed_labels) + "."
        tone = "partial"
    else:
        message = "Seneste dataopdatering fejlede. Dashboardet viser seneste gyldige data."
        tone = "stale"
    return f'<div class="update-status {tone}" role="status">{message}</div>'


def build_data():
    if DATA_OUTPUT.exists():
        data = json.loads(DATA_OUTPUT.read_text(encoding="utf-8"))
        if os.environ.get("JOBINDSATS_API_TOKEN"):
            return refresh_jobindsats(data)
        return data

    ledige = long_sheet("Ledige ")
    labels = ledige["Periode"].tolist()
    unemployment = {
        "labels": labels,
        "total": [number(v) for v in ledige["Ydelsesgrupper i alt - Sæsonkorrigeret antal ledige fuldtidspersoner"]],
        "rate": [number(v) for v in ledige["Ydelsesgrupper i alt - Sæsonkorrigeret fuldtidspersoner i pct. af arbejdsstyrken"]],
        "benefit": [number(v) for v in ledige["A-dagpenge - Sæsonkorrigeret antal ledige fuldtidspersoner"]],
        "assistance": [number(v) for v in ledige["Kontanthjælp - Sæsonkorrigeret antal ledige fuldtidspersoner"]],
    }

    vacancies = long_sheet("Nyopslåede stillinger")
    vacancy_labels = vacancies["Periode"].tolist()
    vacancy_values = [number(v) for v in vacancies["Antal nyopslåede stillinger"]]

    wages = wide_sheet(
        "Lønmodtagere",
        {"total": 2, "public": 3, "private": 4},
    )

    notices = long_sheet("Varslede afskedigelser")
    notice_labels = notices["Periode"].tolist()
    notice_people = [number(v) for v in notices["Varslinger, antal personer"]]
    notice_companies = [number(v) for v in notices["Varslinger, antal virksomheder"]]

    bankruptcies = wide_sheet(
        "Konkurser og tabte job",
        {"bankruptcies": 2, "seasonal": 3, "lostJobs": 4},
    )

    longterm = long_sheet("Langtid opdelt")
    long_labels = longterm["Periode"].tolist()
    long_data = {
        "labels": long_labels,
        "total": [number(v) for v in longterm.iloc[:, 1]],
        "benefit": [number(v) for v in longterm.iloc[:, 2]],
        "assistance": [number(v) for v in longterm.iloc[:, 3]],
    }

    inflation = wide_sheet("Inflation", {"total": 3, "core": 5})
    confidence = wide_sheet("Forbrugerforventninger", {"value": 2})
    business = wide_sheet("Erhvevstillidsindikator", {"value": 2})

    # Replace validated Excel series with fresh official API extracts.
    wage_labels, wage_series, wage_meta = jsonstat_series(
        DATA_DIR / "lbesk104.json", "SEKTOR", ["1000", "1032", "1046"]
    )
    wages = {
        "labels": wage_labels,
        "total": wage_series["1000"],
        "public": wage_series["1032"],
        "private": wage_series["1046"],
    }
    bankruptcy_labels, bankruptcy_series, bankruptcy_meta = jsonstat_series(
        DATA_DIR / "konk3.json", "BNØGLE", ["A", "A1", "A2"]
    )
    bankruptcies = {
        "labels": bankruptcy_labels,
        "bankruptcies": bankruptcy_series["A"],
        "seasonal": bankruptcy_series["A1"],
        "lostJobs": bankruptcy_series["A2"],
    }
    confidence_labels, confidence_series, confidence_meta = jsonstat_series(
        DATA_DIR / "forv1.json", "INDIKATOR", ["F1"]
    )
    confidence = {"labels": confidence_labels, "value": confidence_series["F1"]}

    eurostat_raw = pd.read_excel(INPUT, sheet_name="EUROSTAT kvartalstal for ledige")
    country_col = "geo\\TIME_PERIOD"
    quarter_col = "Kvartal"
    value_col = "Værdi"
    eurostat_raw[country_col] = eurostat_raw[country_col].astype(str)
    eurostat_raw[quarter_col] = eurostat_raw[quarter_col].astype(str)
    eurostat_raw[value_col] = pd.to_numeric(eurostat_raw[value_col], errors="coerce")
    latest_quarter = eurostat_raw.loc[eurostat_raw[value_col].notna(), quarter_col].max()
    selected_codes = ["DK", "EU27_2020", "DE", "SE", "FR"]
    euro_labels = sorted(
        eurostat_raw[
            (eurostat_raw[country_col] == "DK") & eurostat_raw[value_col].notna()
        ][quarter_col].unique().tolist()
    )
    euro_series = {}
    for code in selected_codes:
        subset = eurostat_raw[
            (eurostat_raw[country_col] == code) & eurostat_raw[value_col].notna()
        ].set_index(quarter_col)
        euro_series[code] = [number(subset[value_col].get(p)) for p in euro_labels]

    latest_euro = eurostat_raw[
        (eurostat_raw[quarter_col] == latest_quarter) & eurostat_raw[value_col].notna()
    ].copy()
    latest_euro = latest_euro.drop_duplicates(subset=[country_col])
    latest_euro = latest_euro.sort_values(value_col)
    latest_bar_labels = latest_euro[country_col].tolist()
    latest_bar_values = [number(v) for v in latest_euro[value_col]]

    eu_conf_raw = pd.read_excel(INPUT, sheet_name="Forbrugertillidsindikatoren EU ")
    eu_labels = eu_conf_raw.iloc[:, 0].astype(str).tolist()
    eu_conf = {
        str(col): [number(v) for v in eu_conf_raw[col]]
        for col in eu_conf_raw.columns[1:]
    }

    return {
        "meta": {
            "updated": "23. juli 2026",
            "sourceFile": INPUT.name,
            "retrievedAt": "2026-07-23",
            "sourceRegister": {
                "unemployment": {
                    "source": "Excel-fallback, oprindeligt Danmarks Statistik og Jobindsats",
                    "dataset": "Samlet overblik, fanen Ledige ",
                    "unit": "fuldtidspersoner og pct.",
                    "seasonalAdjustment": "sæsonkorrigeret",
                },
                "vacancies": {
                    "source": "Excel-fallback, oprindeligt Jobindsats",
                    "dataset": "Samlet overblik, fanen Nyopslåede stillinger",
                    "unit": "antal",
                    "seasonalAdjustment": "ikke oplyst",
                },
                "longTermUnemployment": {
                    "source": "Excel-fallback, oprindeligt Jobindsats",
                    "dataset": "Samlet overblik, fanen Langtid opdelt",
                    "unit": "personer",
                    "seasonalAdjustment": "ikke oplyst",
                },
                "notices": {
                    "source": "Excel-fallback, oprindeligt Jobindsats",
                    "dataset": "Samlet overblik, fanen Varslede afskedigelser",
                    "unit": "personer og virksomheder",
                    "seasonalAdjustment": "ikke oplyst",
                },
                "inflation": {
                    "source": "Excel-fallback, oprindeligt Danmarks Statistik",
                    "dataset": "Samlet overblik, fanen Inflation",
                    "unit": "pct. år til år",
                    "seasonalAdjustment": "ikke relevant",
                },
                "businessConfidence": {
                    "source": "Excel-fallback, oprindeligt Danmarks Statistik",
                    "dataset": "Samlet overblik, fanen Erhvevstillidsindikator",
                    "unit": "indeks",
                    "seasonalAdjustment": "ikke oplyst",
                },
                "internationalUnemployment": {
                    "source": "Excel-fallback, oprindeligt Eurostat",
                    "dataset": "Samlet overblik, fanen EUROSTAT kvartalstal for ledige",
                    "unit": "pct.",
                    "seasonalAdjustment": "som leveret i regnearket",
                },
                "internationalConsumerConfidence": {
                    "source": "Excel-fallback, oprindeligt Europa-Kommissionen",
                    "dataset": "Samlet overblik, fanen Forbrugertillidsindikatoren EU ",
                    "unit": "nettotal",
                    "seasonalAdjustment": "som leveret i regnearket",
                },
            },
            "officialApi": {
                "wages": {
                    "source": "Danmarks Statistik",
                    "dataset": "LBESK104",
                    "filters": {"SEKTOR": ["1000", "1032", "1046"]},
                    "unit": "personer",
                    "seasonalAdjustment": "sæsonkorrigeret",
                    "sourceUpdated": wage_meta.get("updated"),
                },
                "bankruptcies": {
                    "source": "Danmarks Statistik",
                    "dataset": "KONK3",
                    "filters": {"BNØGLE": ["A", "A1", "A2"]},
                    "unit": "antal og tabte job",
                    "seasonalAdjustment": "kun serie A1",
                    "sourceUpdated": bankruptcy_meta.get("updated"),
                },
                "consumerConfidence": {
                    "source": "Danmarks Statistik",
                    "dataset": "FORV1",
                    "filters": {"INDIKATOR": ["F1"]},
                    "unit": "nettotal",
                    "seasonalAdjustment": "ikke oplyst",
                    "sourceUpdated": confidence_meta.get("updated"),
                },
            },
        },
        "unemployment": unemployment,
        "vacancies": {"labels": vacancy_labels, "values": vacancy_values},
        "wages": wages,
        "notices": {
            "labels": notice_labels,
            "people": notice_people,
            "companies": notice_companies,
        },
        "bankruptcies": bankruptcies,
        "longterm": long_data,
        "inflation": inflation,
        "confidence": confidence,
        "business": business,
        "eurostat": {
            "labels": euro_labels,
            "series": euro_series,
            "latestPeriod": latest_quarter,
            "latestLabels": latest_bar_labels,
            "latestValues": latest_bar_values,
        },
        "euConfidence": {"labels": eu_labels, "series": eu_conf},
    }


COUNTRY_NAMES = {
    "AT": "Østrig", "BE": "Belgien", "BG": "Bulgarien", "CH": "Schweiz",
    "CY": "Cypern", "CZ": "Tjekkiet", "DE": "Tyskland", "DK": "Danmark",
    "EE": "Estland", "EL": "Grækenland", "ES": "Spanien", "EU27_2020": "EU-27",
    "FI": "Finland", "FR": "Frankrig", "HR": "Kroatien", "HU": "Ungarn",
    "IE": "Irland", "IS": "Island", "IT": "Italien", "LT": "Litauen",
    "LU": "Luxembourg", "LV": "Letland", "MT": "Malta", "NL": "Nederlandene",
    "NO": "Norge", "PL": "Polen", "PT": "Portugal", "RO": "Rumænien",
    "SE": "Sverige", "SI": "Slovenien", "SK": "Slovakiet",
    "EA20": "Euroområdet (20 lande)", "EA21": "Euroområdet (21 lande)",
    "TR": "Tyrkiet", "RS": "Serbien", "BA": "Bosnien-Hercegovina",
    "MK": "Nordmakedonien",
}


def international_table(data, kind):
    if kind == "eurostat":
        codes = ["DK", "EU27_2020", "DE", "SE", "FR"]
        rows = []
        for code in codes:
            value = latest_valid(data["eurostat"]["labels"], data["eurostat"]["series"][code])[1]
            rows.append(
                f"<tr><th>{COUNTRY_NAMES[code]}</th><td>{fmt_number(value, 1)} pct.</td></tr>"
            )
        return f"""
        <article class="table-card">
          <div class="kpi-title">Ledighed i udvalgte lande</div>
          <div class="kpi-period">{fmt_period(data["eurostat"]["latestPeriod"])}</div>
          <table><tbody>{''.join(rows)}</tbody></table>
        </article>"""
    codes = ["DK", "EU27_2020", "DE", "SE", "FR"]
    rows = []
    for code in codes:
        value = latest_valid(data["euConfidence"]["labels"], data["euConfidence"]["series"][code])[1]
        label = COUNTRY_NAMES[code]
        rows.append(f"<tr><th>{label}</th><td>{fmt_number(value, 1)}</td></tr>")
    period = data["euConfidence"]["labels"][-1]
    return f"""
        <article class="table-card">
          <div class="kpi-title">Forbrugertillid i udvalgte lande</div>
          <div class="kpi-period">{fmt_period(period)}</div>
          <table><tbody>{''.join(rows)}</tbody></table>
        </article>"""


def build_html(data):
    expired_period, expired_one_year = latest_valid(
        data["expiredBenefits"]["labels"],
        data["expiredBenefits"]["oneYearGraduate"],
    )
    kpi_cards = [
        card(
            "Bruttoledige",
            {"labels": data["unemployment"]["labels"], "values": data["unemployment"]["total"]},
            "fuldtidspersoner", 0, False, "Sæsonkorrigeret", True,
        ),
        card(
            "A-dagpengemodtagere",
            {"labels": data["unemployment"]["labels"], "values": data["unemployment"]["benefit"]},
            "fuldtidspersoner", 0, False, "Sæsonkorrigeret",
        ),
        card(
            "Langtidsledige",
            {"labels": data["longterm"]["labels"], "values": data["longterm"]["total"]},
            "personer", 0, False,
        ),
        card("Nyopslåede stillinger", data["vacancies"], "stillinger", 0, True, priority=True),
        card(
            "Lønmodtagere",
            {"labels": data["wages"]["labels"], "values": data["wages"]["total"]},
            "personer", 0, True, "Sæsonkorrigeret", True,
        ),
        card(
            "Varslede afskedigelser",
            {"labels": data["notices"]["labels"], "values": data["notices"]["people"]},
            "personer", 0, False, priority=True,
        ),
        card(
            "Tabte job ved konkurser",
            {"labels": data["bankruptcies"]["labels"], "values": data["bankruptcies"]["lostJobs"]},
            "job", 0, False,
        ),
        card(
            "Opbrugt dagpengeret",
            {
                "labels": data["expiredBenefits"]["labels"],
                "values": data["expiredBenefits"]["total"],
            },
            "personer", 0, False,
            f"Heraf {fmt_number(expired_one_year)} med 1 års dimittendret i {fmt_period(expired_period)}",
        ),
    ]
    economy_cards = [
        card(
            "Forbrugertillid",
            {"labels": data["confidence"]["labels"], "values": data["confidence"]["value"]},
            "", 1, True,
        ),
        card(
            "Erhvervstillid",
            {"labels": data["business"]["labels"], "values": data["business"]["value"]},
            "", 1, True,
        ),
        card(
            "Inflation",
            {"labels": data["inflation"]["labels"], "values": data["inflation"]["total"]},
            "pct.", 1, False,
        ),
    ]
    recruitment_period, recruitment_rate = latest_valid(
        data["failedRecruitment"]["labels"],
        data["failedRecruitment"]["rate"],
    )
    dynamics_cards = [
        card(
            "Arbejdsfordelinger",
            {"labels": data["workSharing"]["labels"], "values": data["workSharing"]["total"]},
            "personer", 0, False, "Under og over 13 uger",
        ),
        card(
            "Forgæves rekrutteringsforsøg",
            {
                "labels": data["failedRecruitment"]["labels"],
                "values": data["failedRecruitment"]["attempts"],
            },
            "forsøg", 0, False,
            f"FRR: {fmt_number(recruitment_rate, 1)} pct. i {fmt_period(recruitment_period)}",
        ),
    ]
    insight_cards = [
        insight_card(
            "Bruttoledigheden",
            {"labels": data["unemployment"]["labels"], "values": data["unemployment"]["total"]},
            "fuldtidspersoner", 0, False,
        ),
        insight_card("Nyopslåede stillinger", data["vacancies"], "stillinger", 0, True),
        insight_card(
            "Lønmodtagere",
            {"labels": data["wages"]["labels"], "values": data["wages"]["total"]},
            "personer", 0, True,
        ),
    ]
    safe_data = json_safe(data)
    json_data = json.dumps(safe_data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    country_names = json.dumps(COUNTRY_NAMES, ensure_ascii=False, separators=(",", ":"))
    status_banner = update_status_banner(data)

    return f"""<!doctype html>
<html lang="da">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Analytisk overblik - Arbejdsmarkedet</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <link rel="stylesheet" href="https://ai-michelklos.github.io/arbejdsmarked/stil/efteraar.css">
  <style>
    body {{margin:0; background:#f7f1ea; font-family:Arial,sans-serif; color:#2B211B}}
    .dak-hero {{background-color:#2B211B; background-image:url('assets/arbejdsmarkedets-puls.webp'),linear-gradient(135deg,#2B211B,#4a3428); background-position:center 46%,center; background-size:cover,cover; background-repeat:no-repeat; color:#fff; padding:32px 20px}}
    .dak-hero-inner {{max-width:1280px; margin:0 auto}}
    .dak-eyebrow {{font-size:.75rem; letter-spacing:.12em; text-transform:uppercase; font-weight:700; color:#e2d2bf}}
    .dak-hero h1 {{font-size:clamp(2.1rem,4vw,3.4rem); line-height:1.08; margin:5px 0 0; color:#fff}}
    .dak-byline {{font-size:.78rem; color:#e2d2bf; font-weight:600; margin-top:5px}}
    .dak-hero-sub {{max-width:900px; color:#f7f1ea; line-height:1.5; margin:10px 0 0}}
    #dak-dashboard {{
      --navy:#4A3428; --blue:#146298; --cyan:#9A5B34; --red:#b14f4a; --good:#3D6B47; --bad:#B14F4A;
      --green:#9A5B34; --green-dark:#4a3428; --orange:#8C9C41; --purple:#8C7FCF; --ink:#2B211B;
      --muted:#6f6258; --line:#E6DDD2; --paper:#fff; --soft:#f7f1ea;
      max-width:1280px; margin:0 auto; padding:28px 20px 55px; color:var(--ink);
      font-family:Arial,sans-serif; box-sizing:border-box;
    }}
    #dak-dashboard * {{box-sizing:border-box}}
    #dak-dashboard h1 {{font-size:32px; line-height:1.15; color:var(--navy); margin:0 0 8px}}
    #dak-dashboard h2 {{font-size:23px; color:var(--navy); margin:44px 0 16px;}}
    #dak-dashboard h3 {{font-size:18px; color:var(--navy); margin:0 0 6px}}
    #dak-dashboard .intro {{color:var(--muted); margin:0 0 18px; line-height:1.55}}
    #dak-dashboard .toolbar {{display:flex; flex-wrap:wrap; align-items:center; gap:9px; padding:14px 16px; background:var(--soft); border-radius:8px; margin:18px 0 24px}}
    #dak-dashboard .toolbar-label {{font-weight:700; margin-right:4px}}
    #dak-dashboard button {{border:1px solid var(--green-dark); background:#fff; color:var(--green-dark); border-radius:8px; min-height:42px; padding:9px 14px; font-weight:700; cursor:pointer}}
    #dak-dashboard button.active, #dak-dashboard button:hover {{background:var(--green-dark); color:#fff}}
    #dak-dashboard button:focus-visible, #dak-dashboard a:focus-visible {{outline:3px solid #146298; outline-offset:3px}}
    #dak-dashboard .updated {{margin-left:auto; color:var(--muted); font-size:13px}}
    #dak-dashboard .update-status {{margin:0 0 22px; padding:12px 14px; font-size:13px; line-height:1.45; border-left:4px solid}}
    #dak-dashboard .update-status.partial {{background:#fff4d6; border-color:#ef8b2c}}
    #dak-dashboard .update-status.stale {{background:#fde9e8; border-color:#e34a45}}
    #dak-dashboard .section-nav {{position:sticky; top:0; z-index:20; display:flex; gap:8px; overflow-x:auto; padding:10px 0; margin:0 0 22px; background:rgba(247,241,234,.96); backdrop-filter:blur(8px); border-bottom:1px solid var(--line)}}
    #dak-dashboard .section-nav a {{white-space:nowrap; color:var(--green-dark); background:#fff; border:1px solid var(--line); border-radius:999px; padding:9px 13px; text-decoration:none; font-size:13px; font-weight:700}}
    #dak-dashboard .section-nav a:hover {{background:var(--green-dark); color:#fff}}
    #dak-dashboard .insights {{margin:8px 0 30px}}
    #dak-dashboard .insights-heading {{display:flex; align-items:flex-end; justify-content:space-between; gap:16px; margin-bottom:12px}}
    #dak-dashboard .insights-heading h2 {{margin:0}}
    #dak-dashboard .insights-heading p {{margin:0; color:var(--muted); font-size:13px}}
    #dak-dashboard .insight-grid {{display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px}}
    #dak-dashboard .insight-card {{position:relative; overflow:hidden; background:#fff; border:1px solid var(--line); border-radius:14px; padding:18px 18px 17px}}
    #dak-dashboard .insight-card::before {{content:''; position:absolute; inset:0 auto 0 0; width:5px; background:var(--muted)}}
    #dak-dashboard .insight-card.good::before {{background:var(--good)}}
    #dak-dashboard .insight-card.bad::before {{background:var(--bad)}}
    #dak-dashboard .insight-head {{display:flex; align-items:baseline; justify-content:space-between; gap:12px}}
    #dak-dashboard .insight-kicker {{font-weight:800; color:var(--navy); font-size:14px}}
    #dak-dashboard .insight-period {{flex:0 0 auto; color:var(--muted); font-size:12px; font-weight:700}}
    #dak-dashboard .insight-value {{font-size:25px; font-weight:800; color:var(--navy); margin:7px 0 5px}}
    #dak-dashboard .insight-value span {{font-size:12px; font-weight:400; color:var(--muted)}}
    #dak-dashboard .insight-card p {{margin:0; color:var(--muted); line-height:1.45; font-size:13px}}
    #dak-dashboard .kpi-grid {{display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px}}
    #dak-dashboard .kpi-grid.economy {{grid-template-columns:repeat(3,minmax(0,1fr))}}
    #dak-dashboard .kpi-grid.dynamics {{grid-template-columns:repeat(2,minmax(0,1fr))}}
    #dak-dashboard .kpi-grid.international {{grid-template-columns:repeat(2,minmax(0,1fr))}}
    #dak-dashboard .kpi-card, #dak-dashboard .table-card {{background:var(--paper); border:1px solid var(--line); border-radius:12px; padding:16px; box-shadow:none}}
    #dak-dashboard .kpi-card.priority {{border-top:4px solid var(--green); padding-top:13px; box-shadow:0 8px 24px rgba(74,52,40,.08)}}
    #dak-dashboard .kpi-title {{font-weight:700; color:var(--navy); min-height:38px}}
    #dak-dashboard .kpi-value {{font-size:28px; color:var(--navy); font-weight:800; line-height:1.15; margin:8px 0 2px}}
    #dak-dashboard .kpi-value span {{font-size:13px; font-weight:400; color:var(--muted)}}
    #dak-dashboard .kpi-period {{font-size:13px; color:var(--muted); margin-bottom:10px}}
    #dak-dashboard .kpi-deltas {{border-top:1px solid var(--line); padding-top:8px; min-height:49px}}
    #dak-dashboard .delta-row {{font-size:12px; margin:3px 0}}
    #dak-dashboard .delta-good {{color:var(--good); font-weight:700}}
    #dak-dashboard .delta-bad {{color:var(--bad); font-weight:700}}
    #dak-dashboard .delta-neutral {{color:var(--muted); font-weight:700}}
    #dak-dashboard .change-meaning {{display:inline-block; margin-left:5px; padding:1px 5px; border:1px solid currentColor; border-radius:999px; font-size:9px; text-transform:uppercase; letter-spacing:.04em}}
    #dak-dashboard .compare-label {{color:var(--muted)}}
    #dak-dashboard .kpi-note {{font-size:11px; color:var(--muted); margin-top:7px}}
    #dak-dashboard .chart-grid {{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; margin-top:20px}}
    #dak-dashboard .chart-card {{background:#fff; border:1px solid var(--line); border-radius:12px; padding:20px; box-shadow:none}}
    #dak-dashboard .chart-card {{position:relative}}
    #dak-dashboard .chart-actions {{position:absolute; top:14px; right:14px; z-index:6}}
    #dak-dashboard .share-chart {{padding:6px 9px; min-height:34px; border:1px solid var(--line); background:#fff; color:var(--green-dark); border-radius:7px; font-size:12px; font-weight:700; line-height:1.2; box-shadow:0 2px 8px rgba(43,33,27,.08)}}
    #dak-dashboard .share-chart:hover, #dak-dashboard .share-chart:focus-visible {{background:var(--green-dark); color:#fff; border-color:var(--green-dark)}}
    #dak-dashboard .chart-menu {{position:absolute; right:0; top:42px; width:185px; display:grid; gap:4px; padding:7px; background:#fff; border:1px solid var(--line); border-radius:10px; box-shadow:0 12px 30px rgba(43,33,27,.18)}}
    #dak-dashboard .chart-menu[hidden] {{display:none}}
    #dak-dashboard .chart-menu button {{min-height:38px; width:100%; text-align:left; border:0; padding:8px 10px; font-size:12px}}
    #dak-dashboard .chart-card h3 {{padding-right:82px}}
    #dak-dashboard .chart-insight {{margin:8px 92px 10px 0; color:var(--muted); font-size:13px; line-height:1.45; min-height:19px}}
    #dak-dashboard .chart-card.share-target {{outline:3px solid var(--green); outline-offset:3px; box-shadow:0 10px 28px rgba(74,52,40,.22)}}
    #dak-dashboard .chart-card.wide {{grid-column:1/-1}}
    #dak-dashboard .chart-wrap {{position:relative; height:390px}}
    #dak-dashboard .chart-card.wide .chart-wrap {{height:430px}}
    #dak-dashboard .chart-card.tall .chart-wrap {{height:650px}}
    #dak-dashboard .source {{font-size:11px; color:var(--muted); line-height:1.5; margin:12px 0 0; padding-top:10px; border-top:1px solid var(--line)}}
    #dak-dashboard table {{width:100%; border-collapse:collapse; margin-top:5px}}
    #dak-dashboard th, #dak-dashboard td {{border-bottom:1px solid var(--line); padding:8px 4px; text-align:left; font-size:14px}}
    #dak-dashboard td {{text-align:right; font-weight:700; color:var(--navy)}}
    #dak-dashboard .footnote {{margin-top:30px; background:var(--soft); padding:16px; font-size:13px; color:var(--muted); line-height:1.5}}
    @media(max-width:900px) {{
      #dak-dashboard .kpi-grid, #dak-dashboard .kpi-grid.economy, #dak-dashboard .kpi-grid.dynamics {{grid-template-columns:repeat(2,minmax(0,1fr))}}
      #dak-dashboard .insight-grid {{grid-template-columns:1fr}}
      #dak-dashboard .chart-grid {{grid-template-columns:1fr}}
      #dak-dashboard .chart-card.wide {{grid-column:auto}}
    }}
    @media(max-width:560px) {{
      .dak-hero {{background-position:58% center,center}}
      #dak-dashboard {{padding:14px}}
      #dak-dashboard h1 {{font-size:27px}}
      #dak-dashboard .kpi-grid, #dak-dashboard .kpi-grid.economy, #dak-dashboard .kpi-grid.dynamics, #dak-dashboard .kpi-grid.international {{grid-template-columns:1fr}}
      #dak-dashboard .updated {{width:100%; margin:6px 0 0}}
      #dak-dashboard .chart-wrap, #dak-dashboard .chart-card.wide .chart-wrap {{height:340px}}
      #dak-dashboard button {{min-height:44px}}
      #dak-dashboard .section-nav {{margin-left:-14px; margin-right:-14px; padding-left:14px; padding-right:14px}}
      #dak-dashboard .insights-heading {{display:block}}
      #dak-dashboard .insights-heading p {{margin-top:5px}}
      #dak-dashboard .chart-card {{padding:16px}}
      #dak-dashboard .chart-insight {{margin-right:0; padding-right:72px}}
      #dak-dashboard .chart-actions {{right:12px; top:12px}}
    }}
    @media(prefers-reduced-motion:reduce) {{html {{scroll-behavior:auto}} #dak-dashboard * {{scroll-behavior:auto!important; transition:none!important}}}}
  </style>
</head>
<body>
<header class="dak-hero"><div class="dak-hero-inner"><h1>Analytisk overblik - Arbejdsmarkedet</h1><div class="dak-byline">Udarbejdet af Michel Klos</div><p class="dak-hero-sub">Udvalgte nøgletal for arbejdsmarkedet, samfundsøkonomien og den internationale udvikling. Tallene opdateres i takt med de enkelte kilders offentliggørelser.</p></div></header>
<main id="dak-dashboard">
  <div class="toolbar">
    <span class="toolbar-label">Vis periode:</span>
    <button type="button" data-months="12" aria-pressed="false" onclick="setPeriod(12,this)">12 måneder</button>
    <button type="button" data-months="36" aria-pressed="false" onclick="setPeriod(36,this)">36 måneder</button>
    <button type="button" class="active" data-months="60" aria-pressed="true" onclick="setPeriod(60,this)">60 måneder</button>
    <span class="updated">Opdateret {data["meta"]["updated"]}</span>
  </div>

  <nav class="section-nav" aria-label="Gå til emne">
    <a href="#vigtigste">Det vigtigste</a>
    <a href="#ledighed">Ledighed</a>
    <a href="#rekruttering">Rekruttering</a>
    <a href="#samfundsoekonomi">Samfundsøkonomi</a>
    <a href="#internationalt">Internationalt</a>
  </nav>
  {status_banner}

  <section class="insights" id="vigtigste" aria-labelledby="vigtigste-heading">
    <div class="insights-heading"><h2 id="vigtigste-heading">Det vigtigste lige nu</h2><p>Tre udviklinger, der er værd at bemærke</p></div>
    <div class="insight-grid">{''.join(insight_cards)}</div>
  </section>

  <h2 id="ledighed">Ledighed og beskæftigelse</h2>
  <div class="kpi-grid">{''.join(kpi_cards)}</div>
  <div class="chart-grid">
    <article class="chart-card"><h3>Bruttoledighed</h3><div class="chart-wrap"><canvas id="unemploymentTotal"></canvas></div><p class="source">Kilde: Danmarks Statistik og Jobindsats</p></article>
    <article class="chart-card"><h3>Ledighedsprocent</h3><div class="chart-wrap"><canvas id="unemploymentRate"></canvas></div><p class="source">Kilde: Danmarks Statistik og Jobindsats</p></article>
    <article class="chart-card"><h3>Ledige fordelt på ydelse</h3><div class="chart-wrap"><canvas id="unemploymentType"></canvas></div><p class="source">Kilde: Jobindsats</p></article>
    <article class="chart-card"><h3>Langtidsledige</h3><div class="chart-wrap"><canvas id="longterm"></canvas></div><p class="source">Kilde: Jobindsats</p></article>
    <article class="chart-card"><h3>Nyopslåede stillinger</h3><div class="chart-wrap"><canvas id="vacancies"></canvas></div><p class="source">Kilde: Jobindsats</p></article>
    <article class="chart-card"><h3>Lønmodtagere</h3><div class="chart-wrap"><canvas id="wages"></canvas></div><p class="source">Kilde: Danmarks Statistik</p></article>
    <article class="chart-card"><h3>Varslede afskedigelser</h3><div class="chart-wrap"><canvas id="notices"></canvas></div><p class="source">Kilde: Jobindsats</p></article>
    <article class="chart-card"><h3>Konkurser og tabte job</h3><div class="chart-wrap"><canvas id="bankruptcies"></canvas></div><p class="source">Kilde: Danmarks Statistik</p></article>
    <article class="chart-card wide"><h3>Opbrugt dagpengeret fordelt på dimittendstatus</h3><div class="chart-wrap"><canvas id="expiredBenefits"></canvas></div><p class="source">Kilde: Jobindsats. Antal personer med opbrugt dagpengeret fordelt på 1 års dimittendret, øvrige dimittender og øvrige ledige.</p></article>
  </div>

  <h2 id="rekruttering">Rekruttering og arbejdsfordeling</h2>
  <p class="intro">Udviklingen i arbejdsfordelinger samt virksomhedernes forgæves rekrutteringsforsøg på baggrund af STARs rekrutteringssurvey.</p>
  <div class="kpi-grid dynamics">{''.join(dynamics_cards)}</div>
  <div class="chart-grid">
    <article class="chart-card"><h3>Arbejdsfordelinger under og over 13 uger</h3><div class="chart-wrap"><canvas id="workSharing"></canvas></div><p class="source">Kilde: Jobindsats</p></article>
    <article class="chart-card"><h3>Forgæves rekrutteringsforsøg over tid</h3><div class="chart-wrap"><canvas id="failedRecruitment"></canvas></div><p class="source">Kilde: Jobindsats og STAR. FR omfatter både ikke besatte rekrutteringer og rekrutteringer besat med en anden profil.</p></article>
    <article class="chart-card wide tall"><h3>15 stillinger med flest forgæves rekrutteringsforsøg</h3><div class="chart-wrap"><canvas id="topRecruitmentOccupations"></canvas></div><p class="source">Kilde: Jobindsats og STAR. Seneste surveyperiode: {fmt_period(data["failedRecruitment"]["topPeriod"])}.</p></article>
  </div>

  <h2 id="samfundsoekonomi">Samfundsøkonomi</h2>
  <div class="kpi-grid economy">{''.join(economy_cards)}</div>
  <div class="chart-grid">
    <article class="chart-card"><h3>Forbrugertillid</h3><div class="chart-wrap"><canvas id="confidence"></canvas></div><p class="source">Kilde: Danmarks Statistik</p></article>
    <article class="chart-card"><h3>Erhvervstillid</h3><div class="chart-wrap"><canvas id="business"></canvas></div><p class="source">Kilde: Danmarks Statistik</p></article>
    <article class="chart-card wide"><h3>Inflation</h3><div class="chart-wrap"><canvas id="inflation"></canvas></div><p class="source">Kilde: Danmarks Statistik</p></article>
  </div>

  <h2 id="internationalt">Internationalt</h2>
  <div class="kpi-grid international">{international_table(data, "eurostat")}{international_table(data, "confidence")}</div>
  <div class="chart-grid">
    <article class="chart-card wide"><h3>Ledighed i udvalgte lande</h3><div class="chart-wrap"><canvas id="eurostatTrend"></canvas></div><p class="source">Kilde: Eurostat</p></article>
    <article class="chart-card wide tall"><h3>Ledighed i Europa, seneste kvartal</h3><div class="chart-wrap"><canvas id="eurostatLatest"></canvas></div><p class="source">Kilde: Eurostat</p></article>
    <article class="chart-card wide"><h3>Forbrugertillid i udvalgte lande</h3><div class="chart-wrap"><canvas id="euConfidence"></canvas></div><p class="source">Kilde: Europa-Kommissionen, DG ECFIN</p></article>
  </div>
  <div class="footnote">Bemærk: Serierne offentliggøres på forskellige tidspunkter. Den seneste periode kan derfor variere mellem nøgletal. Ændringer på KPI-kortene er angivet i enheder, ikke i procent.</div>
<div style="margin-top:22px;color:var(--muted);font-size:.78rem;text-align:center">Grafik og databehandling: Michel Klos. Kontakt: <a href="mailto:michel@klos.dk" style="color:var(--green-dark)">michel@klos.dk</a></div>
</main>
<script>
(function(){{
const DATA={json_data};
const COUNTRY_NAMES={country_names};
const COLORS=['#9A5B34','#146298','#8C9C41','#8C7FCF','#7D3659','#1D9999'];
const charts={{}};
let activeMonths=60;

const latestValueLabel={{
  id:'latestValueLabel',
  afterDatasetsDraw(chart,args,options){{
    if(options?.display===false) return;
    const datasetIndex=options?.datasetIndex||0;
    const dataset=chart.data.datasets?.[datasetIndex];
    const meta=chart.getDatasetMeta(datasetIndex);
    if(!dataset || !meta || meta.hidden) return;
    let index=(dataset.data||[]).length-1;
    while(index>=0 && !Number.isFinite(Number(dataset.data[index]))) index--;
    const point=meta.data?.[index];
    if(index<0 || !point) return;
    const value=Number(dataset.data[index]);
    const decimals=Number(options?.decimals||0);
    const text=dkNumber(value,decimals)+(options?.suffix||'');
    const ctx=chart.ctx;
    ctx.save(); ctx.font='700 11px Arial';
    const width=ctx.measureText(text).width+14;
    const x=Math.min(chart.chartArea.right-width/2,Math.max(chart.chartArea.left+width/2,point.x));
    const y=Math.max(chart.chartArea.top+11,point.y-16);
    ctx.fillStyle='rgba(74,52,40,.92)';
    ctx.beginPath(); ctx.roundRect(x-width/2,y-11,width,20,6); ctx.fill();
    ctx.fillStyle='#fff'; ctx.textAlign='center'; ctx.textBaseline='middle'; ctx.fillText(text,x,y-1); ctx.restore();
  }}
}};
const referenceLine={{
  id:'referenceLine',
  beforeDatasetsDraw(chart,args,options){{
    const value=Number(options?.value);
    if(!Number.isFinite(value) || !chart.scales?.y) return;
    const y=chart.scales.y.getPixelForValue(value);
    if(y<chart.chartArea.top || y>chart.chartArea.bottom) return;
    const ctx=chart.ctx; ctx.save(); ctx.setLineDash([6,5]); ctx.strokeStyle='#6f6258'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(chart.chartArea.left,y); ctx.lineTo(chart.chartArea.right,y); ctx.stroke();
    ctx.setLineDash([]); ctx.fillStyle='#6f6258'; ctx.font='600 10px Arial'; ctx.textAlign='right';
    ctx.fillText(options?.label||dkNumber(value,1),chart.chartArea.right-4,y-5); ctx.restore();
  }}
}};
Chart.register(latestValueLabel,referenceLine);

function dkNumber(value,decimals=0){{
  if(value===null || value===undefined) return '';
  return new Intl.NumberFormat('da-DK',{{minimumFractionDigits:decimals,maximumFractionDigits:decimals}}).format(value);
}}
function periodLabel(value){{
  const months=['jan.','feb.','mar.','apr.','maj','jun.','jul.','aug.','sep.','okt.','nov.','dec.'];
  let m=String(value).match(/^(\\d{{4}})M(\\d{{2}})$/);
  if(m) return months[Number(m[2])-1]+' '+m[1];
  m=String(value).match(/^(\\d{{4}})-(\\d{{2}})$/);
  if(m) return months[Number(m[2])-1]+' '+m[1];
  m=String(value).match(/^(\\d{{4}})-Q([1-4])$/);
  if(m) return 'Q'+m[2]+' '+m[1];
  return String(value);
}}
function sliceData(labels,values,n){{
  const start=Math.max(0,labels.length-n);
  return {{labels:labels.slice(start),values:values.slice(start)}};
}}
function sliced(labels, series, n){{
  const start=Math.max(0,labels.length-n);
  return {{labels:labels.slice(start),series:series.map(values=>values.slice(start))}};
}}
function xAxis(){{
  return {{
    type:'category',
    grid:{{display:false}},
    ticks:{{
      maxRotation:0,autoSkip:true,maxTicksLimit:12,
      callback:function(val,idx){{const labels=this.chart.data.labels||[];return periodLabel(labels[val]??labels[idx]??val);}}
    }}
  }};
}}
function commonOptions(decimals=0){{
  return {{
    responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},
    plugins:{{
      legend:{{position:'top',align:'start',labels:{{boxWidth:12,boxHeight:12,padding:18,usePointStyle:true}}}},
      tooltip:{{callbacks:{{title:items=>periodLabel(items[0].label),label:ctx=>' '+ctx.dataset.label+': '+dkNumber(ctx.parsed.y,decimals)}}}},
      latestValueLabel:{{decimals}}
    }},
    scales:{{x:xAxis(),y:{{beginAtZero:false,grid:{{color:'#E6DDD2'}},ticks:{{padding:8,callback:v=>dkNumber(v,decimals)}}}}}}
  }};
}}
function replaceChart(id,config){{
  if(charts[id]) charts[id].destroy();
  charts[id]=new Chart(document.getElementById(id),config);
}}
function lineChart(id,labels,datasets,decimals=0,yMin,reference){{
  const opts=commonOptions(decimals);
  if(yMin!==undefined) opts.scales.y.min=yMin;
  if(reference) opts.plugins.referenceLine=reference;
  opts.plugins.legend.display=datasets.length>1;
  replaceChart(id,{{type:'line',data:{{labels,datasets:datasets.map((d,i)=>({{
    ...d,borderColor:d.borderColor||COLORS[i],backgroundColor:d.backgroundColor||COLORS[i],
    pointRadius:0,pointHoverRadius:4,borderWidth:2.3,tension:.18,spanGaps:true
  }}))}},options:opts}});
}}
function barChart(id,labels,datasets,decimals=0){{
  const opts=commonOptions(decimals); opts.scales.y.beginAtZero=true;
  opts.plugins.legend.display=datasets.length>1;
  replaceChart(id,{{type:'bar',data:{{labels,datasets:datasets.map((d,i)=>({{
    ...d,backgroundColor:d.backgroundColor||COLORS[i],borderRadius:2
  }}))}},options:opts}});
}}
function stackedBarChart(id,labels,datasets){{
  const opts=commonOptions(0);
  opts.scales.x={{...xAxis(),stacked:true}};
  opts.scales.y={{beginAtZero:true,stacked:true,grid:{{color:'#E6DDD2'}},ticks:{{callback:v=>dkNumber(v,0)}}}};
  replaceChart(id,{{type:'bar',data:{{labels,datasets:datasets.map((d,i)=>({{
    ...d,backgroundColor:d.backgroundColor||COLORS[i],borderRadius:2
  }}))}},options:opts}});
}}
function recruitmentChart(id,labels,attempts,rate){{
  const opts=commonOptions(0);
  opts.scales.y={{
    beginAtZero:true,position:'left',title:{{display:true,text:'Forgæves rekrutteringsforsøg'}},
    ticks:{{callback:v=>dkNumber(v,0)}}
  }};
  opts.scales.y1={{
    beginAtZero:true,position:'right',title:{{display:true,text:'FRR, pct.'}},
    grid:{{drawOnChartArea:false}},ticks:{{callback:v=>dkNumber(v,1)+' %'}}
  }};
  opts.plugins.tooltip.callbacks.label=ctx=>ctx.dataset.yAxisID==='y1'
    ? ' '+ctx.dataset.label+': '+dkNumber(ctx.parsed.y,1)+' pct.'
    : ' '+ctx.dataset.label+': '+dkNumber(ctx.parsed.y,0);
  replaceChart(id,{{type:'bar',data:{{labels,datasets:[
    {{label:'Forgæves rekrutteringsforsøg',data:attempts,backgroundColor:'rgba(154,91,52,.78)',borderColor:COLORS[0],borderWidth:1,borderRadius:2,yAxisID:'y'}},
    {{type:'line',label:'FRR',data:rate,borderColor:COLORS[1],backgroundColor:COLORS[1],pointRadius:0,pointHoverRadius:4,borderWidth:2.5,tension:.18,yAxisID:'y1'}}
  ]}},options:opts}});
}}
function horizontalValueBar(id,labels,values,label){{
  replaceChart(id,{{
    type:'bar',data:{{labels,datasets:[{{label,data:values,backgroundColor:COLORS[2],borderRadius:2}}]}},
    options:{{
      indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}},latestValueLabel:{{display:false}},tooltip:{{callbacks:{{label:ctx=>' '+dkNumber(ctx.parsed.x,0)+' forsøg'}}}}}},
      scales:{{
        x:{{beginAtZero:true,grid:{{color:'#E6DDD2'}},ticks:{{callback:v=>dkNumber(v,0)}}}},
        y:{{grid:{{display:false}}}}
      }}
    }}
  }});
}}
function dualAxisChart(id,labels,left,right){{
  const opts=commonOptions(0);
  opts.scales.y={{
    beginAtZero:true,position:'left',
    title:{{display:true,text:left.axisTitle||left.label}},
    ticks:{{callback:v=>dkNumber(v,0)}}
  }};
  opts.scales.y1={{
    beginAtZero:true,position:'right',
    title:{{display:true,text:right.axisTitle||right.label}},
    grid:{{drawOnChartArea:false}},ticks:{{callback:v=>dkNumber(v,0)}}
  }};
  replaceChart(id,{{type:'line',data:{{labels,datasets:[
    {{label:left.label+' (venstre akse)',data:left.data,borderColor:COLORS[0],backgroundColor:COLORS[0],pointRadius:0,borderWidth:2,tension:.18,yAxisID:'y'}},
    {{label:right.label+' (højre akse)',data:right.data,borderColor:COLORS[1],backgroundColor:'rgba(20,98,152,.32)',type:'bar',borderRadius:2,yAxisID:'y1'}}
  ]}},options:opts}});
}}
function horizontalBar(id,labels,values){{
  replaceChart(id,{{
    type:'bar',
    data:{{labels,datasets:[{{label:'Ledighed, pct.',data:values,backgroundColor:labels.map(v=>v==='DK'?COLORS[1]:COLORS[0]),borderRadius:2}}]}},
    options:{{
      indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}},latestValueLabel:{{display:false}},tooltip:{{callbacks:{{label:ctx=>' '+dkNumber(ctx.parsed.x,1)+' pct.'}}}}}},
      scales:{{
        x:{{beginAtZero:true,grid:{{color:'#E6DDD2'}},ticks:{{callback:v=>dkNumber(v,1)+' %'}}}},
        y:{{grid:{{display:false}},ticks:{{autoSkip:false,callback:function(val,idx){{const labels=this.chart.data.labels||[];const code=labels[val]??labels[idx]??val;return COUNTRY_NAMES[code]||code;}}}}}}
      }}
    }}
  }});
}}
function drawAll(n){{
  activeMonths=n;
  let s=sliced(DATA.unemployment.labels,[DATA.unemployment.total],n);
  lineChart('unemploymentTotal',s.labels,[{{label:'Bruttoledige',data:s.series[0]}}]);
  s=sliced(DATA.unemployment.labels,[DATA.unemployment.rate],n);
  lineChart('unemploymentRate',s.labels,[{{label:'Ledighed',data:s.series[0]}}],1,0);
  s=sliced(DATA.unemployment.labels,[DATA.unemployment.benefit,DATA.unemployment.assistance],n);
  lineChart('unemploymentType',s.labels,[{{label:'A-dagpenge',data:s.series[0]}},{{label:'Kontanthjælp',data:s.series[1]}}]);
  s=sliced(DATA.longterm.labels,[DATA.longterm.total,DATA.longterm.benefit,DATA.longterm.assistance],n);
  lineChart('longterm',s.labels,[{{label:'I alt',data:s.series[0]}},{{label:'A-dagpenge',data:s.series[1]}},{{label:'Kontanthjælp',data:s.series[2]}}]);
  s=sliced(DATA.vacancies.labels,[DATA.vacancies.values],n);
  barChart('vacancies',s.labels,[{{label:'Nyopslåede stillinger',data:s.series[0]}}]);
  s=sliced(DATA.wages.labels,[DATA.wages.total,DATA.wages.public,DATA.wages.private],n);
  lineChart('wages',s.labels,[{{label:'I alt',data:s.series[0]}},{{label:'Offentlig sektor',data:s.series[1]}},{{label:'Privat sektor',data:s.series[2]}}]);
  s=sliced(DATA.notices.labels,[DATA.notices.people,DATA.notices.companies],n);
  dualAxisChart('notices',s.labels,{{label:'Varslede personer',data:s.series[0]}},{{label:'Virksomheder',data:s.series[1]}});
  s=sliced(DATA.bankruptcies.labels,[DATA.bankruptcies.seasonal,DATA.bankruptcies.lostJobs],n);
  dualAxisChart('bankruptcies',s.labels,{{label:'Konkurser, sæsonkorrigeret',data:s.series[0]}},{{label:'Tabte job',data:s.series[1]}});
  s=sliced(DATA.expiredBenefits.labels,[DATA.expiredBenefits.total,DATA.expiredBenefits.oneYearGraduate,DATA.expiredBenefits.otherGraduates,DATA.expiredBenefits.otherUnemployed],n);
  lineChart('expiredBenefits',s.labels,[{{label:'I alt',data:s.series[0]}},{{label:'1 års dimittendret',data:s.series[1]}},{{label:'Øvrige dimittender',data:s.series[2]}},{{label:'Øvrige ledige',data:s.series[3]}}]);
  s=sliced(DATA.workSharing.labels,[DATA.workSharing.under13Weeks,DATA.workSharing.over13Weeks],n);
  stackedBarChart('workSharing',s.labels,[{{label:'Under 13 uger',data:s.series[0]}},{{label:'Over 13 uger',data:s.series[1]}}]);
  const surveyPeriods=Math.max(4,Math.ceil(n/3));
  s=sliced(DATA.failedRecruitment.labels,[DATA.failedRecruitment.attempts,DATA.failedRecruitment.rate],surveyPeriods);
  recruitmentChart('failedRecruitment',s.labels,s.series[0],s.series[1]);
  horizontalValueBar(
    'topRecruitmentOccupations',
    DATA.failedRecruitment.topOccupations.map(item=>item.name),
    DATA.failedRecruitment.topOccupations.map(item=>item.value),
    'Forgæves rekrutteringsforsøg'
  );
  s=sliced(DATA.confidence.labels,[DATA.confidence.value],n);
  lineChart('confidence',s.labels,[{{label:'Forbrugertillid',data:s.series[0]}}],1,undefined,{{value:0,label:'Neutral'}});
  s=sliced(DATA.business.labels,[DATA.business.value],n);
  lineChart('business',s.labels,[{{label:'Erhvervstillid',data:s.series[0]}}],1,undefined,{{value:100,label:'Indeks 100'}});
  s=sliced(DATA.inflation.labels,[DATA.inflation.total,DATA.inflation.core],n);
  lineChart('inflation',s.labels,[{{label:'Forbrugerpriser',data:s.series[0]}},{{label:'Kerneinflation',data:s.series[1]}}],1,undefined,{{value:2,label:'2 pct.'}});
  const quarters=Math.max(4,Math.ceil(n/3));
  s=sliced(DATA.eurostat.labels,[DATA.eurostat.series.DK,DATA.eurostat.series.EU27_2020,DATA.eurostat.series.DE,DATA.eurostat.series.SE,DATA.eurostat.series.FR],quarters);
  lineChart('eurostatTrend',s.labels,[{{label:'Danmark',data:s.series[0]}},{{label:'EU-27',data:s.series[1]}},{{label:'Tyskland',data:s.series[2]}},{{label:'Sverige',data:s.series[3]}},{{label:'Frankrig',data:s.series[4]}}],1);
  horizontalBar('eurostatLatest',DATA.eurostat.latestLabels,DATA.eurostat.latestValues);
  s=sliced(DATA.euConfidence.labels,[DATA.euConfidence.series.DK,DATA.euConfidence.series.EU27_2020,DATA.euConfidence.series.DE,DATA.euConfidence.series.SE,DATA.euConfidence.series.FR],n);
  lineChart('euConfidence',s.labels,[{{label:'Danmark',data:s.series[0]}},{{label:'EU-27',data:s.series[1]}},{{label:'Tyskland',data:s.series[2]}},{{label:'Sverige',data:s.series[3]}},{{label:'Frankrig',data:s.series[4]}}],1);
}}
function latestChartPoint(chart,datasetIndex=0){{
  const labels=chart?.data?.labels||[];
  const values=chart?.data?.datasets?.[datasetIndex]?.data||[];
  for(let i=Math.min(labels.length,values.length)-1;i>=0;i--){{
    const value=Number(values[i]);
    if(Number.isFinite(value)) return {{value,period:labels[i],index:i}};
  }}
  return {{value:null,period:'',index:-1}};
}}
function shareSummary(id){{
  const chart=charts[id];
  if(!chart) return {{valueText:'',detailText:''}};
  let point=latestChartPoint(chart,0);
  let decimals=0, suffix='', detail=chart.data.datasets?.[0]?.label||'';
  if(['unemploymentRate','inflation','eurostatTrend'].includes(id)){{decimals=1;suffix=' pct.';}}
  if(['confidence','business','euConfidence'].includes(id)) decimals=1;
  if(id==='unemploymentTotal') suffix=' fuldtidspersoner';
  if(id==='vacancies') suffix=' stillinger';
  if(id==='wages') suffix=' personer';
  if(id==='notices') suffix=' personer';
  if(id==='expiredBenefits') suffix=' personer';
  if(id==='failedRecruitment') suffix=' forsøg';
  if(id==='workSharing'){{
    const a=latestChartPoint(chart,0), b=latestChartPoint(chart,1);
    if(a.value!==null && b.value!==null) point={{value:a.value+b.value,period:a.period,index:a.index}};
    suffix=' personer'; detail='I alt';
  }}
  if(id==='topRecruitmentOccupations'){{
    const labels=chart.data.labels||[];
    const values=chart.data.datasets?.[0]?.data||[];
    let bestIndex=-1, bestValue=null;
    values.forEach((raw,i)=>{{const value=Number(raw);if(Number.isFinite(value)&&(bestValue===null||value>bestValue)){{bestValue=value;bestIndex=i;}}}});
    if(bestIndex>=0){{
      return {{
        valueText:dkNumber(bestValue,0)+' forsøg',
        detailText:'Flest: '+String(labels[bestIndex]||'')+' | '+periodLabel(DATA.failedRecruitment.topPeriod)
      }};
    }}
  }}
  if(id==='eurostatLatest'){{
    const labels=chart.data.labels||[];
    const dkIndex=labels.indexOf('DK');
    const value=dkIndex>=0?Number(chart.data.datasets?.[0]?.data?.[dkIndex]):null;
    return {{
      valueText:Number.isFinite(value)?dkNumber(value,1)+' pct.':'',
      detailText:'Danmark | '+periodLabel(DATA.eurostat.latestPeriod)
    }};
  }}
  if(id==='eurostatTrend') detail='Danmark';
  if(id==='euConfidence') detail='Danmark';
  const valueText=point.value===null?'':dkNumber(point.value,decimals)+suffix;
  const periodText=point.period!==''?periodLabel(point.period):'';
  return {{valueText,detailText:[detail,periodText].filter(Boolean).join(' | ')}};
}}
const CHART_MEANING={{
  unemploymentTotal:{{label:'Bruttoledigheden',goodIfUp:false}}, unemploymentRate:{{label:'Ledighedsprocenten',goodIfUp:false}},
  unemploymentType:{{label:'Antallet på A-dagpenge',goodIfUp:false}}, longterm:{{label:'Langtidsledigheden',goodIfUp:false}},
  vacancies:{{label:'Antallet af nyopslåede stillinger',goodIfUp:true}}, wages:{{label:'Antallet af lønmodtagere',goodIfUp:true}},
  notices:{{label:'Antallet af varslede personer',goodIfUp:false}}, bankruptcies:{{label:'Antallet af konkurser',goodIfUp:false}},
  expiredBenefits:{{label:'Antallet med opbrugt dagpengeret',goodIfUp:false}}, workSharing:{{label:'Arbejdsfordelinger',goodIfUp:false}},
  failedRecruitment:{{label:'Forgæves rekrutteringsforsøg',goodIfUp:false}}, confidence:{{label:'Forbrugertilliden',goodIfUp:true}},
  business:{{label:'Erhvervstilliden',goodIfUp:true}}, inflation:{{label:'Inflationen',goodIfUp:false}},
  eurostatTrend:{{label:'Ledigheden i Danmark',goodIfUp:false}}, euConfidence:{{label:'Forbrugertilliden i Danmark',goodIfUp:true}}
}};
function previousChartPoint(chart,datasetIndex,latestIndex){{
  const labels=chart?.data?.labels||[]; const values=chart?.data?.datasets?.[datasetIndex]?.data||[];
  for(let i=Math.min(latestIndex-1,values.length-1);i>=0;i--){{const value=Number(values[i]);if(Number.isFinite(value)) return {{value,period:labels[i],index:i}};}}
  return {{value:null,period:'',index:-1}};
}}
function chartInsightText(id){{
  const chart=charts[id]; const summary=shareSummary(id);
  if(!chart) return '';
  if(id==='topRecruitmentOccupations' || id==='eurostatLatest') return ['Seneste billede:',summary.valueText,summary.detailText].filter(Boolean).join(' ')+'.';
  const latest=latestChartPoint(chart,0); const previous=previousChartPoint(chart,0,latest.index); const meaning=CHART_MEANING[id];
  if(!meaning || latest.value===null || previous.value===null) return ['Seneste:',summary.valueText,summary.detailText].filter(Boolean).join(' ')+'.';
  const delta=latest.value-previous.value; const direction=delta>0?'steget':delta<0?'faldet':'uændret';
  const assessment=delta===0?'stabilt':((delta>0)===meaning.goodIfUp?'en forbedring':'en forværring');
  return `${{meaning.label}} er ${{direction}} siden ${{periodLabel(previous.period)}}. Seneste observation er ${{summary.valueText||dkNumber(latest.value,1)}} (${{assessment}}).`;
}}
function updateChartInsights(){{
  document.querySelectorAll('#dak-dashboard .chart-card').forEach(card=>{{
    const canvas=card.querySelector('canvas'); if(!canvas) return;
    let insight=card.querySelector('.chart-insight');
    if(!insight){{insight=document.createElement('p'); insight.className='chart-insight'; card.querySelector('h3')?.insertAdjacentElement('afterend',insight);}}
    insight.textContent=chartInsightText(canvas.id);
    const title=(card.querySelector('h3')?.textContent||'Graf').trim();
    canvas.setAttribute('role','img'); canvas.setAttribute('aria-label',title+'. '+insight.textContent);
    canvas.textContent=title+'. '+insight.textContent;
  }});
}}
function wrapCanvasText(ctx,text,x,y,maxWidth,lineHeight,maxLines=2){{
  const words=String(text).split(/\\s+/);
  let line='', lines=[];
  for(const word of words){{
    const test=line?line+' '+word:word;
    if(ctx.measureText(test).width>maxWidth && line){{lines.push(line);line=word;}}
    else line=test;
  }}
  if(line) lines.push(line);
  if(lines.length>maxLines){{
    lines=lines.slice(0,maxLines);
    let last=lines[maxLines-1];
    while(last.length>1 && ctx.measureText(last+'...').width>maxWidth) last=last.slice(0,-1);
    lines[maxLines-1]=last+'...';
  }}
  lines.forEach((lineText,i)=>ctx.fillText(lineText,x,y+i*lineHeight));
  return y+lines.length*lineHeight;
}}
function exportChartCanvas(card,chartCanvas){{
  const title=(card.querySelector('h3')?.textContent||'Graf').trim();
  const source=(card.querySelector('.source')?.textContent||'').trim();
  const summary=shareSummary(chartCanvas.id);
  const width=1200, height=675;
  const output=document.createElement('canvas');
  output.width=width; output.height=height;
  const ctx=output.getContext('2d');
  ctx.fillStyle='#ffffff'; ctx.fillRect(0,0,width,height);

  ctx.fillStyle='#4a3428'; ctx.font='700 17px Segoe UI, Arial, sans-serif';
  ctx.fillText('ANALYTISK OVERBLIK | ARBEJDSMARKEDET',58,38);
  ctx.fillStyle='#9A5B34'; ctx.fillRect(58,53,72,4);

  ctx.fillStyle='#2B211B'; ctx.font='700 34px Segoe UI, Arial, sans-serif';
  const titleBottom=wrapCanvasText(ctx,title,58,91,width-116,40,2);
  let summaryY=Math.max(142,titleBottom+8);
  if(summary.valueText){{
    ctx.fillStyle='#4a3428'; ctx.font='800 38px Segoe UI, Arial, sans-serif';
    ctx.fillText(summary.valueText,58,summaryY);
    summaryY+=29;
  }}
  if(summary.detailText){{
    ctx.fillStyle='#6f6258'; ctx.font='600 18px Segoe UI, Arial, sans-serif';
    ctx.fillText(summary.detailText,58,summaryY);
    summaryY+=20;
  }}

  const chartTop=Math.max(205,summaryY+8);
  const chartBottom=536;
  ctx.drawImage(chartCanvas,58,chartTop,width-116,Math.max(210,chartBottom-chartTop));

  ctx.strokeStyle='#E6DDD2'; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(58,558); ctx.lineTo(width-58,558); ctx.stroke();
  ctx.fillStyle='#6f6258'; ctx.font='17px Segoe UI, Arial, sans-serif';
  const sourceText=source.length>120?source.slice(0,117)+'...':source;
  ctx.fillText(sourceText,58,589);
  ctx.fillStyle='#2B211B'; ctx.font='700 18px Segoe UI, Arial, sans-serif';
  ctx.fillText('Danske A-kasser',58,629);
  ctx.fillStyle='#6f6258'; ctx.font='17px Segoe UI, Arial, sans-serif';
  ctx.fillText('Analytisk overblik - Arbejdsmarkedet',205,629);
  ctx.textAlign='right';
  ctx.fillStyle='#4a3428'; ctx.font='600 16px Segoe UI, Arial, sans-serif';
  ctx.fillText('ai-michelklos.github.io/Dashboard/',width-58,629);
  ctx.textAlign='left';
  return {{canvas:output,title}};
}}
function fileNameFor(title){{
  return title.toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'')+'.png';
}}
function downloadBlob(blob,name){{
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download=name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}}
async function shareChart(card,chartCanvas){{
  const exported=exportChartCanvas(card,chartCanvas);
  const blob=await new Promise(resolve=>exported.canvas.toBlob(resolve,'image/png'));
  if(!blob) return;
  const file=new File([blob],fileNameFor(exported.title),{{type:'image/png'}});
  const shareUrl=location.origin+location.pathname+'share/'+chartCanvas.id+'/';
  if(navigator.share && navigator.canShare && navigator.canShare({{files:[file]}})){{
    try{{
      await navigator.share({{files:[file],title:exported.title,text:'Graf fra Analytisk overblik - Arbejdsmarkedet. '+shareUrl}});
      return;
    }}catch(error){{
      if(error && error.name==='AbortError') return;
    }}
  }}
  downloadBlob(blob,file.name);
}}
async function downloadChartImage(card,chartCanvas){{
  const exported=exportChartCanvas(card,chartCanvas);
  const blob=await new Promise(resolve=>exported.canvas.toBlob(resolve,'image/png'));
  if(blob) downloadBlob(blob,fileNameFor(exported.title));
}}
function downloadChartCsv(card,chartCanvas){{
  const chart=charts[chartCanvas.id];
  if(!chart) return;
  const datasets=chart.data.datasets||[];
  const rows=[['Periode',...datasets.map(item=>item.label||'Værdi')]];
  (chart.data.labels||[]).forEach((label,index)=>{{
    rows.push([periodLabel(label),...datasets.map(item=>item.data?.[index]??'')]);
  }});
  const csv='\ufeff'+rows.map(row=>row.map(value=>'"'+String(value).replace(/"/g,'""')+'"').join(';')).join('\\n');
  const title=(card.querySelector('h3')?.textContent||'graf').trim();
  downloadBlob(new Blob([csv],{{type:'text/csv;charset=utf-8'}}),fileNameFor(title).replace(/[.]png$/,'.csv'));
}}
async function copyChartLink(chartCanvas,button){{
  const url=location.origin+location.pathname+'share/'+chartCanvas.id+'/';
  try{{
    await navigator.clipboard.writeText(url);
    const original=button.textContent; button.textContent='Kopieret';
    setTimeout(()=>button.textContent=original,1400);
  }}catch(error){{
    prompt('Kopiér linket til grafen:',url);
  }}
}}
function setupChartSharing(){{
  document.querySelectorAll('#dak-dashboard .chart-card').forEach(card=>{{
    const canvas=card.querySelector('canvas');
    if(!canvas || card.querySelector('.chart-actions')) return;
    card.id=card.id||'graf-'+canvas.id;
    const actions=document.createElement('div'); actions.className='chart-actions';
    const button=document.createElement('button');
    button.type='button'; button.className='share-chart'; button.textContent='Del / hent';
    button.setAttribute('aria-label','Del eller hent '+(card.querySelector('h3')?.textContent||'graf'));
    button.setAttribute('aria-expanded','false');
    const menu=document.createElement('div'); menu.className='chart-menu'; menu.hidden=true;
    const items=[
      ['Del billede',()=>shareChart(card,canvas)],
      ['Kopiér link',event=>copyChartLink(canvas,event.currentTarget)],
      ['Hent billede (PNG)',()=>downloadChartImage(card,canvas)],
      ['Hent data (CSV)',()=>downloadChartCsv(card,canvas)]
    ];
    items.forEach(([label,handler])=>{{const item=document.createElement('button');item.type='button';item.textContent=label;item.addEventListener('click',async event=>{{await handler(event);menu.hidden=true;button.setAttribute('aria-expanded','false');}});menu.appendChild(item);}});
    button.addEventListener('click',()=>{{menu.hidden=!menu.hidden;button.setAttribute('aria-expanded',String(!menu.hidden));}});
    actions.append(button,menu); card.appendChild(actions);
  }});
  document.addEventListener('click',event=>{{
    document.querySelectorAll('.chart-actions').forEach(actions=>{{
      if(actions.contains(event.target)) return;
      const menu=actions.querySelector('.chart-menu'); const button=actions.querySelector('.share-chart');
      if(menu) menu.hidden=true; if(button) button.setAttribute('aria-expanded','false');
    }});
  }});
}}
setupChartSharing();
function focusSharedChart(){{
  const hash=location.hash;
  if(!hash.startsWith('#graf-')) return;
  const card=document.getElementById(hash.slice(1));
  if(!card) return;
  requestAnimationFrame(()=>{{
    card.scrollIntoView({{behavior:'smooth',block:'center'}});
    card.classList.add('share-target');
    setTimeout(()=>card.classList.remove('share-target'),2600);
  }});
}}
window.addEventListener('hashchange',focusSharedChart);
setTimeout(focusSharedChart,150);

window.setPeriod=function(n,button){{
  document.querySelectorAll('#dak-dashboard [data-months]').forEach(el=>{{el.classList.remove('active');el.setAttribute('aria-pressed','false');}});
  if(button){{button.classList.add('active');button.setAttribute('aria-pressed','true');}}
  drawAll(n);
  updateChartInsights();
}};
drawAll(60);
updateChartInsights();
}})();
</script>
<script data-goatcounter="https://arbejdsmarkedsoverblikket.goatcounter.com/count"
        async src="//gc.zgo.at/count.js"></script>
</body>
</html>"""


def validate(data, html):
    for name in (
        "unemployment",
        "vacancies",
        "notices",
        "longterm",
        "expiredBenefits",
        "workSharing",
        "failedRecruitment",
    ):
        assert len(data[name]["labels"]) >= 12
        assert data[name]["labels"] == sorted(data[name]["labels"])
    assert latest_valid(data["unemployment"]["labels"], data["unemployment"]["total"])[1] is not None
    assert latest_valid(data["vacancies"]["labels"], data["vacancies"]["values"])[1] is not None
    assert latest_valid(data["notices"]["labels"], data["notices"]["people"])[1] is not None
    assert latest_valid(data["longterm"]["labels"], data["longterm"]["total"])[1] is not None
    assert latest_valid(data["expiredBenefits"]["labels"], data["expiredBenefits"]["total"])[1] is not None
    for key in ("total", "oneYearGraduate", "otherGraduates", "otherUnemployed"):
        assert len(data["expiredBenefits"][key]) == len(data["expiredBenefits"]["labels"])
    assert latest_valid(data["workSharing"]["labels"], data["workSharing"]["total"])[1] is not None
    for key in ("under13Weeks", "over13Weeks", "total"):
        assert len(data["workSharing"][key]) == len(data["workSharing"]["labels"])
    assert latest_valid(
        data["failedRecruitment"]["labels"],
        data["failedRecruitment"]["attempts"],
    )[1] is not None
    for key in ("attempts", "rate"):
        assert len(data["failedRecruitment"][key]) == len(data["failedRecruitment"]["labels"])
    assert latest_valid(
        data["failedRecruitment"]["labels"],
        data["failedRecruitment"]["rate"],
    )[1] is not None
    assert len(data["failedRecruitment"]["topOccupations"]) == 15
    assert all(item["name"] and item["value"] is not None for item in data["failedRecruitment"]["topOccupations"])
    assert data["meta"]["officialApi"]["wages"]["dataset"] == "LBESK104"
    assert data["meta"]["officialApi"]["bankruptcies"]["dataset"] == "KONK3"
    assert data["meta"]["officialApi"]["consumerConfidence"]["dataset"] == "FORV1"
    assert data["meta"]["officialApi"]["jobindsats"]["tables"]["unemployment"] == "y25i03"
    assert data["meta"]["officialApi"]["jobindsats"]["tables"]["workSharing"] == "y25i06"
    assert data["meta"]["officialApi"]["jobindsats"]["tables"]["expiredBenefits"] == "y01ud01di"
    assert data["meta"]["officialApi"]["jobindsats"]["tables"]["failedRecruitment"]
    assert "NaN" not in html and "Infinity" not in html
    assert html.count("<canvas") == 18
    assert len(re.findall(r'class="kpi-card(?: priority)?"', html)) == 13
    assert html.count('class="table-card"') == 2
    assert "type:'category'" in html
    assert "indexAxis:'y'" in html
    assert "this.chart.data.labels" in html
    assert "left.label+' (venstre akse)'" in html
    assert "right.label+' (højre akse)'" in html
    assert "Arbejdsfordelinger under og over 13 uger" in html
    assert "Opbrugt dagpengeret fordelt på dimittendstatus" in html
    assert "15 stillinger med flest forgæves rekrutteringsforsøg" in html
    assert html.count("Grafik og databehandling: Michel Klos") == 1
    assert "setupChartSharing();" in html
    assert "navigator.canShare" in html
    assert "shareSummary(id)" in html
    assert "ANALYTISK OVERBLIK | ARBEJDSMARKEDET" in html
    assert "const width=1200, height=675" in html
    assert "share/'+chartCanvas.id+'/'" in html
    assert "share-target" in html
    assert "Det vigtigste lige nu" in html
    assert html.count('class="insight-period"') == 3
    assert 'class="insight-kicker">Lønmodtagere' in html
    assert 'class="insight-kicker">Forbrugertilliden' not in html
    assert 'class="section-nav"' in html
    assert html.count('class="chart-grid"') == 4
    assert 'chart-toggle' not in html
    assert 'data-collapsible' not in html
    assert 'data-preview' not in html
    assert 'setupChartToggles' not in html
    assert 'Vis færre grafer' not in html
    assert 'Vis alle grafer' not in html
    assert "Hent data (CSV)" in html
    assert "updateChartInsights();" in html
    assert "aria-pressed=\"true\"" in html
    assert "referenceLine" in html
    assert "–" not in html and "—" not in html


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dashboard_data = json_safe(build_data())
    dashboard_html = build_html(dashboard_data)
    validate(dashboard_data, dashboard_html)
    OUTPUT.write_text(dashboard_html, encoding="utf-8")
    DATA_OUTPUT.write_text(
        json.dumps(dashboard_data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")
