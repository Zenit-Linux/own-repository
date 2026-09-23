#!/usr/bin/env python3
"""
Otwiera albo aktualizuje (dopisuje komentarz do) POJEDYNCZY, identyfikowalny
GitHub Issue -- zamiast tworzyć nowy przy każdym nieudanym uruchomieniu (co
zasypałoby repo duplikatami). Używane przez generate_latest_versions.py
(masowe niepowodzenie rozwiązywania wersji) i test_new_package.py w trybie
pełnego skanu (FULL_SCAN=1, patrz .github/workflows/full-scan.yml).

Wymaga GITHUB_TOKEN (permissions: issues: write) i GITHUB_REPOSITORY w
środowisku -- obydwa automatycznie dostępne w każdym jobie GitHub Actions,
zero dodatkowej konfiguracji.

Identyfikacja "tego samego" issue: każde zgłoszenie ma niewidoczny znacznik
HTML-comment z `key` w treści -- kolejne wywołania z tym samym `key`
znajdują istniejący OTWARTY issue z tym znacznikiem i dopisują komentarz,
zamiast tworzyć nowy.
"""
import json
import os
import sys
import urllib.error
import urllib.request

MARKER_TEMPLATE = "<!-- zenit-auto-issue:{key} -->"


def _api(method: str, path: str, token: str, body=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "User-Agent": "zenit-own-repository-generator",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_open_issue(repo_full_name: str, token: str, key: str):
    marker = MARKER_TEMPLATE.format(key=key)
    issues = _api("GET", f"/repos/{repo_full_name}/issues?state=open&per_page=50", token)
    for issue in issues:
        if "pull_request" in issue:  # API zwraca PR-y jako "issues" tez -- pomijamy
            continue
        if marker in (issue.get("body") or ""):
            return issue
    return None


def report(key: str, title: str, body: str) -> None:
    """`key` -- stabilny identyfikator (np. "generate-latest-versions",
    "full-scan"), żeby kolejne wywołania trafiały w TEN SAM issue zamiast
    tworzyć nowy za każdym razem."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo_full_name = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo_full_name:
        print("report_issue: brak GITHUB_TOKEN/GITHUB_REPOSITORY -- pomijam zgłoszenie issue.", file=sys.stderr)
        return

    marker = MARKER_TEMPLATE.format(key=key)
    full_body = f"{marker}\n\n{body}"
    try:
        existing = find_open_issue(repo_full_name, token, key)
        if existing:
            _api("POST", f"/repos/{repo_full_name}/issues/{existing['number']}/comments", token,
                 {"body": full_body})
            print(f"report_issue: dopisano komentarz do istniejącego issue #{existing['number']}")
        else:
            created = _api("POST", f"/repos/{repo_full_name}/issues", token,
                            {"title": title, "body": full_body, "labels": ["automated", "generator-failure"]})
            print(f"report_issue: utworzono nowy issue #{created['number']}")
    except urllib.error.HTTPError as e:
        print(f"report_issue: nie udało się zgłosić issue: HTTP {e.code} {e.reason}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"report_issue: nie udało się połączyć z GitHub API: {e.reason}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Użycie: report_issue.py <key> <tytuł> [treść na stdin]", file=sys.stderr)
        sys.exit(2)
    report(sys.argv[1], sys.argv[2], sys.stdin.read())
