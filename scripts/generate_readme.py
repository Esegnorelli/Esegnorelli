#!/usr/bin/env python3
"""Fill README.gtpl from the GitHub API and write README.md."""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent
HOMEPAGE_RE = re.compile(r"^https?://[^\s<>()\[\]\\`]+$")


class FetchError(Exception):
    pass


def fetch(path: str, token: str):
    url = path if path.startswith("http") else f"{API}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "esegnorelli-profile",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise FetchError(f"{url} — {exc}") from exc


def md(text: str) -> str:
    """One line of third-party text, safe to put in Markdown.

    Descriptions and titles are written by other people, so brackets, angle
    brackets and emphasis marks are escaped, and bare URLs are kept from
    turning into links: no image, link or HTML can be smuggled into the profile.
    """
    text = re.sub(r"([\\`*_\[\]()<>~|])", r"\\\1", " ".join((text or "").split()))
    text = re.sub(r"(?i)\b((?:https?|ftp)):", r"\1\\:", text)
    return re.sub(r"(?i)\bwww\.", r"www\\.", text)


def bullet_repo(repo: dict, details: bool = False) -> str:
    name = md(repo.get("full_name") or repo.get("name") or "repository")
    url = repo.get("html_url") or ""
    desc = md(repo.get("description"))
    line = f"- [{name}]({url})"
    if desc:
        line += f" - {desc}"
    if details:
        meta = []
        language = md(repo.get("language"))
        if language:
            meta.append(language)
        homepage = (repo.get("homepage") or "").strip()
        if HOMEPAGE_RE.match(homepage):
            meta.append(f"[site]({homepage})")
        if meta:
            line += " · " + " · ".join(meta)
    return line


def own_repos(username: str, token: str, sort: str) -> list:
    repos = fetch(
        f"/users/{quote(username)}/repos?sort={sort}&direction=desc&per_page=100&type=owner",
        token,
    )
    visible = [
        repo for repo in repos
        if not repo.get("fork")
        and not repo.get("private")
        and repo.get("name", "").lower() != username.lower()
    ]
    return visible


def working_on(username: str, token: str) -> str:
    lines = [bullet_repo(repo, details=True) for repo in own_repos(username, token, "pushed")[:5]]
    return "\n".join(lines) + "\n" if lines else "- Nada público mexido ainda.\n"


def recent_prs(username: str, token: str) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    query = quote(f"author:{username} type:pr is:public created:>={since}")
    try:
        data = fetch(
            f"/search/issues?q={query}&sort=created&order=desc&per_page=5",
            token,
        )
    except FetchError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
        data = None
    items = (data or {}).get("items", []) if isinstance(data, dict) else []

    if not items:
        try:
            events = fetch(f"/users/{quote(username)}/events/public?per_page=100", token)
        except FetchError:
            if data is None:
                raise
            events = []
        for event in events:
            payload = event.get("payload") or {}
            repo = (event.get("repo") or {}).get("name")
            number = payload.get("number")
            # Events are the user's own actions on any PR: "closed" and "merged"
            # also cover other people's, so only "opened" marks one they wrote.
            # The payload carries the PR's number but neither title nor URL.
            if event.get("type") != "PullRequestEvent" or payload.get("action") != "opened" or not repo or not number:
                continue
            try:
                title = fetch(f"/repos/{quote(repo, safe='/')}/pulls/{number}", token).get("title")
            except FetchError:
                title = None
            items.append({
                "title": title or "Pull request",
                "html_url": f"https://github.com/{repo}/pull/{number}",
                "repository_url": f"{API}/repos/{repo}",
            })
            if len(items) == 5:
                break

    lines = []
    for item in items[:5]:
        parts = item.get("repository_url", "").rstrip("/").rsplit("/", 2)
        repo = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 3 else "repository"
        title = md(item.get("title") or "Pull request")
        lines.append(
            f"- [{title}]({item['html_url']}) em [{repo}](https://github.com/{repo})"
        )
    return "\n".join(lines) + "\n" if lines else "- Nenhum pull request recente.\n"


def recent_stars(username: str, token: str) -> str:
    stars = fetch(
        f"/users/{quote(username)}/starred?sort=created&direction=desc&per_page=5",
        token,
    )
    lines = []
    for repo in stars[:5]:
        if isinstance(repo, dict) and repo.get("full_name"):
            lines.append(bullet_repo(repo))
    return "\n".join(lines) + "\n" if lines else "- Nenhuma estrela recente.\n"


def main() -> int:
    username = os.getenv("GH_USERNAME") or os.getenv("GITHUB_REPOSITORY_OWNER") or "Esegnorelli"
    token = os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN") or ""
    template = ROOT / "README.gtpl"
    output = ROOT / "README.md"
    if not template.exists():
        print(f"Error: {template} not found.", file=sys.stderr)
        return 1

    content = template.read_text(encoding="utf-8")
    try:
        replacements = {
            "{{WORKING_ON}}": working_on(username, token),
            "{{RECENT_PRS}}": recent_prs(username, token),
            "{{RECENT_STARS}}": recent_stars(username, token),
        }
    except FetchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for key, value in replacements.items():
        if key not in content:
            print(f"Error: {key} missing from template.", file=sys.stderr)
            return 1
        content = content.replace(key, value)

    output.write_text(content, encoding="utf-8")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
