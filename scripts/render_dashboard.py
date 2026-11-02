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
# Degrees: five years each, ending at graduation. UFBRA is in progress (None = today);
# its semester is counted from the start month so the label stays current.
DEGREES = [
    (2011.58, 2016.58, "FSG", "bacharel em Administração"),
    (2017.58, 2022.58, "Feevale", "tecnólogo em Processos Gerenciais"),
    (2026.08, None, "UFBRA", "Sistemas de Informação"),
]


def semester(start: float, today: date) -> int:
    months = (today.year - int(start)) * 12 + today.month - 1 - round((start % 1) * 12)
    return max(1, min(8, months // 6 + 1))

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
SAFE_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
SEARCH = ("query($q:String!,$after:String){search(query:$q,type:ISSUE,first:100,after:$after){issueCount "
          "pageInfo{hasNextPage endCursor} nodes{...on PullRequest{createdAt repository{id isPrivate}} "
          "...on Issue{createdAt repository{id isPrivate}}}}}")
CREATED_OWN = ("query($after:String){viewer{repositories(first:100,after:$after,isFork:false,ownerAffiliations:[OWNER],"
               "orderBy:{field:CREATED_AT,direction:DESC}){pageInfo{hasNextPage endCursor} nodes{createdAt}}}}")
CREATED_PUBLIC = ("query($login:String!,$after:String){user(login:$login){repositories(first:100,after:$after,"
                  "isFork:false,privacy:PUBLIC,ownerAffiliations:[OWNER],orderBy:{field:CREATED_AT,direction:DESC})"
                  "{pageInfo{hasNextPage endCursor} nodes{createdAt}}}}")
# Names come only from a PUBLIC-privacy query, so no private name can reach the image.
PUBLIC_ORGS = ("query($login:String!){user(login:$login){repositoriesContributedTo(first:100,privacy:PUBLIC,"
               "includeUserRepositories:true,contributionTypes:[COMMIT,PULL_REQUEST,ISSUE,PULL_REQUEST_REVIEW])"
               "{nodes{owner{__typename login}}}}}")


def search_all(query: str, token: str):
    """Return (total count, [(createdAt, repo id, is private)]) for an issue/PR search."""
    found, cursor, total = [], None, 0
    while True:
        page = gql(SEARCH, {"q": query, "after": cursor}, token)["search"]
        total = page["issueCount"]
        for node in page["nodes"]:
            if node and node.get("createdAt"):
                repo = node.get("repository") or {}
                found.append((node["createdAt"], repo.get("id"), bool(repo.get("isPrivate"))))
        if not page["pageInfo"]["hasNextPage"] or len(found) >= 1000:
            return total, found
        cursor = page["pageInfo"]["endCursor"]


def created_since(since_iso: str, private_ok: bool, username: str, token: str) -> list:
    stamps, cursor = [], None
    while True:
        if private_ok:
            page = gql(CREATED_OWN, {"after": cursor}, token)["viewer"]["repositories"]
        else:
            page = gql(CREATED_PUBLIC, {"login": username, "after": cursor}, token)["user"]["repositories"]
        fresh = [r["createdAt"] for r in page["nodes"] if r["createdAt"] >= since_iso]
        stamps.extend(fresh)
        if len(fresh) < len(page["nodes"]) or not page["pageInfo"]["hasNextPage"]:
            return stamps
        cursor = page["pageInfo"]["endCursor"]


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

    if not SAFE_LOGIN.match(username):
        raise FetchError("unexpected username")
    day = since.date().isoformat()
    scope = "" if private_ok else " is:public"
    pr_total, prs = search_all(f"author:{username} is:pr created:>={day}{scope}", token)
    issue_total, issues = search_all(f"author:{username} is:issue created:>={day}{scope}", token)
    review_total, reviews = search_all(f"reviewed-by:{username} -author:{username} is:pr updated:>={day}{scope}", token)
    created = created_since(since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), private_ok, username, token)
    contributed = gql(PUBLIC_ORGS, {"login": username}, token)["user"]["repositoriesContributedTo"]["nodes"]
    orgs = sorted({n["owner"]["login"] for n in contributed if n["owner"]["__typename"] == "Organization"})

    commits, languages, colors, active_repos, private_repos = [], Counter(), {}, 0, 0
    touched = {}
    for repo in repos:
        times = [datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ) for s in stamps[repo["id"]]]
        if not times:
            continue
        active_repos += 1
        private_repos += repo["isPrivate"]
        touched[repo["id"]] = repo["isPrivate"]
        commits.extend(times)
        edges = [e for e in repo["languages"]["edges"] if e["node"]["name"] not in IGNORED]
        size = sum(e["size"] for e in edges)
        if not size:
            languages[None] += len(times)
        for e in edges:
            name = PRETTY.get(e["node"]["name"], e["node"]["name"])
            languages[name] += len(times) * e["size"] / size
            colors[name] = e["node"]["color"] or "#8b949e"
    for _, rid, private in prs + issues + reviews:
        if rid:
            touched.setdefault(rid, private)

    def local(stamp):
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(TZ)

    extra = [local(s) for s, _, _ in prs + issues + reviews] + [local(s) for s in created]
    activity = {"prs": pr_total, "issues": issue_total, "reviews": review_total, "created": len(created)}
    reach = {"repos": len(touched), "private": sum(touched.values()), "orgs": orgs}
    return model(now, commits, extra, activity, reach, languages, colors, active_repos, private_repos, private_ok)


def model(now, commits, extra, activity, reach, languages, colors, active_repos, private_repos, private_ok) -> dict:
    today = now.date()
    first = today - timedelta(days=364)
    per_day = Counter(t.date() for t in commits + extra if first <= t.date() <= today)
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
        "now": now, "first": first, "today": today, "per_day": per_day,
        "contrib": sum(per_day[d] for d in days), "activity": activity, "reach": reach,
        "total": total, "weeks": weeks, "active_days": sum(1 for d in days if per_day[d]),
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
--c0:#151b23;--c1:#033a16;--c2:#196c2e;--c3:#2ea043;--c4:#56d364;--cell:rgba(240,246,252,.04);--ok:#3fb950}
@media (prefers-color-scheme:light){:root{--bg:#ffffff;--border:#d1d9e0;--fg:#1f2328;--muted:#59636e;
--past:#818b98;--grid:#eff2f5;--c0:#eff2f5;--c1:#aceebb;--c2:#4ac26b;--c3:#2da44e;--c4:#116329;--cell:rgba(31,35,40,.06);--ok:#1a7f37}}
text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif;
font-size:14px;fill:var(--fg)}
.m{fill:var(--muted)}.s{font-size:12px}.t{font-weight:600}.h{font-size:16px;font-weight:600}
.n{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.box{fill:var(--bg);stroke:var(--border)}.rule{stroke:var(--border)}.grid{stroke:var(--grid)}
.past{fill:var(--past)}.edu{fill:none;stroke:var(--muted);stroke-width:1.2}
.c0{fill:var(--c0)}.c1{fill:var(--c1)}.c2{fill:var(--c2)}.c3{fill:var(--c3)}.c4{fill:var(--c4)}
.cell{stroke:var(--cell)}.stop3{stop-color:var(--c3)}.l{font-size:16px}
.ok{stroke:var(--ok);stroke-width:1.5}.area{fill:var(--c3);fill-opacity:.45;stroke:var(--ok);stroke-width:1.5;stroke-linejoin:round}
.dot{fill:var(--bg);stroke:var(--ok);stroke-width:1.5}.edu.now{stroke:var(--ok)}
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
    now = m["now"]
    act, reach = m["activity"], m["reach"]

    def rule(y, x1=0, x2=W):
        out.append(f'<line x1="{x1}" y1="{y}.5" x2="{x2}" y2="{y}.5" class="rule"/>')

    # header
    out.append(t(PAD, 30, "Atividade nos últimos 12 meses", "h"))
    out.append(t(W - PAD, 30, f"Atualizado em {day_label(now.date(), True)}", "s m", "end"))
    y = 46
    rule(y)

    # numbers
    ls, le = m["longest_start"], m["longest_end"]
    stats = [
        (num(m["contrib"]), "", "Contribuições", f"{num(m['total'])} commits e {num(act['prs'])} PRs"),
        (str(m["active_days"]), " de 365", "Dias com contribuições", f"{round(100 * m['active_days'] / 365)}% do ano"),
        (str(m["current"]), " dias" if m["current"] != 1 else " dia", "Sequência atual",
         f"desde {day_label(m['current_start'])}" if m["current"] else "sem contribuições hoje ou ontem"),
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
    rule(y)

    # contribution calendar, drawn like github.com
    per_day, first, today = m["per_day"], m["first"], m["today"]
    out.append(t(PAD, y + 30, f"{num(m['contrib'])} contribuições no último ano", "l"))
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    columns = (today - start).days // 7 + 1
    cstep, csize = 12, 10
    x0, y0 = PAD + 30, y + 62
    level = levels([per_day[first + timedelta(days=i)] for i in range(365)])
    sundays = [start + timedelta(days=7 * c) for c in range(columns)]
    changes = [c for c in range(1, columns) if sundays[c].month != sundays[c - 1].month]
    label_cols = ([0] if not changes or changes[0] >= 3 else []) + [c for c in changes if c <= columns - 3]
    for c in label_cols:
        out.append(t(x0 + c * cstep, y0 - 8, MONTHS[sundays[c].month - 1].capitalize(), "s"))
    for r, name in ((1, "seg"), (3, "qua"), (5, "sex")):
        out.append(t(PAD, y0 + r * cstep + 9, name, "s"))
    for c, sunday in enumerate(sundays):
        for r in range(7):
            d = sunday + timedelta(days=r)
            if first <= d <= today:
                out.append(f'<rect x="{x0 + c * cstep}" y="{y0 + r * cstep}" width="{csize}" height="{csize}" rx="2" '
                           f'class="c{level(per_day[d])} cell"/>')
    ly = y0 + 7 * cstep + 22
    out.append(t(PAD, ly, "Commits, pull requests, issues e repositórios criados, inclusive em repositórios privados", "s m"))
    lx = W - PAD - 30 - 5 * cstep
    out.append(t(lx - 6, ly, "Menos", "s m", "end"))
    for k in range(5):
        out.append(f'<rect x="{lx + k * cstep}" y="{ly - 9}" width="{csize}" height="{csize}" rx="2" class="c{k} cell"/>')
    out.append(t(lx + 5 * cstep + 4, ly, "Mais", "s m"))
    y = ly + 18
    rule(y)

    # overview (left) and hours (right)
    top, half = y, W // 2
    out.append(t(PAD, y + 28, "Visão geral", "t"))
    if m["private_ok"]:
        out.append(t(PAD, y + 47, f"Contribuiu com {reach['repos']} repositórios, {reach['private']} deles privados", "s m"))
    else:
        out.append(t(PAD, y + 47, f"Contribuiu com {reach['repos']} repositórios públicos", "s m"))
    if reach["orgs"]:
        out.append(t(PAD, y + 65, "Organizações: " + ", ".join("@" + o for o in reach["orgs"][:3]), "s m"))
    parts = {"Commits": m["total"], "Pull requests": act["prs"], "Issues": act["issues"], "Revisões": act["reviews"]}
    whole = sum(parts.values()) or 1
    share = {k: v / whole for k, v in parts.items()}
    cx, cy, arm = half / 2, y + 158, 62

    def pct_text(k):
        if not parts[k]:
            return ""
        v = 100 * share[k]
        return "<1%" if v < 0.5 else f"{round(v)}%"

    out.append(f'<line x1="{cx}" y1="{cy - arm}" x2="{cx}" y2="{cy + arm}" class="ok"/>'
               f'<line x1="{cx - arm}" y1="{cy}" x2="{cx + arm}" y2="{cy}" class="ok"/>')
    pts = [(cx, cy - arm * share["Revisões"]), (cx + arm * share["Issues"], cy),
           (cx, cy + arm * share["Pull requests"]), (cx - arm * share["Commits"], cy)]
    out.append('<polygon points="' + " ".join(f"{px:.1f},{py:.1f}" for px, py in pts) + '" class="area"/>')
    for px, py in pts:
        out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" class="dot"/>')
    labels = [("Revisões", cx, cy - arm - 10, "middle", -16), ("Pull requests", cx, cy + arm + 34, "middle", -16),
              ("Commits", cx - arm - 10, cy + 14, "end", -16), ("Issues", cx + arm + 10, cy + 14, "start", -16)]
    for name, lx_, ly_, anchor, up in labels:
        out.append(t(lx_, ly_, name, "s m", anchor))
        if pct_text(name):
            out.append(t(lx_, ly_ + up, pct_text(name), "s m", anchor))
    left_bottom = cy + arm + 52

    rx = half + PAD
    grid = m["grid"]
    hl = levels([v for row in grid for v in row])
    off = round(100 * m["off_hours"])
    out.append(t(rx, y + 28, "Horário dos commits", "t"))
    out.append(t(rx, y + 47, f"{off}% fora do expediente (seg a sex, 8h às 18h)" if m["total"]
                 else "Sem commits no período", "s m"))
    gx, gy, hstep, hcell = rx + 30, y + 66, 12, 10
    for d in range(7):
        if d in (0, 2, 4):
            out.append(t(rx, gy + d * hstep + 9, WEEKDAYS[d], "s"))
        for h in range(24):
            out.append(f'<rect x="{gx + h * hstep}" y="{gy + d * hstep}" width="{hcell}" height="{hcell}" rx="2" '
                       f'class="c{hl(grid[d][h])} cell"/>')
    for h in (0, 6, 12, 18):
        out.append(t(gx + h * hstep, gy + 7 * hstep + 16, f"{h}h", "s m"))
    hy = gy + 7 * hstep + 38
    out.append(t(rx, hy, "Horário de Brasília", "s m"))
    hx = W - PAD - 30 - 5 * hstep
    out.append(t(hx - 6, hy, "Menos", "s m", "end"))
    for k in range(5):
        out.append(f'<rect x="{hx + k * hstep}" y="{hy - 9}" width="{hcell}" height="{hcell}" rx="2" class="c{k} cell"/>')
    out.append(t(hx + 5 * hstep + 4, hy, "Mais", "s m"))
    right_bottom = hy + 18
    y = max(left_bottom, right_bottom)
    out.append(f'<line x1="{half}.5" y1="{top}" x2="{half}.5" y2="{y}" class="rule"/>')
    rule(y)

    # languages, like a repository's language bar
    out.append(t(PAD, y + 28, "Linguagens", "t"))
    out.append(t(PAD, y + 47, "Proporção nos commits do ano", "s m"))
    langs, total = m["langs"], m["lang_total"] or 1
    bar_y, bar_w = y + 62, W - 2 * PAD
    defs.append(f'<clipPath id="langbar"><rect x="{PAD}" y="{bar_y}" width="{bar_w}" height="8" rx="4"/></clipPath>')
    cursor, segments = float(PAD), []
    for name, n in langs:
        width = bar_w * n / total
        color = escape(m["colors"].get(name, "#8b949e"))
        segments.append(f'<rect x="{cursor:.2f}" y="{bar_y}" width="{max(width - 2, 0.5):.2f}" height="8" fill="{color}"/>')
        cursor += width
    out.append(f'<g clip-path="url(#langbar)"><rect x="{PAD}" y="{bar_y}" width="{bar_w}" height="8" class="c0"/>'
               f'{"".join(segments)}</g>')
    colw = bar_w / 3
    for i, (name, n) in enumerate(langs):
        c, r = i % 3, i // 3
        lx2, ly2 = PAD + c * colw, bar_y + 34 + r * 24
        color = escape(m["colors"].get(name, "#8b949e"))
        out.append(f'<circle cx="{lx2 + 4:.1f}" cy="{ly2 - 4.5}" r="4" fill="{color}"/>')
        out.append(t(lx2 + 14, ly2, name, "t" if i == 0 else ""))
        out.append(t(lx2 + colw - 24, ly2, f"{100 * n / total:.1f}%".replace(".", ","), "s m", "end"))
    if not langs:
        out.append(t(PAD, bar_y + 34, "Sem dados no período", "s m"))
    rows = max(1, (len(langs) + 2) // 3)
    y = bar_y + 34 + (rows - 1) * 24 + 18
    rule(y)

    # career, as a Gantt chart
    out.append(t(PAD, y + 28, "Trajetória e formação", "t"))
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

    def row(base, company, role, xa, xb, kind, xa_year=None, xb_year=None):
        out.append(t(PAD, base, f'{escape(company)}<tspan class="s m" dx="6">{escape(role)}</tspan>', raw=True))
        width = max(xb - xa, 3)
        if kind == "now":
            out.append(f'<rect x="{xa:.1f}" y="{base - 10}" width="{width:.1f}" height="8" rx="4" fill="url(#dawn)"/>')
        elif kind == "edu":
            out.append(f'<rect x="{xa:.1f}" y="{base - 9.4}" width="{width:.1f}" height="6.8" rx="3.4" class="edu"/>')
            years = round(xb_year - xa_year) if xb_year else 0
            if years:
                out.append(t(xb + 6, base - 2, f"{years} anos", "s m"))
        elif kind == "study":
            out.append(f'<rect x="{xa:.1f}" y="{base - 9.4}" width="{width:.1f}" height="6.8" rx="3.4" class="edu now"/>')
        else:
            out.append(f'<rect x="{xa:.1f}" y="{base - 10}" width="{width:.1f}" height="8" rx="4" class="past"/>')

    for i, (begin, stop, company, role) in enumerate(CAREER):
        row(first_row + i * step_row, company, role, tx(begin), tx(stop if stop else end), "past" if stop else "now")
    for k, (begin, stop, school, degree) in enumerate(DEGREES):
        if stop:
            row(edu_top + k * step_row, school, degree, tx(begin), tx(stop), "edu", begin, stop)
        else:
            row(edu_top + k * step_row, school, f"{degree}, {semester(begin, today)}º semestre",
                tx(begin), tx(end), "study")
    y = bottom + 34
    rule(y)

    scope = (f"Dados de {reach['repos']} repositórios, {reach['private']} deles privados (sem nomes)"
             if m["private_ok"] else f"Dados de {reach['repos']} repositórios públicos")
    out.append(t(PAD, y + 24, f"{scope}. Atualizado a cada 6 horas.", "s m"))
    H = y + 40

    desc = (f"{num(m['contrib'])} contribuições nos últimos 12 meses ({num(m['total'])} commits, "
            f"{num(act['prs'])} pull requests, {num(act['issues'])} issues), {m['active_days']} dias com contribuições, "
            f"sequência atual de {m['current']} dias e maior sequência de {m['longest']} dias. "
            "Trajetória: Endosul, Kipel, McDonald's, Hora do Pastel e BGPF Group. "
            "Formação: Administração (FSG), Processos Gerenciais (Feevale) e Sistemas de Informação (UFBRA, em curso).")
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
