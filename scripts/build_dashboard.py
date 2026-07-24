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
        cls, arrow = "delta-neutral", "●"
    else:
        up = delta > 0
        cls = "delta-good" if up == good_if_up else "delta-bad"
        arrow = "▲" if up else "▼"
    return (
        f'<span class="{cls}">{arrow} {fmt_number(abs(delta), decimals)}</span>'
        f'<span class="compare-label"> siden {fmt_period(comparison_period)}</span>'
    )


def card(title, series, unit="", decimals=0, good_if_up=True, note=""):
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
      <article class="kpi-card">
        <div class="kpi-title">{title}</div>
        <div class="kpi-value">{fmt_number(current["value"], decimals)}<span>{suffix}</span></div>
        <div class="kpi-period">{fmt_period(current["period"])}</div>
        <div class="kpi-deltas">{''.join(rows)}</div>
        {f'<div class="kpi-note">{note}</div>' if note else ''}
      </article>"""


def build_data():
    if DATA_OUTPUT.exists():
        return refresh_jobindsats(json.loads(DATA_OUTPUT.read_text(encoding="utf-8")))

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
    kpi_cards = [
        card(
            "Bruttoledige",
            {"labels": data["unemployment"]["labels"], "values": data["unemployment"]["total"]},
            "fuldtidspersoner", 0, False, "Sæsonkorrigeret",
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
        card("Nyopslåede stillinger", data["vacancies"], "stillinger", 0, True),
        card(
            "Lønmodtagere",
            {"labels": data["wages"]["labels"], "values": data["wages"]["total"]},
            "personer", 0, True, "Sæsonkorrigeret",
        ),
        card(
            "Varslede afskedigelser",
            {"labels": data["notices"]["labels"], "values": data["notices"]["people"]},
            "personer", 0, False,
        ),
        card(
            "Tabte job ved konkurser",
            {"labels": data["bankruptcies"]["labels"], "values": data["bankruptcies"]["lostJobs"]},
            "job", 0, False,
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
    json_data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    country_names = json.dumps(COUNTRY_NAMES, ensure_ascii=False, separators=(",", ":"))

    return f"""<!doctype html>
<html lang="da">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Analytisk overblik - Arbejdsmarkedet</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    #dak-dashboard {{
      --navy:#13263f; --blue:#0076a8; --cyan:#42b6d5; --red:#e34a45;
      --green:#24895b; --orange:#ef8b2c; --purple:#7355a3; --ink:#202b38;
      --muted:#667483; --line:#dce3e8; --paper:#fff; --soft:#f3f7f9;
      max-width:1280px; margin:0 auto; padding:24px; color:var(--ink);
      font-family:Arial,Helvetica,sans-serif; box-sizing:border-box;
    }}
    #dak-dashboard * {{box-sizing:border-box}}
    #dak-dashboard h1 {{font-size:32px; line-height:1.15; color:var(--navy); margin:0 0 8px}}
    #dak-dashboard h2 {{font-size:23px; color:var(--navy); margin:44px 0 16px; border-bottom:3px solid var(--blue); padding-bottom:8px}}
    #dak-dashboard h3 {{font-size:18px; color:var(--navy); margin:0 0 6px}}
    #dak-dashboard .intro {{color:var(--muted); margin:0 0 18px; line-height:1.55}}
    #dak-dashboard .toolbar {{display:flex; flex-wrap:wrap; align-items:center; gap:9px; padding:14px 16px; background:var(--soft); border-radius:8px; margin:18px 0 24px}}
    #dak-dashboard .toolbar-label {{font-weight:700; margin-right:4px}}
    #dak-dashboard button {{border:1px solid var(--blue); background:#fff; color:var(--blue); border-radius:5px; padding:8px 13px; font-weight:700; cursor:pointer}}
    #dak-dashboard button.active, #dak-dashboard button:hover {{background:var(--blue); color:#fff}}
    #dak-dashboard .updated {{margin-left:auto; color:var(--muted); font-size:13px}}
    #dak-dashboard .kpi-grid {{display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px}}
    #dak-dashboard .kpi-grid.economy {{grid-template-columns:repeat(3,minmax(0,1fr))}}
    #dak-dashboard .kpi-grid.international {{grid-template-columns:repeat(2,minmax(0,1fr))}}
    #dak-dashboard .kpi-card, #dak-dashboard .table-card {{background:var(--paper); border:1px solid var(--line); border-top:5px solid var(--blue); border-radius:7px; padding:16px; box-shadow:0 2px 7px rgba(19,38,63,.08)}}
    #dak-dashboard .kpi-title {{font-weight:700; color:var(--navy); min-height:38px}}
    #dak-dashboard .kpi-value {{font-size:28px; color:var(--navy); font-weight:800; line-height:1.15; margin:8px 0 2px}}
    #dak-dashboard .kpi-value span {{font-size:13px; font-weight:400; color:var(--muted)}}
    #dak-dashboard .kpi-period {{font-size:13px; color:var(--muted); margin-bottom:10px}}
    #dak-dashboard .kpi-deltas {{border-top:1px solid var(--line); padding-top:8px; min-height:49px}}
    #dak-dashboard .delta-row {{font-size:12px; margin:3px 0}}
    #dak-dashboard .delta-good {{color:var(--green); font-weight:700}}
    #dak-dashboard .delta-bad {{color:var(--red); font-weight:700}}
    #dak-dashboard .delta-neutral {{color:var(--muted); font-weight:700}}
    #dak-dashboard .compare-label {{color:var(--muted)}}
    #dak-dashboard .kpi-note {{font-size:11px; color:var(--muted); margin-top:7px}}
    #dak-dashboard .chart-grid {{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; margin-top:20px}}
    #dak-dashboard .chart-card {{background:#fff; border:1px solid var(--line); border-radius:7px; padding:18px; box-shadow:0 2px 7px rgba(19,38,63,.06)}}
    #dak-dashboard .chart-card.wide {{grid-column:1/-1}}
    #dak-dashboard .chart-wrap {{position:relative; height:390px}}
    #dak-dashboard .chart-card.wide .chart-wrap {{height:430px}}
    #dak-dashboard .chart-card.tall .chart-wrap {{height:650px}}
    #dak-dashboard .source {{font-size:11px; color:var(--muted); line-height:1.5; margin:10px 0 0}}
    #dak-dashboard table {{width:100%; border-collapse:collapse; margin-top:5px}}
    #dak-dashboard th, #dak-dashboard td {{border-bottom:1px solid var(--line); padding:8px 4px; text-align:left; font-size:14px}}
    #dak-dashboard td {{text-align:right; font-weight:700; color:var(--navy)}}
    #dak-dashboard .footnote {{margin-top:30px; background:var(--soft); padding:16px; border-left:4px solid var(--blue); font-size:13px; color:var(--muted); line-height:1.5}}
    @media(max-width:900px) {{
      #dak-dashboard .kpi-grid, #dak-dashboard .kpi-grid.economy {{grid-template-columns:repeat(2,minmax(0,1fr))}}
      #dak-dashboard .chart-grid {{grid-template-columns:1fr}}
      #dak-dashboard .chart-card.wide {{grid-column:auto}}
    }}
    @media(max-width:560px) {{
      #dak-dashboard {{padding:14px}}
      #dak-dashboard h1 {{font-size:27px}}
      #dak-dashboard .kpi-grid, #dak-dashboard .kpi-grid.economy, #dak-dashboard .kpi-grid.international {{grid-template-columns:1fr}}
      #dak-dashboard .updated {{width:100%; margin:6px 0 0}}
      #dak-dashboard .chart-wrap, #dak-dashboard .chart-card.wide .chart-wrap {{height:340px}}
    }}
  </style>
</head>
<body>
<main id="dak-dashboard">
  <h1>Analytisk overblik - Arbejdsmarkedet</h1>
  <p class="intro">Udvalgte nøgletal for arbejdsmarkedet, samfundsøkonomien og den internationale udvikling. Tallene opdateres i takt med de enkelte kilders offentliggørelser.</p>
  <div class="toolbar">
    <span class="toolbar-label">Vis periode:</span>
    <button type="button" data-months="12" onclick="setPeriod(12,this)">12 måneder</button>
    <button type="button" data-months="36" onclick="setPeriod(36,this)">36 måneder</button>
    <button type="button" class="active" data-months="60" onclick="setPeriod(60,this)">60 måneder</button>
    <span class="updated">Opdateret {data["meta"]["updated"]}</span>
  </div>

  <h2>Ledighed og beskæftigelse</h2>
  <div class="kpi-grid">{''.join(kpi_cards)}</div>
  <div class="chart-grid">
    <article class="chart-card"><h3>Bruttoledighed</h3><div class="chart-wrap"><canvas id="unemploymentTotal"></canvas></div><p class="source">Kilde: Danmarks Statistik og Jobindsats<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card"><h3>Ledighedsprocent</h3><div class="chart-wrap"><canvas id="unemploymentRate"></canvas></div><p class="source">Kilde: Danmarks Statistik og Jobindsats<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card"><h3>Ledige fordelt på ydelse</h3><div class="chart-wrap"><canvas id="unemploymentType"></canvas></div><p class="source">Kilde: Jobindsats<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card"><h3>Langtidsledige</h3><div class="chart-wrap"><canvas id="longterm"></canvas></div><p class="source">Kilde: Jobindsats<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card"><h3>Nyopslåede stillinger</h3><div class="chart-wrap"><canvas id="vacancies"></canvas></div><p class="source">Kilde: Jobindsats<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card"><h3>Lønmodtagere</h3><div class="chart-wrap"><canvas id="wages"></canvas></div><p class="source">Kilde: Danmarks Statistik<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card"><h3>Varslede afskedigelser</h3><div class="chart-wrap"><canvas id="notices"></canvas></div><p class="source">Kilde: Jobindsats<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card"><h3>Konkurser og tabte job</h3><div class="chart-wrap"><canvas id="bankruptcies"></canvas></div><p class="source">Kilde: Danmarks Statistik<br>Grafik og databehandling: Danske A-kasser</p></article>
  </div>

  <h2>Samfundsøkonomi</h2>
  <div class="kpi-grid economy">{''.join(economy_cards)}</div>
  <div class="chart-grid">
    <article class="chart-card"><h3>Forbrugertillid</h3><div class="chart-wrap"><canvas id="confidence"></canvas></div><p class="source">Kilde: Danmarks Statistik<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card"><h3>Erhvervstillid</h3><div class="chart-wrap"><canvas id="business"></canvas></div><p class="source">Kilde: Danmarks Statistik<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card wide"><h3>Inflation</h3><div class="chart-wrap"><canvas id="inflation"></canvas></div><p class="source">Kilde: Danmarks Statistik<br>Grafik og databehandling: Danske A-kasser</p></article>
  </div>

  <h2>Internationalt</h2>
  <div class="kpi-grid international">{international_table(data, "eurostat")}{international_table(data, "confidence")}</div>
  <div class="chart-grid">
    <article class="chart-card wide"><h3>Ledighed i udvalgte lande</h3><div class="chart-wrap"><canvas id="eurostatTrend"></canvas></div><p class="source">Kilde: Eurostat<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card wide tall"><h3>Ledighed i Europa, seneste kvartal</h3><div class="chart-wrap"><canvas id="eurostatLatest"></canvas></div><p class="source">Kilde: Eurostat<br>Grafik og databehandling: Danske A-kasser</p></article>
    <article class="chart-card wide"><h3>Forbrugertillid i udvalgte lande</h3><div class="chart-wrap"><canvas id="euConfidence"></canvas></div><p class="source">Kilde: Europa-Kommissionen, DG ECFIN<br>Grafik og databehandling: Danske A-kasser</p></article>
  </div>
  <div class="footnote">Bemærk: Serierne offentliggøres på forskellige tidspunkter. Den seneste periode kan derfor variere mellem nøgletal. Ændringer på KPI-kortene er angivet i enheder, ikke i procent.</div>
</main>
<script>
(function(){{
const DATA={json_data};
const COUNTRY_NAMES={country_names};
const COLORS=['#0076a8','#e34a45','#24895b','#ef8b2c','#7355a3','#42b6d5','#13263f'];
const charts={{}};
let activeMonths=60;

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
      legend:{{position:'top',labels:{{boxWidth:13,usePointStyle:true}}}},
      tooltip:{{callbacks:{{title:items=>periodLabel(items[0].label),label:ctx=>' '+ctx.dataset.label+': '+dkNumber(ctx.parsed.y,decimals)}}}}
    }},
    scales:{{x:xAxis(),y:{{beginAtZero:false,ticks:{{callback:v=>dkNumber(v,decimals)}}}}}}
  }};
}}
function replaceChart(id,config){{
  if(charts[id]) charts[id].destroy();
  charts[id]=new Chart(document.getElementById(id),config);
}}
function lineChart(id,labels,datasets,decimals=0){{
  replaceChart(id,{{type:'line',data:{{labels,datasets:datasets.map((d,i)=>({{
    ...d,borderColor:d.borderColor||COLORS[i],backgroundColor:d.backgroundColor||COLORS[i],
    pointRadius:0,pointHoverRadius:4,borderWidth:2,tension:.18,spanGaps:true
  }}))}},options:commonOptions(decimals)}});
}}
function barChart(id,labels,datasets,decimals=0){{
  const opts=commonOptions(decimals); opts.scales.y.beginAtZero=true;
  replaceChart(id,{{type:'bar',data:{{labels,datasets:datasets.map((d,i)=>({{
    ...d,backgroundColor:d.backgroundColor||COLORS[i],borderRadius:2
  }}))}},options:opts}});
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
    {{label:right.label+' (højre akse)',data:right.data,borderColor:COLORS[1],backgroundColor:'rgba(227,74,69,.32)',type:'bar',borderRadius:2,yAxisID:'y1'}}
  ]}},options:opts}});
}}
function horizontalBar(id,labels,values){{
  replaceChart(id,{{
    type:'bar',
    data:{{labels,datasets:[{{label:'Ledighed, pct.',data:values,backgroundColor:labels.map(v=>v==='DK'?COLORS[1]:COLORS[0]),borderRadius:2}}]}},
    options:{{
      indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>' '+dkNumber(ctx.parsed.x,1)+' pct.'}}}}}},
      scales:{{
        x:{{beginAtZero:true,ticks:{{callback:v=>dkNumber(v,1)+' %'}}}},
        y:{{grid:{{display:false}},ticks:{{callback:function(val,idx){{const labels=this.chart.data.labels||[];const code=labels[val]??labels[idx]??val;return COUNTRY_NAMES[code]||code;}}}}}}
      }}
    }}
  }});
}}
function drawAll(n){{
  activeMonths=n;
  let s=sliced(DATA.unemployment.labels,[DATA.unemployment.total],n);
  lineChart('unemploymentTotal',s.labels,[{{label:'Bruttoledige',data:s.series[0]}}]);
  s=sliced(DATA.unemployment.labels,[DATA.unemployment.rate],n);
  lineChart('unemploymentRate',s.labels,[{{label:'Ledighed',data:s.series[0]}}],1);
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
  s=sliced(DATA.confidence.labels,[DATA.confidence.value],n);
  lineChart('confidence',s.labels,[{{label:'Forbrugertillid',data:s.series[0]}}],1);
  s=sliced(DATA.business.labels,[DATA.business.value],n);
  lineChart('business',s.labels,[{{label:'Erhvervstillid',data:s.series[0]}}],1);
  s=sliced(DATA.inflation.labels,[DATA.inflation.total,DATA.inflation.core],n);
  lineChart('inflation',s.labels,[{{label:'Forbrugerpriser',data:s.series[0]}},{{label:'Kerneinflation',data:s.series[1]}}],1);
  const quarters=Math.max(4,Math.ceil(n/3));
  s=sliced(DATA.eurostat.labels,[DATA.eurostat.series.DK,DATA.eurostat.series.EU27_2020,DATA.eurostat.series.DE,DATA.eurostat.series.SE,DATA.eurostat.series.FR],quarters);
  lineChart('eurostatTrend',s.labels,[{{label:'Danmark',data:s.series[0]}},{{label:'EU-27',data:s.series[1]}},{{label:'Tyskland',data:s.series[2]}},{{label:'Sverige',data:s.series[3]}},{{label:'Frankrig',data:s.series[4]}}],1);
  horizontalBar('eurostatLatest',DATA.eurostat.latestLabels,DATA.eurostat.latestValues);
  s=sliced(DATA.euConfidence.labels,[DATA.euConfidence.series.DK,DATA.euConfidence.series.EU27_2020,DATA.euConfidence.series.DE,DATA.euConfidence.series.SE,DATA.euConfidence.series.FR],n);
  lineChart('euConfidence',s.labels,[{{label:'Danmark',data:s.series[0]}},{{label:'EU-27',data:s.series[1]}},{{label:'Tyskland',data:s.series[2]}},{{label:'Sverige',data:s.series[3]}},{{label:'Frankrig',data:s.series[4]}}],1);
}}
window.setPeriod=function(n,button){{
  document.querySelectorAll('#dak-dashboard [data-months]').forEach(el=>el.classList.remove('active'));
  if(button) button.classList.add('active');
  drawAll(n);
}};
drawAll(60);
}})();
</script>
</body>
</html>"""


def validate(data, html):
    for name in ("unemployment", "vacancies", "notices", "longterm"):
        assert len(data[name]["labels"]) >= 12
        assert data[name]["labels"] == sorted(data[name]["labels"])
    assert latest_valid(data["unemployment"]["labels"], data["unemployment"]["total"])[1] is not None
    assert latest_valid(data["vacancies"]["labels"], data["vacancies"]["values"])[1] is not None
    assert latest_valid(data["notices"]["labels"], data["notices"]["people"])[1] is not None
    assert latest_valid(data["longterm"]["labels"], data["longterm"]["total"])[1] is not None
    assert data["meta"]["officialApi"]["wages"]["dataset"] == "LBESK104"
    assert data["meta"]["officialApi"]["bankruptcies"]["dataset"] == "KONK3"
    assert data["meta"]["officialApi"]["consumerConfidence"]["dataset"] == "FORV1"
    assert data["meta"]["officialApi"]["jobindsats"]["tables"]["unemployment"] == "y25i03"
    assert "NaN" not in html and "Infinity" not in html
    assert html.count("<canvas") == 14
    assert html.count('class="kpi-card"') == 10
    assert html.count('class="table-card"') == 2
    assert "type:'category'" in html
    assert "indexAxis:'y'" in html
    assert "this.chart.data.labels" in html
    assert "left.label+' (venstre akse)'" in html
    assert "right.label+' (højre akse)'" in html
    assert html.count("Grafik og databehandling: Danske A-kasser") == 14
    assert "–" not in html and "—" not in html


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dashboard_data = build_data()
    dashboard_html = build_html(dashboard_data)
    validate(dashboard_data, dashboard_html)
    OUTPUT.write_text(dashboard_html, encoding="utf-8")
    DATA_OUTPUT.write_text(
        json.dumps(dashboard_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")
