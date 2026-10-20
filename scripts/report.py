#!/usr/bin/env python3
"""SpaceKnife stats: release download snapshots, the Markdown report and the Pages site.

Runs in .github/workflows/stats.yml after jgehrcke/github-repo-stats, in this repo's checkout:

    python3 scripts/report.py --repo DuskXi/SpaceKnife [--site _site] [--pages-url URL] [--no-fetch]

- Appends today's download count of every release file to <repo>/downloads/downloads.csv. GitHub only
  keeps running totals, so the history is these daily snapshots; a second run on the same day
  replaces that day's rows.
- Reads github-repo-stats' data in <repo>/ghrs-data/ (daily views and clones, stars, forks, the
  latest referrers and paths).
- Writes README.md and charts/*.svg; with --site, a static page plus github-repo-stats' HTML report.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import re
import shutil
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

API = "https://api.github.com"
DL_FIELDS = ["date", "tag", "prerelease", "asset", "downloads"]
# checksum and zsync files are fetched by tools, not people: listed, but not counted as downloads
NOT_COUNTED = re.compile(r"^SHA256SUMS|\.zsync$", re.I)
COLORS = {"a": "#8b9cff", "b": "#5ec8b8", "c": "#f2a65a"}
TABLE_DAYS = 30
CHART_DAYS = 90


# ------------------------------------------------------------------ download snapshots


def fetch_releases(repo: str, token: str | None) -> list[dict]:
    url: str | None = f"{API}/repos/{repo}/releases?per_page=100"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "spaceknife-stats",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    releases: list[dict] = []
    while url:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            releases += json.load(r)
            nxt = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link") or "")
            url = nxt.group(1) if nxt else None
    return [x for x in releases if not x.get("draft")]


def snapshot_downloads(path: Path, releases: list[dict], day: str) -> None:
    rows = read_csv(path)
    rows = [r for r in rows if r["date"] != day]
    for rel in releases:
        for a in rel.get("assets", []):
            rows.append(
                {
                    "date": day,
                    "tag": rel["tag_name"],
                    "prerelease": "true" if rel.get("prerelease") else "false",
                    "asset": a["name"],
                    "downloads": str(a["download_count"]),
                }
            )
    rows.sort(key=lambda r: (r["date"], r["tag"], r["asset"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DL_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------ data


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def day_of(stamp: str) -> str:
    return stamp[:10]


def days_back(end: str, n: int) -> list[str]:
    last = dt.date.fromisoformat(end)
    return [(last - dt.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def cumulative_on(points: list[tuple[str, int]], days: list[str]) -> list[int | None]:
    """A cumulative series (only days with a change) carried forward over ``days``."""
    out: list[int | None] = []
    i, value = 0, None
    for d in days:
        while i < len(points) and points[i][0] <= d:
            value = points[i][1]
            i += 1
        out.append(value)
    return out


class Data:
    def __init__(self, root: Path) -> None:
        g = root / "ghrs-data"
        self.traffic: dict[str, tuple[int, int, int, int]] = {}
        for r in read_csv(g / "views_clones_aggregate.csv"):
            self.traffic[day_of(r["time_iso8601"])] = (
                int(float(r["views_total"] or 0)),
                int(float(r["views_unique"] or 0)),
                int(float(r["clones_total"] or 0)),
                int(float(r["clones_unique"] or 0)),
            )
        self.stars = [(day_of(r["time_iso8601"]), int(float(r["stars_cumulative"]))) for r in read_csv(g / "stargazers.csv")]
        self.forks = [(day_of(r["time_iso8601"]), int(float(r["forks_cumulative"]))) for r in read_csv(g / "forks.csv")]
        snaps = sorted((g / "snapshots").glob("*_top_*_snapshot.csv")) if (g / "snapshots").exists() else []
        self.referrers = read_csv(next((p for p in reversed(snaps) if "referrers" in p.name), Path("-")))
        self.paths = read_csv(next((p for p in reversed(snaps) if "paths" in p.name), Path("-")))
        # downloads: date -> (tag, asset) -> count, and each tag's prerelease flag
        self.dl: dict[str, dict[tuple[str, str], int]] = defaultdict(dict)
        self.pre: dict[str, bool] = {}
        for r in read_csv(root / "downloads" / "downloads.csv"):
            self.dl[r["date"]][(r["tag"], r["asset"])] = int(r["downloads"])
            self.pre[r["tag"]] = r["prerelease"] == "true"
        self.dl_days = sorted(self.dl)

    def end(self) -> str:
        known = list(self.traffic) + self.dl_days + [d for d, _ in self.stars]
        return max(known) if known else dt.datetime.now(dt.UTC).date().isoformat()

    def dl_total(self, day: str) -> int | None:
        snap = self.dl.get(day)
        return None if snap is None else sum(n for (_, a), n in snap.items() if not NOT_COUNTED.search(a))

    def dl_per_day(self, days: list[str]) -> list[int | None]:
        """Downloads during each day: the change from the previous snapshot (None without one)."""
        out: list[int | None] = []
        for d in days:
            i = self.dl_days.index(d) if d in self.dl else -1
            if i <= 0:
                out.append(None)
                continue
            prev = self.dl_days[i - 1]
            out.append((self.dl_total(d) or 0) - (self.dl_total(prev) or 0))
        return out

    def dl_since(self, day_from: str) -> dict[tuple[str, str], int]:
        """Each file's downloads since the latest snapshot on or before ``day_from``."""
        if not self.dl_days:
            return {}
        latest = self.dl[self.dl_days[-1]]
        base_day = max((d for d in self.dl_days if d <= day_from), default=self.dl_days[0])
        base = self.dl[base_day]
        return {k: n - base.get(k, 0) for k, n in latest.items()}


# ------------------------------------------------------------------ charts


def nice_max(v: float) -> int:
    """The top of the y axis: four equal steps of 1, 2 or 5 × 10ⁿ that reach ``v``."""
    if v <= 4:
        return 4
    raw = v / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    return int(step * 4)


def svg_chart(title: str, days: list[str], series: list[tuple[str, str, list[int | None]]], bars: bool = False) -> str:
    """A small chart that reads in GitHub's light and dark themes (transparent background, grey text)."""
    w, h, left, right, top, bottom = 760, 220, 44, 12, 34, 28
    pw, ph = w - left - right, h - top - bottom
    values = [v for _, _, s in series for v in s if v is not None]
    ymax = nice_max(max(values, default=0))
    n = len(days)
    x = lambda i: left + (pw * (i + 0.5) / n if bars else pw * i / max(1, n - 1))  # noqa: E731
    y = lambda v: top + ph - ph * v / ymax  # noqa: E731
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="system-ui,-apple-system,Segoe UI,sans-serif" font-size="11">',
        f'<text x="{left}" y="16" font-size="13" font-weight="600" fill="#8b949e">{html.escape(title)}</text>',
    ]
    lx = w - right
    for label, color, _ in reversed(series):
        tw = 7 * len(label) + 18
        lx -= tw
        out.append(f'<rect x="{lx}" y="8" width="10" height="10" rx="2" fill="{color}"/>')
        out.append(f'<text x="{lx + 14}" y="17" fill="#8b949e">{html.escape(label)}</text>')
    for k in range(5):
        v = ymax * k / 4
        yy = y(v)
        out.append(f'<line x1="{left}" x2="{w - right}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#8b949e" stroke-opacity="{0.45 if k == 0 else 0.18}"/>')
        out.append(f'<text x="{left - 6}" y="{yy + 4:.1f}" text-anchor="end" fill="#8b949e">{int(v) if v == int(v) else v}</text>')
    for i in sorted({0, n // 2, n - 1}):
        anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"  # the outer labels stay inside
        out.append(f'<text x="{x(i):.1f}" y="{h - 8}" text-anchor="{anchor}" fill="#8b949e">{days[i][5:]}</text>')
    bw = max(1.0, pw / n * 0.7 / len(series))
    for si, (_, color, s) in enumerate(series):
        if bars:
            for i, v in enumerate(s):
                if v:
                    bx = x(i) - bw * len(series) / 2 + bw * si
                    out.append(f'<rect x="{bx:.1f}" y="{y(v):.1f}" width="{bw:.1f}" height="{y(0) - y(v):.1f}" fill="{color}"/>')
            continue
        pts, path = [], []
        for i, v in enumerate(s):
            if v is None:
                if pts:
                    path.append(pts)
                pts = []
                continue
            pts.append(f"{x(i):.1f},{y(v):.1f}")
        if pts:
            path.append(pts)
        for p in path:
            if len(p) == 1:
                cx, cy = p[0].split(",")
                out.append(f'<circle cx="{cx}" cy="{cy}" r="2.5" fill="{color}"/>')
            else:
                out.append(f'<polyline points="{" ".join(p)}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>')
    if not values:
        out.append(f'<text x="{left + pw / 2}" y="{top + ph / 2}" text-anchor="middle" fill="#8b949e">no data yet</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def charts(d: Data, end: str) -> dict[str, str]:
    days = days_back(end, CHART_DAYS)
    t = [d.traffic.get(x) for x in days]
    return {
        "traffic.svg": svg_chart(
            f"Views per day (last {CHART_DAYS} days)",
            days,
            [("Views", COLORS["a"], [v[0] if v else None for v in t]), ("Unique visitors", COLORS["b"], [v[1] if v else None for v in t])],
        ),
        "downloads.svg": svg_chart(f"Downloads per day (last {CHART_DAYS} days)", days, [("Downloads", COLORS["a"], d.dl_per_day(days))], bars=True),
        "stars.svg": svg_chart(
            f"Stars and forks (last {CHART_DAYS} days)",
            days,
            [("Stars", COLORS["a"], cumulative_on(d.stars, days)), ("Forks", COLORS["c"], cumulative_on(d.forks, days))],
        ),
    }


# ------------------------------------------------------------------ report


def fmt(v: int | None) -> str:
    return "—" if v is None else f"{v:,}"


def md_table(head: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    if not rows:
        return "_No data yet._\n"
    align = ["---:" if i in right else "---" for i in range(len(head))]
    lines = ["| " + " | ".join(head) + " |", "| " + " | ".join(align) + " |"]
    lines += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def tag_order(d: Data) -> list[str]:
    tags = {t for snap in d.dl.values() for t, _ in snap}

    def key(t: str) -> tuple:
        return tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\-]", t.lstrip("v")))

    return sorted(tags, key=key, reverse=True)


def sections(d: Data, end: str) -> dict[str, tuple[list[str], list[list[str]], set[int]]]:
    days = days_back(end, TABLE_DAYS)
    stars = cumulative_on(d.stars, days)
    per_day = d.dl_per_day(days)
    daily = []
    for i, day in reversed(list(enumerate(days))):
        t = d.traffic.get(day)
        daily.append([day, fmt(t[0] if t else None), fmt(t[1] if t else None), fmt(t[2] if t else None), fmt(t[3] if t else None), fmt(stars[i]), fmt(per_day[i])])
    week = d.dl_since((dt.date.fromisoformat(end) - dt.timedelta(days=7)).isoformat())
    latest = d.dl[d.dl_days[-1]] if d.dl_days else {}
    releases, files = [], []
    for tag in tag_order(d):
        mine = {a: n for (t, a), n in latest.items() if t == tag}
        counted = {a: n for a, n in mine.items() if not NOT_COUNTED.search(a)}
        name = tag + (" (pre-release)" if d.pre.get(tag) else "")
        releases.append([name, fmt(sum(counted.values())), fmt(sum(week.get((tag, a), 0) for a in counted))])
        for a, n in sorted(mine.items(), key=lambda kv: (bool(NOT_COUNTED.search(kv[0])), -kv[1], kv[0])):
            files.append([tag, f"`{a}`" + (" ¹" if NOT_COUNTED.search(a) else ""), fmt(n), fmt(week.get((tag, a)))])
    refs = [[r["referrer"], fmt(int(r["views_total"])), fmt(int(r["views_unique"]))] for r in d.referrers[:15]]
    paths = [[f"`{r['url_path']}`", fmt(int(r["views_total"])), fmt(int(r["views_unique"]))] for r in d.paths[:15]]
    return {
        "daily": (["Day (UTC)", "Views", "Visitors", "Clones", "Cloners", "Stars", "Downloads"], daily, {1, 2, 3, 4, 5, 6}),
        "releases": (["Release", "Downloads", "Last 7 days"], releases, {1, 2}),
        "files": (["Release", "File", "Downloads", "Last 7 days"], files, {2, 3}),
        "referrers": (["Referrer", "Views", "Visitors"], refs, {1, 2}),
        "paths": (["Page", "Views", "Visitors"], paths, {1, 2}),
    }


def summary(d: Data, end: str) -> list[tuple[str, str]]:
    last14 = [d.traffic.get(x) for x in days_back(end, 14)]
    return [
        ("Stars", fmt(d.stars[-1][1] if d.stars else None)),
        ("Forks", fmt(d.forks[-1][1] if d.forks else None)),
        ("Downloads", fmt(d.dl_total(d.dl_days[-1]) if d.dl_days else None)),
        ("Views, 14 days", fmt(sum(t[0] for t in last14 if t))),
        ("Clones, 14 days", fmt(sum(t[2] for t in last14 if t))),
    ]


NOTES = [
    "Views, visitors, clones, referrers and pages come from GitHub's traffic API, which keeps 14 days; this repo keeps them for good. Days are UTC.",
    "Clones include GitHub Actions checkouts (SpaceKnife's own CI), so they don't count people.",
    "Downloads count the installers and archives. ¹ Checksum and `.zsync` files are fetched by tools: listed, not counted.",
    'Downloads per day are the change between two daily snapshots, so they start the day after the first one; until there is a week of snapshots, "last 7 days" counts from the first.',
]


def write_readme(root: Path, repo: str, d: Data, end: str, pages_url: str | None) -> None:
    s = sections(d, end)
    lines = [
        f"# {repo.split('/')[-1]} stats",
        "",
        f"Traffic, stars, forks and downloads of [{repo}](https://github.com/{repo}), collected every day by "
        "[`.github/workflows/stats.yml`](.github/workflows/stats.yml) with "
        "[github-repo-stats](https://github.com/jgehrcke/github-repo-stats) and [`scripts/report.py`](scripts/report.py). "
        f"Updated {end} (UTC).",
        "",
    ]
    if pages_url:
        lines += [f"**Dashboard:** {pages_url}/ · **Detailed traffic report:** {pages_url}/report/report.html", ""]
    lines += ["| " + " | ".join(k for k, _ in summary(d, end)) + " |", "|" + "---:|" * 5, "| " + " | ".join(v for _, v in summary(d, end)) + " |", ""]
    lines += ["![Views per day](charts/traffic.svg)", "", "![Downloads per day](charts/downloads.svg)", "", "![Stars and forks](charts/stars.svg)", ""]
    titles = {
        "daily": f"## Last {TABLE_DAYS} days",
        "releases": "## Downloads by release",
        "files": "## Downloads by file",
        "referrers": "## Referrers (latest 14 days)",
        "paths": "## Popular pages (latest 14 days)",
    }
    for key, title in titles.items():
        head, rows, right = s[key]
        lines += [title, "", md_table(head, rows, right)]
    lines += ["## Notes", ""] + [f"- {n}" for n in NOTES] + [""]
    lines += [f"Raw data: [`{repo}/ghrs-data/`]({repo}/ghrs-data/), [`{repo}/downloads/downloads.csv`]({repo}/downloads/downloads.csv).", ""]
    (root / "README.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def html_table(head: list[str], rows: list[list[str]], right: set[int]) -> str:
    if not rows:
        return "<p class=muted>No data yet.</p>"
    code = re.compile(r"`([^`]+)`")
    cell = lambda c: code.sub(r"<code>\1</code>", html.escape(c))  # noqa: E731
    th = "".join(f"<th{' class=r' if i in right else ''}>{html.escape(h)}</th>" for i, h in enumerate(head))
    tr = "".join("<tr>" + "".join(f"<td{' class=r' if i in right else ''}>{cell(c)}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>"


def write_site(out: Path, root: Path, repo: str, d: Data, end: str, svgs: dict[str, str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():  # empty it, but keep the folder itself
        shutil.rmtree(old) if old.is_dir() else old.unlink()
    report = root / "latest-report"
    if report.exists():
        shutil.copytree(report, out / "report")
    s = sections(d, end)
    cards = "".join(f"<div class=card><div class=v>{v}</div><div class=k>{html.escape(k)}</div></div>" for k, v in summary(d, end))
    blocks = [
        ("Last 30 days", "daily"),
        ("Downloads by release", "releases"),
        ("Downloads by file", "files"),
        ("Referrers (latest 14 days)", "referrers"),
        ("Popular pages (latest 14 days)", "paths"),
    ]
    body = "".join(f"<h2>{t}</h2>{html_table(*s[k])}" for t, k in blocks)
    figs = "".join(f"<figure>{svg.split(chr(10), 1)[1] if svg.startswith('<?xml') else svg}</figure>" for svg in svgs.values())
    notes = "".join(f"<li>{html.escape(n).replace('`', '')}</li>" for n in NOTES)
    name = repo.split("/")[-1]
    report_link = '<a href="report/report.html">Detailed traffic report</a> · ' if report.exists() else ""
    here = os.environ.get("GITHUB_REPOSITORY")  # this repo, when run in Actions
    code_link = f'<a href="https://github.com/{here}">Data and code</a>' if here else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} stats</title>
<style>
:root{{color-scheme:dark;--bg:#08090b;--fg:#e2e4ea;--muted:#8f95a2;--line:#1d2027;--card:#0e1013;--accent:#8b9cff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:880px;margin:0 auto;padding:32px 16px 64px}}h1{{font-size:26px;margin:0 0 4px}}h2{{font-size:17px;margin:36px 0 10px}}
a{{color:var(--accent)}}.muted,.sub{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:20px 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}.v{{font-size:22px;font-weight:650}}.k{{color:var(--muted);font-size:12px}}
figure{{margin:14px 0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;overflow-x:auto}}figure svg{{max-width:100%;height:auto}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{color:var(--muted);font-weight:500}}.r{{text-align:right}}
code{{font:12px ui-monospace,Consolas,monospace;background:var(--card);padding:1px 4px;border-radius:4px}}.wrap{{overflow-x:auto}}
</style></head><body><main>
<h1>{name} stats</h1>
<div class=sub>Traffic, stars, forks and downloads of <a href="https://github.com/{repo}">{repo}</a>, updated {end} (UTC). {report_link}{code_link}</div>
<div class=cards>{cards}</div>
{figs}
<div class=wrap>{body}</div>
<h2>Notes</h2><ul class=muted>{notes}</ul>
</main></body></html>
"""
    (out / "index.html").write_text(page, encoding="utf-8", newline="\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="owner/name of the repository the stats are about")
    ap.add_argument("--root", type=Path, default=Path("."), help="this repo's checkout")
    ap.add_argument("--site", type=Path, help="also write the Pages site here")
    ap.add_argument("--pages-url", help="where the site is published (for the README's links)")
    ap.add_argument("--no-fetch", action="store_true", help="don't take today's download snapshot")
    args = ap.parse_args()
    root = args.root.resolve()
    data = root / args.repo
    if not args.no_fetch:
        today = dt.datetime.now(dt.UTC).date().isoformat()
        token = os.environ.get("GITHUB_TOKEN") or None
        snapshot_downloads(data / "downloads" / "downloads.csv", fetch_releases(args.repo, token), today)
    d = Data(data)
    end = d.end()
    svgs = charts(d, end)
    (root / "charts").mkdir(exist_ok=True)
    for name, svg in svgs.items():
        (root / "charts" / name).write_text(svg, encoding="utf-8", newline="\n")
    write_readme(root, args.repo, d, end, args.pages_url.rstrip("/") if args.pages_url else None)
    if args.site:
        write_site(args.site.resolve(), data, args.repo, d, end, svgs)
    print(f"report for {end}: {len(d.traffic)} traffic days, {len(d.dl_days)} download snapshots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
