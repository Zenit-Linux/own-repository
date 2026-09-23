#!/usr/bin/env python3
"""
Generuje repo/latest-versions.json na podstawie repo/own-repository.json.

Dla KAZDEGO wpisu (i kazdego wariantu w "branches", jesli obecny), ktorego
pole "bin" zawiera placeholder "{version}", ustala prawdziwa NAJNOWSZA
wersje danego repo GitHuba (GET /repos/{owner}/{repo}/releases/latest) i
zapisuje pod kluczem "owner/repo" -> "vX.Y.Z" (dokladnie ten sam format
klucza, ktorego uzywa lokalny cache klienta zpm -- zpmpkg/versioncache.nim
w repo zpm, ownerRepo = f"{owner}/{repo}").

Wpisy BEZ "{version}" w "bin" (np. "carbon" przypiete na sztywno do
konkretnego taga) i wpisy czysto typu "git" bez pola "bin" (np. top-level
"kernel", ktory jest type=git -- tylko jego "branches.stable" ma "bin" z
placeholderem) sa pomijane, bo nie ma tam niczego do "rozwiazywania".

Uruchamiane RAZ DZIENNIE z jednego, scentralizowanego miejsca (patrz
.github/workflows/generate-latest-versions.yml) -- klienci zpm na calym
swiecie czytaja juz GOTOWY wynik przez raw.githubusercontent.com (limit
inny i DUZO luzniejszy niz api.github.com), zamiast kazdy z osobna
odpytywac limitowane REST API (60 zapytan/h NA ADRES IP dla
niezalogowanych -- przy wielu uzytkownikach na tym samym IP/w tej samej
sieci wyczerpuje sie natychmiast).

Blad rozwiazania POJEDYNCZEGO repo (np. brak jeszcze opublikowanego
wydania) NIE przerywa calego joba -- lepszy czesciowy plik niz brak pliku
w ogole; klienci zpm i tak maja fallback (redirect strony WWW / REST API)
dla wpisow, ktorych tu zabraklo.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

import report_issue

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_PATH = os.path.join(REPO_ROOT, "repo", "own-repository.json")
OUTPUT_PATH = os.path.join(REPO_ROOT, "repo", "latest-versions.json")

# v0.7 -- jesli wiecej niz ten odsetek celow nie da sie rozwiazac, to NIE
# jest juz "kernel jeszcze bez wydania" (normalne, pojedyncze niepowodzenie)
# -- to wyglada na cos SYSTEMOWEGO (np. zmiana w GitHub API, zla zmienna
# srodowiskowa, padly DNS). W takim wypadku job ma się GLOSNO wywalic
# (zamiast cicho zapisac czesciowy plik) I zgłosić to jako GitHub Issue,
# bo nikt nie czyta logow crona, ktorego nikt nie uruchomil recznie.
MASS_FAILURE_THRESHOLD = 0.4

GITHUB_RELEASE_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/releases/download/\{version\}/"
)


def extract_owner_repo(bin_url: str):
    """Zwraca (owner, repo) jesli 'bin_url' to link do release'a GitHuba z
    placeholderem {version}, inaczej None (nic do rozwiazania)."""
    m = GITHUB_RELEASE_URL_RE.match(bin_url or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def collect_targets(data: dict):
    """Zbiera WSZYSTKIE (owner, repo) do rozwiazania -- z gornego poziomu
    kazdego narzedzia ORAZ z kazdego wariantu w jego ew. 'branches'."""
    targets = set()
    for tool in data.get("tools", []):
        top = extract_owner_repo(tool.get("bin", ""))
        if top:
            targets.add(top)
        for variant in (tool.get("branches") or {}).values():
            v = extract_owner_repo(variant.get("bin", ""))
            if v:
                targets.add(v)
    return sorted(targets)


def fetch_latest_tag(owner: str, repo: str, token: str):
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "zenit-own-repository-generator",
            "Accept": "application/vnd.github+json",
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("tag_name") or None
    except urllib.error.HTTPError as e:
        print(f"  [pomijam] {owner}/{repo}: HTTP {e.code} ({e.reason})", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  [pomijam] {owner}/{repo}: {e.reason}", file=sys.stderr)
        return None


def main():
    with open(SOURCE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    targets = collect_targets(data)
    print(f"Znaleziono {len(targets)} repo(z) do rozwiazania z {SOURCE_PATH}:")
    for owner, repo in targets:
        print(f"  - {owner}/{repo}")

    # GITHUB_TOKEN wbudowany w kazdy job GitHub Actions -- daje limit
    # 1000/h, ogromny zapas dla ~kilkunastu zapytan raz dziennie. Nie
    # wymaga zadnego dodatkowego sekretu do skonfigurowania.
    token = os.environ.get("GITHUB_TOKEN", "")

    result = {}
    failed = []
    for owner, repo in targets:
        tag = fetch_latest_tag(owner, repo, token)
        if tag:
            result[f"{owner}/{repo}"] = tag
            print(f"  OK {owner}/{repo} -> {tag}")
        else:
            failed.append(f"{owner}/{repo}")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"\nZapisano {len(result)}/{len(targets)} wpisow do {OUTPUT_PATH}")
    if failed:
        print(f"Nie udalo sie rozwiazac ({len(failed)}): {', '.join(failed)}", file=sys.stderr)

    if targets and (len(failed) / len(targets)) > MASS_FAILURE_THRESHOLD:
        title = "Masowe niepowodzenie generate-latest-versions"
        body = (
            f"Nie udało się rozwiązać {len(failed)}/{len(targets)} repo "
            f"(próg alarmu: {int(MASS_FAILURE_THRESHOLD * 100)}%).\n\n"
            f"Nieudane: {', '.join(failed)}\n\n"
            "To wygląda na coś systemowego (np. zmiana w GitHub API, problem sieciowy "
            "w runnerze, wygasły/odwołany token), nie na pojedyncze repo bez wydania. "
            f"Zobacz log: {os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
        )
        report_issue.report("generate-latest-versions", title, body)
        print(
            f"\nBLAD: {len(failed)}/{len(targets)} ({len(failed) / len(targets):.0%}) przekracza "
            f"próg masowego niepowodzenia ({MASS_FAILURE_THRESHOLD:.0%}) -- kończę z błędem.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Pojedyncze/nieliczne niepowodzenia (np. "installer" bez jeszcze
    # opublikowanego wydania) NIE kończą calego joba błędem -- częściowy
    # plik jest lepszy niż brak pliku w ogóle.


if __name__ == "__main__":
    main()
