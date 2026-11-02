#!/usr/bin/env python3
"""Draw the profile dashboard as an SVG that looks like GitHub's own UI.

Counts commits on the default branch of every repository the token can
see, so private work counts without any repository name leaving the API.
Colours are GitHub's Primer palette (light and dark) and its language
colours; text uses the same system font stack as github.com.
If the API fails, nothing is written.
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "painel.svg"
API = "https://api.github.com/graphql"

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - tzdata missing
    TZ = timezone(timedelta(hours=-3))

MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
PRETTY = {"PLpgSQL": "PL/pgSQL", "Jupyter Notebook": "Jupyter"}
IGNORED = {"Dockerfile", "Makefile", "Procfile", "Batchfile"}

# Career, in Evandro's own words. Years are decimal (month / 12); None = today.
CAREER = [
    (2007.00, 2012.42, "Endosul", "analista de dados sênior"),
    (2013.50, 2015.25, "Kipel", "vendedor interno"),
    (2016.08, 2017.83, "McDonald's", "gerente, Arcos Dourados"),
    (2019.83, 2020.92, "Hora do Pastel", "gerente de loja"),
    (2021.00, None, "Hora do Pastel", "analista de dados e programador"),
    (2021.00, None, "BGPF Group", "analista administrativo"),
]
DEGREES = [
    (2011.42, 2016.58, "FSG", "bacharel em Administração"),
    (2016.00, 2022.58, "Feevale", "tecnólogo em Processos Gerenciais"),
]

W = 720
PAD = 16


class FetchError(Exception):
    pass


# -- data ---------------------------------------------------------------------

def gql(query: str, variables: dict, token: str) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = Request(API, data=body, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "esegnorelli-profile",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=60) as response:
            data = json.load(response)
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise FetchError(f"GraphQL — {exc}") from exc
    if data.get("errors"):
        raise FetchError(f"GraphQL — {data['errors'][0].get('message')}")
    return data["data"]


REPO_FIELDS = ("pageInfo{hasNextPage endCursor} nodes{id isPrivate pushedAt "
               "languages(first:10,orderBy:{field:SIZE,direction:DESC}){edges{size node{name color}}}}")
OWN_REPOS = ("query($after:String){viewer{repositories(first:100,after:$after,isFork:false,"
             "affiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER],"
             "ownerAffiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER],"
             "orderBy:{field:PUSHED_AT,direction:DESC}){" + REPO_FIELDS + "}}}")
PUBLIC_REPOS = ("query($login:String!,$after:String){user(login:$login){repositories(first:100,"
                "after:$after,isFork:false,privacy:PUBLIC,ownerAffiliations:[OWNER],"
                "orderBy:{field:PUSHED_AT,direction:DESC}){" + REPO_FIELDS + "}}}")
SAFE_ID = re.compile(r"^[A-Za-z0-9_=-]+$")


def history_block(alias: str, repo_id: str, cursor) -> str:
    if not SAFE_ID.match(repo_id):
        raise FetchError("unexpected repository id")
    after = f",after:{json.dumps(cursor)}" if cursor else ""
    return (f'{alias}:node(id:"{repo_id}"){{...on Repository{{defaultBranchRef{{target{{...on Commit{{'
            f"history(first:100{after},author:{{id:$uid}},since:$since)"
            "{pageInfo{hasNextPage endCursor} nodes{authoredDate}}}}}}}")


def fetch(username: str, token: str) -> dict:
    now = datetime.now(TZ)
    since = (now - timedelta(days=365)).replace(microsecond=0)
    viewer = gql("query{viewer{login}}", {}, token)["viewer"]["login"]
    user = gql("query($l:String!){user(login:$l){id}}", {"l": username}, token)["user"]
    if not user:
        raise FetchError(f"user {username} not found")
    private_ok = viewer.lower() == username.lower()

    repos, cursor = [], None
    while True:
        if private_ok:
            page = gql(OWN_REPOS, {"after": cursor}, token)["viewer"]["repositories"]
        else:
            page = gql(PUBLIC_REPOS, {"login": username, "after": cursor}, token)["user"]["repositories"]
        fresh = [r for r in page["nodes"] if r["pushedAt"] and r["pushedAt"] >= since.isoformat()]
        repos.extend(fresh)
        if len(fresh) < len(page["nodes"]) or not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    stamps = {r["id"]: [] for r in repos}
    pending = [(r["id"], None) for r in repos]
    while pending:
        batch, pending = pending[:20], pending[20:]
        blocks = "".join(history_block(f"r{i}", rid, cur) for i, (rid, cur) in enumerate(batch))
        query = "query($uid:ID!,$since:GitTimestamp!){" + blocks + "}"
        data = gql(query, {"uid": user["id"], "since": since.isoformat()}, token)
        for i, (rid, _) in enumerate(batch):
            node = data.get(f"r{i}") or {}
            target = ((node.get("defaultBranchRef") or {}).get("target")) or {}
            history = target.get("history")
            if not history:
                continue
            stamps[rid].extend(n["authoredDate"] for n in history["nodes"])
            if history["pageInfo"]["hasNextPage"]:
                pending.append((rid, history["pageInfo"]["endCursor"]))

    commits, languages, colors, active_repos, private_repos = [], Counter(), {}, 0, 0
    for repo in repos:
        times = [datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ) for s in stamps[repo["id"]]]
        if not times:
            continue
        active_repos += 1
        private_repos += repo["isPrivate"]
        commits.extend(times)
        edges = [e for e in repo["languages"]["edges"] if e["node"]["name"] not in IGNORED]
        size = sum(e["size"] for e in edges)
        if not size:
            languages[None] += len(times)
        for e in edges:
            name = PRETTY.get(e["node"]["name"], e["node"]["name"])
            languages[name] += len(times) * e["size"] / size
            colors[name] = e["node"]["color"] or "#8b949e"
    return model(now, commits, languages, colors, active_repos, private_repos, private_ok)


def model(now, commits, languages, colors, active_repos, private_repos, private_ok) -> dict:
    today = now.date()
    first = today - timedelta(days=364)
    per_day = Counter(t.date() for t in commits if t.date() >= first)
    days = [first + timedelta(days=i) for i in range(365)]

    weeks = []
    for i in range(52):
        start = today - timedelta(days=7 * (52 - i) - 1)
        weeks.append((start, sum(per_day[start + timedelta(days=k)] for k in range(7))))

    longest, run, best_end = 0, 0, None
    for day in days:
        run = run + 1 if per_day[day] else 0
        if run > longest:
            longest, best_end = run, day
    anchor = today if per_day[today] else today - timedelta(days=1)
    current = 0
    while per_day[anchor - timedelta(days=current)]:
        current += 1

    grid = [[0] * 24 for _ in range(7)]
    for t in commits:
        grid[t.weekday()][t.hour] += 1
    total = len(commits)
    office = sum(grid[d][h] for d in range(5) for h in range(8, 18))

    named = [(k, v) for k, v in languages.most_common() if k]
    other = sum(v for k, v in languages.items() if not k) + sum(v for _, v in named[5:])
    langs = named[:5] + ([("Outras", other)] if other >= 0.5 else [])

    return {
        "now": now, "total": total, "weeks": weeks, "active_days": sum(1 for d in days if per_day[d]),
        "longest": longest, "longest_start": best_end - timedelta(days=longest - 1) if best_end else None,
        "longest_end": best_end, "current": current,
        "current_start": anchor - timedelta(days=current - 1) if current else None,
        "grid": grid, "off_hours": 1 - office / total if total else 0.0,
        "langs": langs, "lang_total": sum(languages.values()), "colors": colors,
        "repos": active_repos, "private": private_repos, "private_ok": private_ok,
    }


# -- drawing ------------------------------------------------------------------

def num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def day_label(d: date, year: bool = False) -> str:
    text = f"{d.day} {MONTHS[d.month - 1]}"
    return f"{text} de {d.year}" if year else text


# GitHub Primer: dark default, and light default under prefers-color-scheme.
STYLE = """
:root{--bg:#0d1117;--border:#3d444d;--fg:#f0f6fc;--muted:#9198a1;--past:#656c76;--grid:#262c36;
--c0:#151b23;--c1:#033a16;--c2:#196c2e;--c3:#2ea043;--c4:#56d364;--cell:rgba(240,246,252,.04)}
@media (prefers-color-scheme:light){:root{--bg:#ffffff;--border:#d1d9e0;--fg:#1f2328;--muted:#59636e;
--past:#818b98;--grid:#eff2f5;--c0:#eff2f5;--c1:#aceebb;--c2:#4ac26b;--c3:#2da44e;--c4:#116329;--cell:rgba(31,35,40,.06)}}
text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif;
font-size:14px;fill:var(--fg)}
.m{fill:var(--muted)}.s{font-size:12px}.t{font-weight:600}.h{font-size:16px;font-weight:600}
.n{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.box{fill:var(--bg);stroke:var(--border)}.rule{stroke:var(--border)}.grid{stroke:var(--grid)}
.past{fill:var(--past)}.edu{fill:none;stroke:var(--muted);stroke-width:1.2}
.c0{fill:var(--c0)}.c1{fill:var(--c1)}.c2{fill:var(--c2)}.c3{fill:var(--c3)}.c4{fill:var(--c4)}
.cell{stroke:var(--cell)}.stop3{stop-color:var(--c3)}
"""


def t(x, y, content, cls="", anchor="start", raw=False):
    klass = f' class="{cls}"' if cls else ""
    end = ' text-anchor="end"' if anchor == "end" else (' text-anchor="middle"' if anchor == "middle" else "")
    body = content if raw else escape(content)
    return f'<text x="{x:.1f}" y="{y:.1f}"{klass}{end}>{body}</text>'


def levels(values):
    """GitHub-style quartile levels for non-zero values."""
    nz = sorted(v for v in values if v)
    if not nz:
        return lambda v: 0
    q = [nz[min(len(nz) - 1, int(len(nz) * f))] for f in (0.25, 0.5, 0.75)]
    return lambda v: 0 if not v else 1 + sum(v > x for x in q)


def painel(m: dict) -> str:
    out, defs = [], []
    y = 0
    now = m["now"]

    # header
    out.append(t(PAD, 30, "Atividade nos últimos 12 meses", "h"))
    out.append(t(W - PAD, 30, f"Atualizado em {day_label(now.date(), True)}", "s m", "end"))
    y = 46
    out.append(f'<line x1="0" y1="{y}.5" x2="{W}" y2="{y}.5" class="rule"/>')

    # numbers
    ls, le = m["longest_start"], m["longest_end"]
    stats = [
        (num(m["total"]), "", "Commits", f"em {m['repos']} repositórios"),
        (str(m["active_days"]), " de 365", "Dias com commits", f"{round(100 * m['active_days'] / 365)}% do ano"),
        (str(m["current"]), " dias" if m["current"] != 1 else " dia", "Sequência atual",
         f"desde {day_label(m['current_start'])}" if m["current"] else "sem commits hoje ou ontem"),
        (str(m["longest"]), " dias" if m["longest"] != 1 else " dia", "Maior sequência",
         f"{day_label(ls)} a {day_label(le)}" if ls else "nenhuma no período"),
    ]
    col = W / 4
    for i, (value, unit, label, detail) in enumerate(stats):
        x = i * col + PAD
        if i:
            out.append(f'<line x1="{i * col:.1f}" y1="{y}" x2="{i * col:.1f}" y2="{y + 92}" class="rule"/>')
        unit_span = f'<tspan class="m" style="font-size:14px;font-weight:400">{escape(unit)}</tspan>' if unit else ""
        out.append(t(x, y + 38, f"{escape(value)}{unit_span}", "n", raw=True))
        out.append(t(x, y + 60, label, "s"))
        out.append(t(x, y + 78, detail, "s m"))
    y += 92
    out.append(f'<line x1="0" y1="{y}.5" x2="{W}" y2="{y}.5" class="rule"/>')

    # commits per week
    weeks = m["weeks"]
    counts = [n for _, n in weeks]
    peak = max(counts) if counts else 0
    out.append(t(PAD, y + 28, "Commits por semana", "t"))
    if peak:
        pw = weeks[counts.index(peak)][0]
        out.append(t(W - PAD, y + 28, f"Pico: semana de {day_label(pw)}, {num(peak)} commits", "s m", "end"))
    base, height = y + 108, 62
    slot = (W - 2 * PAD) / len(weeks)
    last_label = -99
    for i, (start, n) in enumerate(weeks):
        x = PAD + i * slot
        h = max(height * n / peak, 2) if peak and n else 2
        cls = "c4" if n == peak and peak else ("c3" if n else "c0")
        out.append(f'<rect x="{x + 1:.1f}" y="{base - h:.1f}" width="{slot - 3:.1f}" height="{h:.1f}" rx="2" class="{cls}"/>')
        if start.day <= 7 and x - last_label > 30:
            out.append(t(x + 1, base + 18, MONTHS[start.month - 1].capitalize(), "s m"))
            last_label = x
    y = base + 32
    out.append(f'<line x1="0" y1="{y}.5" x2="{W}" y2="{y}.5" class="rule"/>')

    # when: weekday x hour, like the contribution graph
    split = 412
    top = y
    grid = m["grid"]
    flat = [v for row in grid for v in row]
    level = levels(flat)
    off = round(100 * m["off_hours"])
    out.append(t(PAD, y + 28, "Horário dos commits", "t"))
    out.append(t(PAD, y + 47, f"{off}% fora do horário comercial (seg a sex, 8h às 18h)" if m["total"]
                 else "Sem commits no período", "s m"))
    gx, gy, step, cell = PAD + 36, y + 62, 13, 10
    for d in range(7):
        if d in (0, 2, 4):
            out.append(t(PAD, gy + d * step + 9, WEEKDAYS[d], "s m"))
        for h in range(24):
            out.append(f'<rect x="{gx + h * step}" y="{gy + d * step}" width="{cell}" height="{cell}" rx="2" '
                       f'class="c{level(grid[d][h])} cell"/>')
    for h in (0, 6, 12, 18):
        out.append(t(gx + h * step, gy + 7 * step + 14, f"{h}h", "s m"))
    ly = gy + 7 * step + 36
    out.append(t(gx + 12 * step - 6, ly, "Menos", "s m", "end"))
    for k in range(5):
        out.append(f'<rect x="{gx + 12 * step + k * step}" y="{ly - 9}" width="{cell}" height="{cell}" rx="2" class="c{k} cell"/>')
    out.append(t(gx + 17 * step + 3, ly, "Mais", "s m"))
    out.append(t(PAD, ly, "Horário de Brasília", "s m"))

    # languages, like a repository's language bar
    lx, lw = split + PAD, W - split - 2 * PAD
    out.append(t(lx, y + 28, "Linguagens", "t"))
    out.append(t(lx, y + 47, "Proporção nos commits do ano", "s m"))
    langs, total = m["langs"], m["lang_total"] or 1
    bar_y = y + 62
    defs.append(f'<clipPath id="langbar"><rect x="{lx}" y="{bar_y}" width="{lw}" height="8" rx="4"/></clipPath>')
    cursor = lx
    segments = []
    for name, n in langs:
        width = lw * n / total
        color = m["colors"].get(name, "#8b949e")
        segments.append(f'<rect x="{cursor:.2f}" y="{bar_y}" width="{max(width - 2, 0.5):.2f}" height="8" fill="{color}"/>')
        cursor += width
    out.append(f'<g clip-path="url(#langbar)"><rect x="{lx}" y="{bar_y}" width="{lw}" height="8" class="c0"/>{"".join(segments)}</g>')
    for i, (name, n) in enumerate(langs):
        ry = bar_y + 34 + i * 22
        pct = 100 * n / total
        color = m["colors"].get(name, "#8b949e")
        out.append(f'<circle cx="{lx + 4}" cy="{ry - 4.5}" r="4" fill="{color}"/>')
        out.append(t(lx + 14, ry, name, "t" if i == 0 else ""))
        out.append(t(W - PAD, ry, f"{pct:.1f}%".replace(".", ","), "s m", "end"))
    if not langs:
        out.append(t(lx, bar_y + 34, "Sem dados no período", "s m"))
    y = max(gy + 7 * step + 52, bar_y + 34 + (len(langs) - 1) * 22 + 20)
    out.append(f'<line x1="{split}.5" y1="{top}" x2="{split}.5" y2="{y}" class="rule"/>')
    out.append(f'<line x1="0" y1="{y}.5" x2="{W}" y2="{y}.5" class="rule"/>')

    # career, as a Gantt chart
    out.append(t(PAD, y + 28, "Trajetória profissional", "t"))
    a0, a1 = 366, W - PAD
    end = now.year + (now.timetuple().tm_yday - 1) / 365

    def tx(year):
        return a0 + (year - 2007) * (a1 - a0) / (end - 2007)

    first_row, step_row = y + 58, 24
    edu_top = first_row + step_row * len(CAREER) + 14
    bottom = edu_top + step_row * (len(DEGREES) - 1) + 8
    for year in (2007, 2012, 2017, 2022):
        out.append(f'<line x1="{tx(year):.1f}" y1="{first_row - 16}" x2="{tx(year):.1f}" y2="{bottom}" class="grid"/>')
        out.append(t(tx(year), bottom + 18, str(year), "s m", "middle" if year > 2007 else "start"))
    out.append(t(a1, bottom + 18, "hoje", "s m", "end"))
    out.append(f'<line x1="{PAD}" y1="{edu_top - 19}.5" x2="{W - PAD}" y2="{edu_top - 19}.5" class="grid"/>')
    defs.append(f'<linearGradient id="dawn" gradientUnits="userSpaceOnUse" x1="{tx(2021.0):.1f}" x2="{tx(2022.0):.1f}" y1="0" y2="0">'
                '<stop offset="0" class="stop3" stop-opacity="0"/><stop offset="1" class="stop3"/></linearGradient>')

    def row(base, company, role, x0, x1, kind):
        out.append(t(PAD, base, f'{escape(company)}<tspan class="s m" dx="6">{escape(role)}</tspan>', raw=True))
        width = max(x1 - x0, 3)
        if kind == "now":
            out.append(f'<rect x="{x0:.1f}" y="{base - 10}" width="{width:.1f}" height="8" rx="4" fill="url(#dawn)"/>')
        elif kind == "edu":
            out.append(f'<rect x="{x0:.1f}" y="{base - 9.4}" width="{width:.1f}" height="6.8" rx="3.4" class="edu"/>')
        else:
            out.append(f'<rect x="{x0:.1f}" y="{base - 10}" width="{width:.1f}" height="8" rx="4" class="past"/>')

    for i, (start, stop, company, role) in enumerate(CAREER):
        row(first_row + i * step_row, company, role, tx(start), tx(stop if stop else end), "past" if stop else "now")
    for k, (start, stop, school, degree) in enumerate(DEGREES):
        row(edu_top + k * step_row, school, degree, tx(start), tx(stop), "edu")
    y = bottom + 34
    out.append(f'<line x1="0" y1="{y}.5" x2="{W}" y2="{y}.5" class="rule"/>')

    scope = (f"{m['repos']} repositórios, {m['private']} deles privados" if m["private_ok"]
             else f"{m['repos']} repositórios públicos")
    out.append(t(PAD, y + 24, f"Commits no branch principal de {scope}. Atualizado a cada 6 horas.", "s m"))
    H = y + 40

    desc = (f"{num(m['total'])} commits nos últimos 12 meses, {m['active_days']} dias com commits, "
            f"sequência atual de {m['current']} dias e maior sequência de {m['longest']} dias. "
            "Trajetória: Endosul, Kipel, McDonald's, Hora do Pastel e BGPF Group.")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'role="img" aria-labelledby="t d"><title id="t">Painel de atividade de Evandro Segnorelli</title>'
            f'<desc id="d">{escape(desc)}</desc><style>{STYLE}</style><defs>{"".join(defs)}</defs>'
            f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="6" class="box"/>'
            f'{"".join(out)}</svg>\n')


# -- main -----------------------------------------------------------------------

def main() -> int:
    username = os.getenv("GH_USERNAME") or os.getenv("GITHUB_REPOSITORY_OWNER") or "Esegnorelli"
    token = os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN") or ""
    if not token:
        print("Error: set GH_PAT or GITHUB_TOKEN (GraphQL needs a token).", file=sys.stderr)
        return 1
    try:
        data = fetch(username, token)
    except FetchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    content = painel(data)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, OUT)
    print(f"Wrote {OUT} ({len(content) // 1024} KB)")
    print(f"{data['total']} commits · {data['active_days']} active days · streak {data['current']}/{data['longest']} · "
          f"{data['repos']} repos ({data['private']} private)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
