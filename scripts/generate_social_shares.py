from __future__ import annotations

import html
import json
import math
import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter


BASE = Path(__file__).resolve().parents[1]
DATA_PATH = BASE / "data" / "dashboard-data.json"
SHARE_DIR = BASE / "share"
PUBLIC_BASE = "https://ai-michelklos.github.io/Dashboard/"

COLORS = ["#6B9E78", "#E07A40", "#4A90C4", "#9B59B6", "#3d6b47", "#0F2B36"]
INK = "#0F2B36"
GREEN = "#3d6b47"
MUTED = "#68777d"
GRID = "#E8EBE8"

MONTHS = [
    "jan.", "feb.", "mar.", "apr.", "maj", "jun.",
    "jul.", "aug.", "sep.", "okt.", "nov.", "dec.",
]

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


def fmt_number(value, decimals=0):
    if value is None:
        return ""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value):
        return ""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_period(period):
    text = str(period)
    if len(text) == 7 and text[4] == "M" and text[5:].isdigit():
        month = int(text[5:])
        if 1 <= month <= 12:
            return f"{MONTHS[month - 1]} {text[:4]}"
    if len(text) == 7 and text[4] == "-" and text[5:].isdigit():
        month = int(text[5:])
        if 1 <= month <= 12:
            return f"{MONTHS[month - 1]} {text[:4]}"
    if len(text) == 7 and text[4:6] == "-Q":
        return f"Q{text[-1]} {text[:4]}"
    return text


def last_valid(labels, values):
    for label, value in zip(reversed(labels), reversed(values)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return str(label), number
    return "", None


def last_n(labels, series, n=60):
    start = max(0, len(labels) - n)
    return labels[start:], [values[start:] for values in series]


def make_specs(data):
    return [
        {
            "id": "unemploymentTotal",
            "title": "Bruttoledighed",
            "source": "Kilde: Danmarks Statistik og Jobindsats",
            "kind": "line",
            "labels": data["unemployment"]["labels"],
            "series": [("Bruttoledige", data["unemployment"]["total"])],
            "unit": " fuldtidspersoner",
            "decimals": 0,
        },
        {
            "id": "unemploymentRate",
            "title": "Ledighedsprocent",
            "source": "Kilde: Danmarks Statistik og Jobindsats",
            "kind": "line",
            "labels": data["unemployment"]["labels"],
            "series": [("Ledighed", data["unemployment"]["rate"])],
            "unit": " pct.",
            "decimals": 1,
        },
        {
            "id": "unemploymentType",
            "title": "Ledige fordelt på ydelse",
            "source": "Kilde: Jobindsats",
            "kind": "line",
            "labels": data["unemployment"]["labels"],
            "series": [
                ("A-dagpenge", data["unemployment"]["benefit"]),
                ("Kontanthjælp", data["unemployment"]["assistance"]),
            ],
            "unit": "",
            "decimals": 0,
        },
        {
            "id": "longterm",
            "title": "Langtidsledige",
            "source": "Kilde: Jobindsats",
            "kind": "line",
            "labels": data["longterm"]["labels"],
            "series": [
                ("I alt", data["longterm"]["total"]),
                ("A-dagpenge", data["longterm"]["benefit"]),
                ("Kontanthjælp", data["longterm"]["assistance"]),
            ],
            "unit": " personer",
            "decimals": 0,
        },
        {
            "id": "vacancies",
            "title": "Nyopslåede stillinger",
            "source": "Kilde: Jobindsats",
            "kind": "bar",
            "labels": data["vacancies"]["labels"],
            "series": [("Nyopslåede stillinger", data["vacancies"]["values"])],
            "unit": " stillinger",
            "decimals": 0,
        },
        {
            "id": "wages",
            "title": "Lønmodtagere",
            "source": "Kilde: Danmarks Statistik",
            "kind": "line",
            "labels": data["wages"]["labels"],
            "series": [
                ("I alt", data["wages"]["total"]),
                ("Offentlig sektor", data["wages"]["public"]),
                ("Privat sektor", data["wages"]["private"]),
            ],
            "unit": " personer",
            "decimals": 0,
        },
        {
            "id": "notices",
            "title": "Varslede afskedigelser",
            "source": "Kilde: Jobindsats",
            "kind": "dual",
            "labels": data["notices"]["labels"],
            "series": [
                ("Varslede personer", data["notices"]["people"]),
                ("Virksomheder", data["notices"]["companies"]),
            ],
            "unit": " personer",
            "decimals": 0,
        },
        {
            "id": "bankruptcies",
            "title": "Konkurser og tabte job",
            "source": "Kilde: Danmarks Statistik",
            "kind": "dual",
            "labels": data["bankruptcies"]["labels"],
            "series": [
                ("Konkurser, sæsonkorrigeret", data["bankruptcies"]["seasonal"]),
                ("Tabte job", data["bankruptcies"]["lostJobs"]),
            ],
            "unit": "",
            "decimals": 0,
        },
        {
            "id": "expiredBenefits",
            "title": "Opbrugt dagpengeret fordelt på dimittendstatus",
            "source": "Kilde: Jobindsats",
            "kind": "line",
            "labels": data["expiredBenefits"]["labels"],
            "series": [
                ("I alt", data["expiredBenefits"]["total"]),
                ("1 års dimittendret", data["expiredBenefits"]["oneYearGraduate"]),
                ("Øvrige dimittender", data["expiredBenefits"]["otherGraduates"]),
                ("Øvrige ledige", data["expiredBenefits"]["otherUnemployed"]),
            ],
            "unit": " personer",
            "decimals": 0,
        },
        {
            "id": "workSharing",
            "title": "Arbejdsfordelinger under og over 13 uger",
            "source": "Kilde: Jobindsats",
            "kind": "stacked",
            "labels": data["workSharing"]["labels"],
            "series": [
                ("Under 13 uger", data["workSharing"]["under13Weeks"]),
                ("Over 13 uger", data["workSharing"]["over13Weeks"]),
            ],
            "unit": " personer",
            "decimals": 0,
        },
        {
            "id": "failedRecruitment",
            "title": "Forgæves rekrutteringsforsøg over tid",
            "source": "Kilde: Jobindsats og STAR",
            "kind": "combo",
            "labels": data["failedRecruitment"]["labels"],
            "series": [
                ("Forgæves rekrutteringsforsøg", data["failedRecruitment"]["attempts"]),
                ("FRR", data["failedRecruitment"]["rate"]),
            ],
            "unit": " forsøg",
            "decimals": 0,
        },
        {
            "id": "topRecruitmentOccupations",
            "title": "15 stillinger med flest forgæves rekrutteringsforsøg",
            "source": "Kilde: Jobindsats og STAR",
            "kind": "horizontal",
            "labels": [item["name"] for item in data["failedRecruitment"]["topOccupations"]],
            "series": [(
                "Forgæves rekrutteringsforsøg",
                [item["value"] for item in data["failedRecruitment"]["topOccupations"]],
            )],
            "unit": " forsøg",
            "decimals": 0,
            "summary_period": data["failedRecruitment"].get("topPeriod", ""),
        },
        {
            "id": "confidence",
            "title": "Forbrugertillid",
            "source": "Kilde: Danmarks Statistik",
            "kind": "line",
            "labels": data["confidence"]["labels"],
            "series": [("Forbrugertillid", data["confidence"]["value"])],
            "unit": "",
            "decimals": 1,
        },
        {
            "id": "business",
            "title": "Erhvervstillid",
            "source": "Kilde: Danmarks Statistik",
            "kind": "line",
            "labels": data["business"]["labels"],
            "series": [("Erhvervstillid", data["business"]["value"])],
            "unit": "",
            "decimals": 1,
        },
        {
            "id": "inflation",
            "title": "Inflation",
            "source": "Kilde: Danmarks Statistik",
            "kind": "line",
            "labels": data["inflation"]["labels"],
            "series": [
                ("Forbrugerpriser", data["inflation"]["total"]),
                ("Kerneinflation", data["inflation"]["core"]),
            ],
            "unit": " pct.",
            "decimals": 1,
        },
        {
            "id": "eurostatTrend",
            "title": "Ledighed i udvalgte lande",
            "source": "Kilde: Eurostat",
            "kind": "line",
            "labels": data["eurostat"]["labels"],
            "series": [
                ("Danmark", data["eurostat"]["series"]["DK"]),
                ("EU-27", data["eurostat"]["series"]["EU27_2020"]),
                ("Tyskland", data["eurostat"]["series"]["DE"]),
                ("Sverige", data["eurostat"]["series"]["SE"]),
                ("Frankrig", data["eurostat"]["series"]["FR"]),
            ],
            "unit": " pct.",
            "decimals": 1,
        },
        {
            "id": "eurostatLatest",
            "title": "Ledighed i Europa, seneste kvartal",
            "source": "Kilde: Eurostat",
            "kind": "horizontal_countries",
            "labels": data["eurostat"]["latestLabels"],
            "series": [("Ledighed, pct.", data["eurostat"]["latestValues"])],
            "unit": " pct.",
            "decimals": 1,
            "summary_period": data["eurostat"].get("latestPeriod", ""),
        },
        {
            "id": "euConfidence",
            "title": "Forbrugertillid i udvalgte lande",
            "source": "Kilde: Europa-Kommissionen, DG ECFIN",
            "kind": "line",
            "labels": data["euConfidence"]["labels"],
            "series": [
                ("Danmark", data["euConfidence"]["series"]["DK"]),
                ("EU-27", data["euConfidence"]["series"]["EU27_2020"]),
                ("Tyskland", data["euConfidence"]["series"]["DE"]),
                ("Sverige", data["euConfidence"]["series"]["SE"]),
                ("Frankrig", data["euConfidence"]["series"]["FR"]),
            ],
            "unit": "",
            "decimals": 1,
        },
    ]


def summary_for(spec):
    if spec["id"] == "workSharing":
        period_a, a = last_valid(spec["labels"], spec["series"][0][1])
        period_b, b = last_valid(spec["labels"], spec["series"][1][1])
        if period_a and period_a == period_b and a is not None and b is not None:
            return f"{fmt_number(a + b, 0)} personer", f"I alt | {fmt_period(period_a)}"

    if spec["id"] == "topRecruitmentOccupations":
        values = spec["series"][0][1]
        if values:
            best_index = max(range(len(values)), key=lambda i: float(values[i] or 0))
            return (
                f"{fmt_number(values[best_index], 0)} forsøg",
                f"Flest: {spec['labels'][best_index]} | {fmt_period(spec.get('summary_period', ''))}",
            )

    if spec["id"] == "eurostatLatest":
        labels = spec["labels"]
        values = spec["series"][0][1]
        if "DK" in labels:
            index = labels.index("DK")
            return (
                f"{fmt_number(values[index], 1)} pct.",
                f"Danmark | {fmt_period(spec.get('summary_period', ''))}",
            )

    period, value = last_valid(spec["labels"], spec["series"][0][1])
    if value is None:
        return "", ""

    detail = spec["series"][0][0]
    if spec["id"] in {"eurostatTrend", "euConfidence"}:
        detail = "Danmark"

    return (
        f"{fmt_number(value, spec.get('decimals', 0))}{spec.get('unit', '')}",
        " | ".join(part for part in [detail, fmt_period(period)] if part),
    )


def configure_axis(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(axis="both", labelsize=8, colors=MUTED)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _pos: fmt_number(x, 0)))


def set_time_ticks(ax, labels):
    if not labels:
        return
    count = min(8, len(labels))
    if count == 1:
        indices = [0]
    else:
        indices = sorted(set(round(i * (len(labels) - 1) / (count - 1)) for i in range(count)))
    ax.set_xticks(indices)
    ax.set_xticklabels([fmt_period(labels[i]) for i in indices], rotation=0, ha="center")


def draw_chart(ax, spec):
    kind = spec["kind"]

    if kind in {"line", "bar", "stacked", "dual", "combo"}:
        n = 20 if spec["id"] in {"eurostatTrend", "failedRecruitment"} else 60
        labels, series_values = last_n(spec["labels"], [s[1] for s in spec["series"]], n)
        series = [(spec["series"][i][0], series_values[i]) for i in range(len(series_values))]
        x = list(range(len(labels)))
        set_time_ticks(ax, labels)
    else:
        labels = spec["labels"]
        series = spec["series"]

    if kind == "line":
        for index, (label, values) in enumerate(series):
            ax.plot(
                x,
                values,
                label=label,
                color=COLORS[index % len(COLORS)],
                linewidth=2.0,
            )
        configure_axis(ax)
        if len(series) > 1:
            ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=min(3, len(series)))

    elif kind == "bar":
        ax.bar(x, series[0][1], color=COLORS[0], width=0.78)
        configure_axis(ax)

    elif kind == "stacked":
        first = series[0][1]
        second = series[1][1]
        ax.bar(x, first, label=series[0][0], color=COLORS[0], width=0.78)
        ax.bar(x, second, bottom=first, label=series[1][0], color=COLORS[1], width=0.78)
        configure_axis(ax)
        ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=2)

    elif kind == "dual":
        first_label, first_values = series[0]
        second_label, second_values = series[1]
        ax.plot(x, first_values, color=COLORS[0], linewidth=2.0, label=first_label)
        configure_axis(ax)
        ax2 = ax.twinx()
        ax2.bar(x, second_values, color=COLORS[1], alpha=0.35, width=0.72, label=second_label)
        ax2.tick_params(axis="y", labelsize=8, colors=MUTED)
        ax2.spines["top"].set_visible(False)
        ax2.spines["right"].set_color(GRID)
        ax2.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: fmt_number(value, 0)))
        lines, line_labels = ax.get_legend_handles_labels()
        bars, bar_labels = ax2.get_legend_handles_labels()
        ax.legend(lines + bars, line_labels + bar_labels, loc="upper left", fontsize=8, frameon=False, ncol=2)

    elif kind == "combo":
        first_label, first_values = series[0]
        second_label, second_values = series[1]
        ax.bar(x, first_values, color=COLORS[0], alpha=0.78, width=0.72, label=first_label)
        configure_axis(ax)
        ax2 = ax.twinx()
        ax2.plot(x, second_values, color=COLORS[1], linewidth=2.2, label=second_label)
        ax2.tick_params(axis="y", labelsize=8, colors=MUTED)
        ax2.spines["top"].set_visible(False)
        ax2.spines["right"].set_color(GRID)
        ax2.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: fmt_number(value, 1)))
        lines, line_labels = ax.get_legend_handles_labels()
        second_lines, second_labels = ax2.get_legend_handles_labels()
        ax.legend(lines + second_lines, line_labels + second_labels, loc="upper left", fontsize=8, frameon=False, ncol=2)

    elif kind == "horizontal":
        values = series[0][1]
        positions = list(range(len(labels)))
        ax.barh(positions, values, color=COLORS[2], height=0.72)
        ax.set_yticks(positions)
        ax.set_yticklabels(labels, fontsize=6.8)
        ax.invert_yaxis()
        configure_axis(ax)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.grid(axis="y", visible=False)

    elif kind == "horizontal_countries":
        values = series[0][1]
        country_labels = [COUNTRY_NAMES.get(code, code) for code in labels]
        positions = list(range(len(labels)))
        colors = [COLORS[1] if code == "DK" else COLORS[0] for code in labels]
        ax.barh(positions, values, color=colors, height=0.7)
        ax.set_yticks(positions)
        ax.set_yticklabels(country_labels, fontsize=6.8)
        ax.invert_yaxis()
        configure_axis(ax)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.grid(axis="y", visible=False)


def write_image(spec):
    value_text, detail_text = summary_for(spec)

    fig = plt.figure(figsize=(12, 6.75), dpi=100, facecolor="white")
    fig.text(0.075, 0.955, "ANALYTISK OVERBLIK | ARBEJDSMARKEDET", fontsize=10, fontweight="bold", color=GREEN)
    fig.lines.append(plt.Line2D([0.075, 0.135], [0.935, 0.935], transform=fig.transFigure, color=COLORS[0], linewidth=3))

    title = "\n".join(textwrap.wrap(spec["title"], width=62))
    fig.text(0.075, 0.885, title, fontsize=21, fontweight="bold", color=INK, va="top")

    if value_text:
        fig.text(0.075, 0.765, value_text, fontsize=24, fontweight="bold", color=GREEN)
    if detail_text:
        fig.text(0.075, 0.72, detail_text, fontsize=11, fontweight="bold", color=MUTED)

    ax = fig.add_axes([0.075, 0.20, 0.85, 0.43])
    draw_chart(ax, spec)

    fig.lines.append(plt.Line2D([0.075, 0.925], [0.16, 0.16], transform=fig.transFigure, color=GRID, linewidth=1))
    fig.text(0.075, 0.125, spec["source"], fontsize=9, color=MUTED)
    fig.text(0.075, 0.065, "Danske A-kasser", fontsize=10, fontweight="bold", color=INK)
    fig.text(0.195, 0.065, "Analytisk overblik - Arbejdsmarkedet", fontsize=9, color=MUTED)
    fig.text(0.925, 0.065, "ai-michelklos.github.io/Dashboard/", fontsize=8.5, color=GREEN, ha="right")

    output = SHARE_DIR / f"{spec['id']}.png"
    fig.savefig(output, dpi=100, facecolor="white")
    plt.close(fig)
    return value_text, detail_text


def write_share_page(spec, value_text, detail_text):
    share_url = f"{PUBLIC_BASE}share/{spec['id']}/"
    image_url = f"{PUBLIC_BASE}share/{spec['id']}.png"
    target_url = f"{PUBLIC_BASE}#graf-{spec['id']}"
    description = " | ".join(part for part in [value_text, detail_text] if part)
    if description:
        description += ". "
    description += "Se grafen i Analytisk overblik - Arbejdsmarkedet."

    target_dir = SHARE_DIR / spec["id"]
    target_dir.mkdir(parents=True, exist_ok=True)
    page = f"""<!doctype html>
<html lang="da">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(spec['title'])} | Analytisk overblik</title>
  <meta name="description" content="{html.escape(description, quote=True)}">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{html.escape(spec['title'], quote=True)}">
  <meta property="og:description" content="{html.escape(description, quote=True)}">
  <meta property="og:url" content="{html.escape(share_url, quote=True)}">
  <meta property="og:image" content="{html.escape(image_url, quote=True)}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="675">
  <meta property="og:image:alt" content="{html.escape(spec['title'], quote=True)}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{html.escape(spec['title'], quote=True)}">
  <meta name="twitter:description" content="{html.escape(description, quote=True)}">
  <meta name="twitter:image" content="{html.escape(image_url, quote=True)}">
  <link rel="canonical" href="{html.escape(target_url, quote=True)}">
  <style>
    body {{font-family:Segoe UI,Arial,sans-serif; background:#f5f7f5; color:#0F2B36; margin:0; padding:40px}}
    main {{max-width:720px; margin:0 auto; background:#fff; padding:28px; border-radius:12px}}
    img {{max-width:100%; height:auto; display:block; border:1px solid #E8EBE8}}
    a {{color:#3d6b47; font-weight:700}}
  </style>
  <script>window.addEventListener('DOMContentLoaded',function(){{window.location.replace({json.dumps(target_url)});}});</script>
</head>
<body>
  <main>
    <h1>{html.escape(spec['title'])}</h1>
    <p>{html.escape(description)}</p>
    <img src="../{spec['id']}.png" alt="{html.escape(spec['title'], quote=True)}">
    <p><a href="{html.escape(target_url, quote=True)}">Åbn grafen i dashboardet</a></p>
  </main>
</body>
</html>
"""
    (target_dir / "index.html").write_text(page, encoding="utf-8")


def main():
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    specs = make_specs(data)
    if len(specs) != 18:
        raise RuntimeError(f"Forventede 18 grafer, fandt {len(specs)}")

    for spec in specs:
        value_text, detail_text = write_image(spec)
        write_share_page(spec, value_text, detail_text)

    for spec in specs:
        image = SHARE_DIR / f"{spec['id']}.png"
        page = SHARE_DIR / spec["id"] / "index.html"
        if not image.exists() or image.stat().st_size < 10_000:
            raise RuntimeError(f"Ugyldigt share-billede: {image}")
        if not page.exists() or "og:image" not in page.read_text(encoding="utf-8"):
            raise RuntimeError(f"Ugyldig share-side: {page}")

    print(f"Genererede {len(specs)} social-billeder og share-sider")


if __name__ == "__main__":
    main()
