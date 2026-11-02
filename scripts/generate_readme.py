#!/usr/bin/env python3
"""Fill README.gtpl from the GitHub API and write README.md."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent


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


def bullet_repo(repo: dict) -> str:
    name = repo.get("full_name") or repo.get("name") or "repository"
    url = repo.get("html_url") or ""
    desc = " ".join((repo.get("description") or "").split())
    line = f"- [{name}]({url})"
    if desc:
        line += f" - {desc}"
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
    lines = [bullet_repo(repo) for repo in own_repos(username, token, "pushed")[:5]]
    return "\n".join(lines) + "\n" if lines else "- No public activity yet.\n"


def latest_projects(username: str, token: str) -> str:
    lines = [bullet_repo(repo) for repo in own_repos(username, token, "created")[:5]]
    return "\n".join(lines) + "\n" if lines else "- No public repositories yet.\n"


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
            if event.get("type") != "PullRequestEvent":
                continue
            pr = (event.get("payload") or {}).get("pull_request") or {}
            if not pr.get("html_url"):
                continue
            items.append({
                "title": pr.get("title") or "Pull request",
                "html_url": pr.get("html_url"),
                "repository_url": "https://api.github.com/repos/" + event.get("repo", {}).get("name", ""),
            })
            if len(items) == 5:
                break

    lines = []
    for item in items[:5]:
        parts = item.get("repository_url", "").rstrip("/").rsplit("/", 2)
        repo = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 3 else "repository"
        title = " ".join((item.get("title") or "Pull request").split())
        lines.append(
            f"- [{title}]({item['html_url']}) on [{repo}](https://github.com/{repo})"
        )
    return "\n".join(lines) + "\n" if lines else "- No recent pull requests.\n"


def recent_stars(username: str, token: str) -> str:
    stars = fetch(
        f"/users/{quote(username)}/starred?sort=created&direction=desc&per_page=5",
        token,
    )
    lines = []
    for repo in stars[:5]:
        if isinstance(repo, dict) and repo.get("full_name"):
            lines.append(bullet_repo(repo))
    return "\n".join(lines) + "\n" if lines else "- No recent stars.\n"


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
            "{{LATEST_PROJECTS}}": latest_projects(username, token),
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
