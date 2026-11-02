#!/usr/bin/env python3
"""Draw the profile hero and dashboard as animated SVGs from GitHub data.

Counts commits on the default branch of every repository the token can
see, so private work counts without any repository name leaving the API.
If the API fails, nothing is written.
"""

import json
import math
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from svgtext import Font, Glyphs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "assets" / "fonts"
API = "https://api.github.com/graphql"

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - tzdata missing
    TZ = timezone(timedelta(hours=-3))

MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
WEEKDAYS_LONG = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
PRETTY = {"PLpgSQL": "PL/pgSQL", "Jupyter Notebook": "Jupyter", "Vim Script": "Vim script"}

# Career, from the CV and from Evandro himself. Years are decimal (month / 12).
CAREER = [
    (2007.00, 2012.42, "Endosul", "planejamento industrial", "above"),
    (2012.67, 2012.92, "Rotamil", "produção", "below"),
    (2013.50, 2015.25, "Kipel", "vendas", "above"),
    (2016.08, 2017.83, "Val Belluna · Rex", "restaurante e gestão", "below"),
    (2019.83, 2020.92, "Tais Comércio de Pastéis", "gerente administrativo", "above"),
]
CODE_START = 2021.0
CODE_LABEL = ("Hora do Pastel → BGPF Group", "sistemas (MEI) → dados e gestão")
DEGREES = [(2016.58, "Administração · FSG"), (2022.58, "Processos Gerenciais · Feevale")]

W = 960


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
               "languages(first:10,orderBy:{field:SIZE,direction:DESC}){edges{size node{name}}}}")
IGNORED = {"Dockerfile", "Makefile", "Procfile", "Batchfile"}
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

    commits, languages, active_repos, private_repos = [], Counter(), 0, 0
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
            name = e["node"]["name"]
            languages[PRETTY.get(name, name)] += len(times) * e["size"] / size
    return model(now, commits, languages, active_repos, private_repos, private_ok)


def model(now, commits, languages, active_repos, private_repos, private_ok) -> dict:
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
    other = sum(v for k, v in languages.items() if not k) + sum(v for _, v in named[4:])
    langs = named[:4] + ([("outras", other)] if other else [])

    return {
        "now": now, "total": total, "weeks": weeks, "active_days": sum(1 for d in days if per_day[d]),
        "longest": longest, "longest_start": best_end - timedelta(days=longest - 1) if best_end else None,
        "longest_end": best_end, "current": current,
        "current_start": anchor - timedelta(days=current - 1) if current else None,
        "grid": grid, "off_hours": 1 - office / total if total else 0.0,
        "langs": langs, "lang_total": sum(languages.values()),
        "repos": active_repos, "private": private_repos, "private_ok": private_ok,
    }


# -- drawing helpers ----------------------------------------------------------

def num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def day_label(d: date, year: bool = False) -> str:
    text = f"{d.day} {MONTHS[d.month - 1]}"
    return f"{text} {d.year}" if year else text


def smooth(points) -> str:
    """Catmull-Rom through the points, as cubic Béziers."""
    out = [f"M{points[0][0]:.1f} {points[0][1]:.1f}"]
    for i in range(len(points) - 1):
        p0 = points[max(i - 1, 0)]
        p1, p2 = points[i], points[i + 1]
        p3 = points[min(i + 2, len(points) - 1)]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        out.append(f"C{c1[0]:.1f} {c1[1]:.1f} {c2[0]:.1f} {c2[1]:.1f} {p2[0]:.1f} {p2[1]:.1f}")
    return "".join(out)


STYLE = """
:root{--bg:#141311;--card:#1b1a17;--line:#2c2925;--ink:#f5f1ea;--mute:#8f887d;--faint:#38342e;--gold:#d6b17a;--band:#211f1b}
@media (prefers-color-scheme:light){:root{--bg:#f7f2e8;--card:#f0e9dc;--line:#e3d9c7;--ink:#1d1b18;--mute:#7a7265;--faint:#dcd1bd;--gold:#a8742c;--band:#e9e0cf}}
.bg{fill:var(--bg)}.card{fill:var(--card);stroke:var(--line)}.edge{fill:none;stroke:var(--line)}
.ink{fill:var(--ink)}.mute{fill:var(--mute)}.faint{fill:var(--faint)}.gold{fill:var(--gold)}.band{fill:var(--band)}
.stroke-gold{fill:none;stroke:var(--gold)}.stroke-faint{fill:none;stroke:var(--faint)}.stroke-mute{fill:none;stroke:var(--mute)}
.stop-gold{stop-color:var(--gold)}
.rise{animation:rise .9s cubic-bezier(.2,.7,.2,1) backwards}
.fade{animation:fade 1.2s ease-out backwards}
.draw{stroke-dasharray:1000;animation:draw 2.6s cubic-bezier(.6,0,.2,1) backwards}
.grow{transform-box:fill-box;transform-origin:0 50%;animation:grow 1.1s cubic-bezier(.2,.7,.2,1) backwards}
.pop{transform-box:fill-box;transform-origin:50% 50%;animation:pop .7s cubic-bezier(.3,1.6,.5,1) backwards}
.roll{animation:roll 1.8s cubic-bezier(.15,.8,.2,1) backwards}
.breathe{animation:breathe 3.2s ease-in-out infinite}
.ring{animation:ring 1.8s cubic-bezier(.2,.7,.2,1) backwards}
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
@keyframes fade{from{opacity:0}}
@keyframes draw{from{stroke-dashoffset:1000}}
@keyframes grow{from{transform:scaleX(0)}}
@keyframes pop{from{transform:scale(0);opacity:0}}
@keyframes roll{from{transform:translateY(0)}}
@keyframes breathe{50%{opacity:.3}}
@keyframes ring{from{stroke-dasharray:0 100}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""


class Canvas:
    def __init__(self, height: int, title: str, desc: str):
        self.h = height
        self.title, self.desc = title, desc
        self.g = Glyphs()
        self.q = Font(str(FONTS / "iAWriterQuattroS-Regular.ttf"), "q")
        self.i = Font(str(FONTS / "iAWriterQuattroS-Italic.ttf"), "i")
        self.m = Font(str(FONTS / "iAWriterMonoS-Regular.ttf"), "m")
        self.defs, self.body = [], []

    def add(self, *parts: str):
        self.body.extend(parts)

    def text(self, font, s, size, x, y, cls="ink", anchor="start", tracking=0.0, delay=None, anim="rise"):
        drawn = self.g.text(font, s, size, x, y, anchor=anchor, tracking=tracking, cls=cls)
        if delay is None:
            return drawn
        return f'<g class="{anim}" style="animation-delay:{delay:.2f}s">{drawn}</g>'

    def label(self, s, x, y, anchor="start", delay=None, cls="mute", size=12):
        return self.text(self.m, s.upper(), size, x, y, cls=cls, anchor=anchor, tracking=0.12, delay=delay)

    def odometer(self, value: str, size: float, x: float, y: float, delay: float, cid: str) -> str:
        font = self.q
        scale = size / font.upm
        width = font.width(value, size)
        step = size * 1.15
        self.defs.append(f'<clipPath id="{cid}"><rect x="{x - 2:.1f}" y="{y - size * 0.82:.1f}" '
                         f'width="{width + 4:.1f}" height="{size * 0.98:.1f}"/></clipPath>')
        out, cursor = [f'<g clip-path="url(#{cid})" class="ink">'], x
        for k, ch in enumerate(value):
            adv = font.advance(font.glyph(ch)) * scale
            if ch.isdigit():
                d = int(ch)
                uses = "".join(
                    f'<use href="#{self.g.ref(font, font.glyph(str(j % 10)))}" y="{-j * step / scale:.0f}"/>'
                    for j in range(10 + d + 1))
                shift = (10 + d) * step
                out.append(f'<g class="roll" style="transform:translateY(-{shift:.1f}px);'
                           f'animation-delay:{delay + 0.07 * k:.2f}s">'
                           f'<g transform="translate({cursor:.1f} {y:.1f}) scale({scale:.5f} {-scale:.5f})">'
                           f"{uses}</g></g>")
            else:
                out.append(self.g.text(font, ch, size, cursor, y))
            cursor += adv
        out.append("</g>")
        return "".join(out)

    def svg(self) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {self.h}" width="{W}" '
                f'height="{self.h}" role="img" aria-labelledby="t d">'
                f'<title id="t">{self.title}</title><desc id="d">{self.desc}</desc>'
                f"<style>{STYLE}</style><defs>{self.g.defs()}{''.join(self.defs)}</defs>"
                f"{''.join(self.body)}</svg>\n")


# -- hero -----------------------------------------------------------------------

def hero(m: dict) -> str:
    H = 384
    c = Canvas(H, "Evandro Segnorelli — do balcão ao banco de dados",
               "Analista de dados, desenvolvedor web e de scripts, gestor administrativo. "
               f"A linha dourada mostra os commits por semana nos últimos 12 meses ({num(m['total'])} no total).")
    c.defs.append(f'<clipPath id="card"><rect x="0" y="0" width="{W}" height="{H}" rx="18"/></clipPath>')
    c.defs.append('<linearGradient id="sand" x1="0" y1="0" x2="0" y2="1">'
                  '<stop offset="0" class="stop-gold" stop-opacity=".32"/>'
                  '<stop offset="1" class="stop-gold" stop-opacity="0"/></linearGradient>')
    c.defs.append('<filter id="grain" x="0" y="0" width="100%" height="100%">'
                  '<feTurbulence type="fractalNoise" baseFrequency=".85" numOctaves="2" stitchTiles="stitch"/>'
                  '<feColorMatrix values="0 0 0 0 .5 0 0 0 0 .45 0 0 0 0 .38 0 0 0 .07 0"/></filter>')

    # the dune: weekly commits, full bleed along the bottom
    counts = [n for _, n in m["weeks"]]
    top = max(counts) or 1
    base, height = H + 2, 78
    xs = [-8 + i * (W + 16) / (len(counts) - 1) for i in range(len(counts))]
    pts = [(x, base - 6 - height * math.sqrt(n / top)) for x, n in zip(xs, counts)]
    line = smooth(pts)
    area = line + f"L{W + 8} {base}L-8 {base}Z"
    peak = counts.index(top)
    px, py = pts[peak]

    c.defs.append(f'<radialGradient id="glow" cx="{px / W:.3f}" cy="1" r=".55">'
                  '<stop offset="0" class="stop-gold" stop-opacity=".16"/>'
                  '<stop offset="1" class="stop-gold" stop-opacity="0"/></radialGradient>')

    c.add('<g clip-path="url(#card)">', f'<rect class="bg" width="{W}" height="{H}"/>',
          f'<rect width="{W}" height="{H}" fill="url(#glow)" class="fade" style="animation-delay:1.4s"/>',
          f'<rect width="{W}" height="{H}" filter="url(#grain)"/>',
          f'<path d="{area}" fill="url(#sand)" class="fade" style="animation-delay:1.6s"/>',
          f'<path d="{line}" class="stroke-gold draw" stroke-width="2" pathLength="1000" '
          'style="animation-delay:.5s" stroke-linejoin="round"/>', "</g>")
    c.add(f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="17.5" class="edge"/>')

    # the island: a quiet pill with the name
    name = "evandro segnorelli"
    pw = 34 + c.m.width(name, 14) + 16
    c.add(f'<g class="rise" style="animation-delay:.05s"><rect x="40" y="36" width="{pw:.1f}" height="32" rx="16" class="card"/>'
          '<circle cx="57" cy="52" r="4" class="gold breathe"/></g>',
          c.text(c.m, name, 14, 72, 57, delay=0.05))
    c.add(c.text(c.m, "o fio dourado é o meu ano em commits ↘", 13.5, W - 40, 57, cls="mute",
                 anchor="end", delay=2.2, anim="fade"))

    c.add(c.text(c.q, "do balcão ao", 66, 37, 146, delay=0.15),
          c.text(c.q, "banco de dados.", 66, 37, 216, delay=0.3))
    c.add(c.label("analista de dados  ·  dev web & scripts  ·  gestor administrativo", 40, 256, delay=0.5, size=13.5))

    # peak annotation
    week_start = m["weeks"][peak][0]
    note = f"pico · semana de {day_label(week_start)} · {num(top)} commits"
    right = px > W * 0.62
    tx = px - 14 if right else px + 14
    c.add(f'<g class="pop" style="animation-delay:2.7s"><circle cx="{px:.1f}" cy="{py:.1f}" r="11" class="gold" opacity=".18"/>'
          f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4.5" class="gold"/></g>',
          c.text(c.m, note, 13, tx, py - 14, cls="mute", anchor="end" if right else "start", delay=2.9, anim="fade"))
    return c.svg()


# -- dashboard ----------------------------------------------------------------

def tile(c: Canvas, x, y, w, h, label, value, sub, delay, cid, ring=None):
    c.add(f'<g class="rise" style="animation-delay:{delay:.2f}s"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" class="card"/></g>')
    c.add(c.label(label, x + 18, y + 28, delay=delay + 0.05))
    c.add(c.odometer(value, 44, x + 17, y + 80, delay + 0.15, cid))
    c.add(c.text(c.m, sub, 12.5, x + 18, y + 102, cls="mute", delay=delay + 0.3, anim="fade"))
    if ring is not None:
        cx, cy, r = x + w - 40, y + 52, 19
        pct = max(0.0, min(ring, 1.0)) * 100
        c.add(f'<circle cx="{cx}" cy="{cy}" r="{r}" class="stroke-faint" stroke-width="4"/>',
              f'<circle cx="{cx}" cy="{cy}" r="{r}" class="stroke-gold ring" stroke-width="4" stroke-linecap="round" '
              f'pathLength="100" stroke-dasharray="{pct:.1f} 100" transform="rotate(-90 {cx} {cy})" '
              f'style="animation-delay:{delay + 0.3:.2f}s"/>')


def painel(m: dict) -> str:
    H = 820
    c = Canvas(H, "Painel de Evandro Segnorelli",
               f"{num(m['total'])} commits nos últimos 12 meses, {m['active_days']} dias ativos, "
               f"sequência atual de {m['current']} dias e maior sequência de {m['longest']} dias.")
    c.add(f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="17.5" class="bg" stroke-width="1"/>',
          f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="17.5" class="edge"/>')

    now = m["now"]
    c.add(c.text(c.q, "o último ano, em dados", 22, 40, 58, delay=0.0))
    stamp = f"atualizado em {day_label(now.date(), True)}, {now:%H:%M}"
    c.add(c.text(c.m, stamp, 12.5, W - 40, 58, cls="mute", anchor="end", delay=0.1, anim="fade"),
          f'<circle cx="{W - 52 - c.m.width(stamp, 12.5):.1f}" cy="54" r="3.5" class="gold breathe"/>')

    # KPIs
    y, w, gap = 82, 208, 16
    ls, le = m["longest_start"], m["longest_end"]
    current_sub = f"dias · desde {day_label(m['current_start'])}" if m["current"] else "recomeça no próximo commit"
    tiles = [
        ("commits", num(m["total"]), "nos últimos 12 meses", None),
        ("dias ativos", str(m["active_days"]), f"de 365 · {round(100 * m['active_days'] / 365)}%", None),
        ("sequência atual", str(m["current"]), current_sub, (m["current"] / m["longest"]) if m["longest"] else 0),
        ("maior sequência", str(m["longest"]),
         f"dias · {day_label(ls)} – {day_label(le)}" if ls else "sem sequência no período", None),
    ]
    for i, (label, value, sub, ring) in enumerate(tiles):
        tile(c, 40 + i * (w + gap), y, w, 116, label, value, sub, 0.15 + 0.12 * i, f"k{i}", ring)

    # when: weekday × hour
    x0, y0, cw, ch = 40, 214, 572, 266
    c.add(f'<g class="rise" style="animation-delay:.6s"><rect x="{x0}" y="{y0}" width="{cw}" height="{ch}" rx="12" class="card"/></g>')
    c.add(c.label("quando eu programo · brasília", x0 + 20, y0 + 30, delay=0.65))
    grid = m["grid"]
    top = max(max(row) for row in grid) or 1
    pd, ph = max(((d, h) for d in range(7) for h in range(24)), key=lambda k: grid[k[0]][k[1]])
    off = round(100 * m["off_hours"])
    if not m["total"]:
        title = "sem commits no período"
    elif off >= 50:
        title = f"{off}% dos commits saem fora do expediente"
    else:
        title = f"o pico é {WEEKDAYS_LONG[pd]} às {ph}h"
    c.add(c.text(c.q, title, 19, x0 + 20, y0 + 60, delay=0.7))
    gx, gy, cell, row = x0 + 62, y0 + 84, 20, 21
    c.add(f'<g class="fade" style="animation-delay:.9s"><rect x="{gx + 8 * cell - 2}" y="{gy - 3}" width="{10 * cell + 4}" '
          f'height="{5 * row + 6}" rx="8" class="band"/></g>')
    for d in range(7):
        c.add(c.text(c.m, WEEKDAYS[d], 12.5, x0 + 20, gy + d * row + 14.5, cls="mute", delay=0.8, anim="fade"))
        for h in range(24):
            n = grid[d][h]
            cx, cy = gx + h * cell + cell / 2, gy + d * row + row / 2
            delay = 1.0 + 0.025 * h + 0.02 * d
            if n:
                r = 1.8 + 7.2 * math.sqrt(n / top)
                op = 0.45 + 0.55 * (n / top)
                c.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" class="gold pop" opacity="{op:.2f}" '
                      f'style="animation-delay:{delay:.2f}s"/>')
            else:
                c.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="1.3" class="faint"/>')
    for h in (0, 6, 12, 18, 23):
        c.add(c.text(c.m, f"{h}h", 12, gx + h * cell + cell / 2, y0 + ch - 14, cls="mute", anchor="middle",
                     delay=0.9, anim="fade"))
    legend = "expediente: seg–sex, 8h–18h"
    lx = x0 + cw - 20 - c.m.width(legend, 12)
    c.add(f'<g class="fade" style="animation-delay:1s"><rect x="{lx - 22:.1f}" y="{y0 + 20}" width="14" height="12" rx="3" class="band" '
          f'stroke="var(--line)"/></g>',
          c.text(c.m, legend, 12, x0 + cw - 20, y0 + 30, cls="mute", anchor="end", delay=1.0, anim="fade"))
    fact = (f"pico · {WEEKDAYS[pd]} {ph}h" if off >= 50 else f"{off}% fora do expediente") if m["total"] else ""
    c.add(c.text(c.m, fact, 13, x0 + cw - 20, y0 + 60, cls="mute", anchor="end", delay=1.2, anim="fade"))

    # languages
    x1, lw = x0 + cw + gap, W - 40 - (x0 + cw + gap)
    c.add(f'<g class="rise" style="animation-delay:.7s"><rect x="{x1}" y="{y0}" width="{lw}" height="{ch}" rx="12" class="card"/></g>')
    c.add(c.label("em que linguagem", x1 + 20, y0 + 30, delay=0.75))
    langs, total = m["langs"], m["lang_total"] or 1
    if langs:
        named = [(k, v) for k, v in langs if k != "outras"]
        if len(named) > 1 and named[1][1] / total >= 0.2:
            headline = f"{named[0][0]} e {named[1][0]}"
        else:
            headline = f"{langs[0][0]} lidera"
        c.add(c.text(c.q, headline, 19, x1 + 20, y0 + 60, delay=0.8))
    for i, (name, n) in enumerate(langs[:5]):
        ry = y0 + 96 + i * 34
        pct = 100 * n / total
        c.add(c.text(c.q, name, 15, x1 + 20, ry, delay=0.9 + 0.08 * i, anim="fade"),
              c.text(c.m, f"{pct:.0f}%" if pct >= 1 else "<1%", 13, x1 + lw - 20, ry, cls="mute", anchor="end", delay=0.9 + 0.08 * i, anim="fade"))
        bw = lw - 40
        c.add(f'<rect x="{x1 + 20}" y="{ry + 8}" width="{bw}" height="5" rx="2.5" class="faint"/>',
              f'<rect x="{x1 + 20}" y="{ry + 8}" width="{max(bw * pct / 100, 5):.1f}" height="5" rx="2.5" '
              f'class="{"gold" if i == 0 else "mute"} grow" style="animation-delay:{1.0 + 0.1 * i:.2f}s"/>')

    # career
    ty, th = y0 + ch + gap, 262
    c.add(f'<g class="rise" style="animation-delay:.85s"><rect x="40" y="{ty}" width="{W - 80}" height="{th}" rx="12" class="card"/></g>')
    c.add(c.label("trajetória", 60, ty + 30, delay=0.9))
    years = now.year - 2007
    c.add(c.text(c.q, f"{years} anos entre operação, gestão e, agora, código", 19, 60, ty + 60, delay=0.95))
    t0, t1 = 60, W - 60
    end = now.year + (now.month - 1) / 12 + now.day / 365

    def tx(year):
        return t0 + (year - 2007) * (t1 - t0) / (end + 0.35 - 2007)

    lane = ty + 130
    c.add(f'<line x1="{t0}" y1="{lane + 4}" x2="{t1}" y2="{lane + 4}" class="stroke-faint" stroke-width="1"/>')
    for k, (a, b, company, role, side) in enumerate(CAREER):
        delay = 1.1 + 0.12 * k
        c.add(f'<rect x="{tx(a):.1f}" y="{lane}" width="{max(tx(b) - tx(a), 4):.1f}" height="8" rx="4" '
              f'class="mute grow" style="animation-delay:{delay:.2f}s"/>')
        if side == "above":
            c.add(c.text(c.q, company, 14, tx(a), lane - 30, delay=delay + 0.1, anim="fade"),
                  c.text(c.m, role, 12.5, tx(a), lane - 14, cls="mute", delay=delay + 0.1, anim="fade"))
        else:
            c.add(c.text(c.q, company, 14, tx(a), lane + 32, delay=delay + 0.1, anim="fade"),
                  c.text(c.m, role, 12.5, tx(a), lane + 48, cls="mute", delay=delay + 0.1, anim="fade"))
    c.defs.append(f'<linearGradient id="dawn" gradientUnits="userSpaceOnUse" x1="{tx(CODE_START):.1f}" x2="{tx(CODE_START + 1.2):.1f}" y1="0" y2="0">'
                  '<stop offset="0" class="stop-gold" stop-opacity="0"/><stop offset="1" class="stop-gold"/></linearGradient>')
    gold_delay = 1.1 + 0.12 * len(CAREER)
    c.add(f'<rect x="{tx(CODE_START):.1f}" y="{lane - 1}" width="{tx(end) - tx(CODE_START):.1f}" height="10" rx="5" '
          f'fill="url(#dawn)" class="grow" style="animation-delay:{gold_delay:.2f}s"/>',
          f'<g class="pop" style="animation-delay:{gold_delay + 1:.2f}s"><circle cx="{tx(end):.1f}" cy="{lane + 4}" r="10" class="gold breathe" opacity=".25"/>'
          f'<circle cx="{tx(end):.1f}" cy="{lane + 4}" r="5" class="gold"/></g>')
    label_w = max(c.q.width(CODE_LABEL[0], 14), c.m.width(CODE_LABEL[1], 12.5))
    gx0 = min(tx(CODE_START + 1.4), t1 - label_w)
    c.add(c.text(c.q, CODE_LABEL[0], 14, gx0, lane + 32, delay=gold_delay + 0.3, anim="fade"),
          c.text(c.m, CODE_LABEL[1], 12.5, gx0, lane + 48, cls="mute", delay=gold_delay + 0.3, anim="fade"))

    # degrees
    dl = lane + 84
    c.add(c.label("formação", t0, dl + 4, delay=1.2, size=11.5))
    for k, (year, text) in enumerate(DEGREES):
        delay = 1.3 + 0.2 * k
        c.add(f'<g class="pop" style="animation-delay:{delay:.2f}s"><circle cx="{tx(year):.1f}" cy="{dl}" r="4" class="stroke-mute" stroke-width="1.5"/></g>',
              c.text(c.m, text, 12, tx(year) - 11, dl + 4, cls="mute", anchor="end", delay=delay, anim="fade"))
    for year in (2007, 2012, 2017, 2022):
        c.add(c.text(c.m, str(year), 12, tx(year), ty + th - 14, cls="mute", anchor="middle", delay=1.0, anim="fade"))
    c.add(c.text(c.m, "hoje", 12, tx(end), ty + th - 14, cls="mute", anchor="middle", delay=1.0, anim="fade"))

    scope = (f"{m['repos']} repositórios · {m['private']} privados, sem nomes" if m["private_ok"]
             else f"{m['repos']} repositórios públicos")
    c.add(c.text(c.m, f"{scope} · branch principal · gerado por scripts/render_dashboard.py a cada 6h",
                 12, W / 2, H - 22, cls="mute", anchor="middle", delay=1.5, anim="fade"))
    return c.svg()


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
    out = {ROOT / "assets" / "hero.svg": hero(data), ROOT / "assets" / "painel.svg": painel(data)}
    for path, content in out.items():
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        print(f"Wrote {path} ({len(content) // 1024} KB)")
    print(f"{data['total']} commits · {data['active_days']} active days · streak {data['current']}/{data['longest']} · "
          f"{data['repos']} repos ({data['private']} private)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
