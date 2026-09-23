#!/usr/bin/env python3
"""
Waliduje wpisy w own-repository.json, ktore zostaly DODANE lub ZMIENIONE
w tym PR/pushu (diff wobec bazowego commita/brancha) -- nie testuje calego
pliku za kazdym razem, tylko to, co sie realnie zmienilo (albo konkretne
narzedzie podane recznie przez ONLY_NAMES, patrz test.yml).

Dla kazdego zmienionego/nowego narzedzia (i kazdego wariantu w "branches"):
  - jesli ma pole "bin" z {version}: ustala PRAWDZIWA najnowsza wersje
    przez redirect strony wydan (dokladnie tak samo jak klient zpm --
    zeby NIE zuzywac limitu api.github.com w samym CI walidacyjnym) i
    sprawdza, ze URL po podstawieniu tej wersji FAKTYCZNIE ISTNIEJE
    (HEAD -> 200) -- lapie literowki w nazwie pliku/rozszerzeniu assetu,
    zle nazwy repo/organizacji (np. dokladnie taki blad jak
    "Zenith-Linux" zamiast "Zenit-Linux", ktory kiedys przeszedl bez
    zauwazenia) zanim trafia na produkcje.
  - jesli ma "type": "git": sprawdza `git ls-remote`, ze podana
    galaz/tag ("ref") FAKTYCZNIE istnieje w podanym repo.

Blad = kod wyjscia 1 (blokuje merge PR-a, jesli podpiete jako required
check) i czytelna lista problemow na stderr.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

import report_issue

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_PATH = os.path.join(REPO_ROOT, "repo", "own-repository.json")

GITHUB_RELEASE_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/releases/download/\{version\}/(.+)$"
)


def load_json_at_ref(ref: str):
    """Zwraca zawartosc own-repository.json z podanego ref-a (git show),
    albo None jesli plik tam nie istnial (np. pierwszy commit repo)."""
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:repo/own-repository.json"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        return json.loads(out.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def tools_by_name(data):
    return {t["name"]: t for t in (data or {}).get("tools", [])}


def changed_tool_names(base_ref: str, head_data: dict):
    base_tools = tools_by_name(load_json_at_ref(base_ref))
    head_tools = tools_by_name(head_data)
    changed = []
    for name, tool in head_tools.items():
        if name not in base_tools or base_tools[name] != tool:
            changed.append(name)
    return changed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # nie podazaj za przekierowaniem -- chcemy zobaczyc samo Location


def resolve_latest_tag_via_redirect(owner: str, repo: str):
    """Tak samo jak klient zpm (ownrepo.nim, resolveLatestTagViaRedirect) --
    HEAD na releases/latest, czytamy Location z odpowiedzi 30x, BEZ
    dotykania limitowanego api.github.com. Zwraca None, jesli sie nie uda
    (np. repo bez zadnego wydania, albo prywatne)."""
    url = f"https://github.com/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "zenit-own-repository-test"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(req, timeout=15)
        return None  # brak przekierowania = nie to, czego oczekiwalismy
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location", "")
            m = re.search(r"/releases/tag/([^/?]+)", loc)
            if m:
                return m.group(1)
        return None
    except urllib.error.URLError:
        return None


def url_exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "zenit-own-repository-test"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False


def git_ref_exists(repo_url: str, ref: str) -> bool:
    if not repo_url or not ref:
        return False
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--heads", "--tags", repo_url, ref],
            capture_output=True, text=True, timeout=20,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def check_bin_entry(label: str, bin_url: str, errors: list):
    if not bin_url:
        errors.append(f"{label}: puste pole 'bin'")
        return

    if "{version}" not in bin_url:
        # URL przypięty na sztywno do konkretnego wydania (np. "carbon" ->
        # v0.0.0-trunk) -- nie ma czego "rozwiązywać", sprawdzamy tylko,
        # że taki URL, jaki jest, FAKTYCZNIE istnieje.
        if url_exists(bin_url):
            print(f"  OK {label}: {bin_url} (przypięty na sztywno, bez {{version}})")
        else:
            errors.append(f"{label}: URL przypięty na sztywno (bez {{version}}) NIE ISTNIEJE: {bin_url}")
        return

    m = GITHUB_RELEASE_URL_RE.match(bin_url)
    if not m:
        errors.append(
            f"{label}: pole 'bin' zawiera {{version}}, ale nie pasuje do oczekiwanego formatu "
            f"URL-a wydania GitHuba (https://github.com/OWNER/REPO/releases/download/{{version}}/PLIK): {bin_url!r}"
        )
        return
    owner, repo, _asset = m.groups()
    tag = resolve_latest_tag_via_redirect(owner, repo)
    if not tag:
        errors.append(
            f"{label}: nie udało się ustalić najnowszej wersji dla {owner}/{repo} "
            "(brak opublikowanego wydania? zła nazwa repo/organizacji?)"
        )
        return
    real_url = bin_url.replace("{version}", tag)
    if not url_exists(real_url):
        errors.append(
            f"{label}: URL po podstawieniu najnowszej wersji ({tag}) NIE ISTNIEJE: {real_url} "
            "(sprawdź nazwę pliku/rozszerzenie assetu w tym wydaniu)"
        )
    else:
        print(f"  OK {label}: {real_url}")


def check_git_entry(label: str, repo_url: str, ref: str, errors: list):
    if not git_ref_exists(repo_url, ref):
        errors.append(f"{label}: gałąź/tag '{ref}' nie istnieje w {repo_url} (albo repo nie istnieje)")
    else:
        print(f"  OK {label}: {repo_url}@{ref}")


def check_tool(name: str, tool: dict, errors: list):
    if "bin" in tool:
        check_bin_entry(name, tool["bin"], errors)
    elif tool.get("type") == "git":
        check_git_entry(name, tool.get("repo", ""), tool.get("ref", ""), errors)
    else:
        errors.append(f"'{name}': brak pola 'bin' i type != \"git\" -- nie wiadomo jak to zwalidować")

    for branch_name, variant in (tool.get("branches") or {}).items():
        label = f"{name}.branches.{branch_name}"
        if "bin" in variant:
            check_bin_entry(label, variant["bin"], errors)
        elif variant.get("type") == "git":
            check_git_entry(label, variant.get("repo", tool.get("repo", "")), variant.get("ref", ""), errors)
        else:
            errors.append(f"{label}: brak pola 'bin' i type != \"git\" -- nie wiadomo jak to zwalidować")


def main():
    with open(SOURCE_PATH, encoding="utf-8") as f:
        head_data = json.load(f)

    full_scan = os.environ.get("FULL_SCAN", "").strip() == "1"
    base_ref = os.environ.get("BASE_REF", "").strip()
    only_names_raw = os.environ.get("ONLY_NAMES", "").strip()
    tools = tools_by_name(head_data)

    if full_scan:
        # v0.7 -- okresowy PEŁNY skan (patrz .github/workflows/full-scan.yml,
        # cron cotygodniowy): sprawdza WSZYSTKIE wpisy, nie tylko te
        # zmienione w ostatnim PR/pushu. Pakiet poprawny w momencie
        # mergowania mógł z czasem "zgnić" (usunięty release, zmieniona
        # nazwa repo) -- diff temu nie zapobiegnie, bo own-repository.json
        # się wtedy nie zmienia.
        names = sorted(tools.keys())
        print(f"PEŁNY SKAN: sprawdzam wszystkie {len(names)} narzędzia w own-repository.json.")
    elif only_names_raw:
        names = [n.strip() for n in only_names_raw.split(",") if n.strip()]
        print(f"Testuję ręcznie podane narzędzie(a): {', '.join(names)}")
    elif base_ref:
        names = changed_tool_names(base_ref, head_data)
        if not names:
            print("Brak zmienionych/nowych narzędzi w own-repository.json -- nic do przetestowania.")
            return
        print(f"Wykryto {len(names)} zmienione/nowe narzędzie(a) (diff vs {base_ref}): {', '.join(names)}")
    else:
        print("Brak BASE_REF i brak ONLY_NAMES -- nic do przetestowania (sprawdź konfigurację workflow).")
        return

    errors = []
    for name in names:
        tool = tools.get(name)
        if tool is None:
            errors.append(f"'{name}': nie znaleziono w bieżącym own-repository.json")
            continue
        check_tool(name, tool, errors)

    if errors and full_scan:
        # W trybie diffa (PR) sam status faila w GitHubie wystarczy -- ktoś
        # patrzy na ten konkretny PR. W trybie pełnego skanu (cron, nikt
        # ręcznie nie patrzy) trzeba to GŁOŚNO zgłosić, żeby ktokolwiek się
        # dowiedział.
        title = "Pełny skan own-repository.json: znaleziono zepsute wpisy"
        body = (
            f"Cotygodniowy pełny skan znalazł {len(errors)} problem(ów) w "
            f"{len(names)} sprawdzonych narzędziach:\n\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n\n"
            f"Zobacz log: {os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
        )
        report_issue.report("full-scan", title, body)

    if errors:
        print("\nBŁĘDY:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    print("\nWszystkie sprawdzone wpisy są poprawne.")


if __name__ == "__main__":
    main()
