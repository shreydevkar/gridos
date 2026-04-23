"""GitHub connector — public repo stats as formulas.

Zero-config for public repos (no auth needed within GitHub's anonymous
rate limit of 60 req/hour). Optional GITHUB_TOKEN env var bumps that to
5000 req/hour and unlocks private-repo reads.

Exposed formulas:
  =GITHUB_STARS("user/repo")                       → star count
  =GITHUB_FORKS("user/repo")                       → fork count
  =GITHUB_OPEN_ISSUES("user/repo")                 → open issues + PRs
  =GITHUB_COMMITS_LAST_N_DAYS("user/repo", days)   → commits in the window

The "30-second plugin" showcase — four public-API metrics in ~70 lines,
meant to be the example everyone copies when writing their first plugin.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

_CACHE: dict = {}
_CACHE_TTL = 60.0

_KERNEL = None


def _get(path: str, params: dict | None = None):
    # path is the GitHub API path sans leading slash, e.g. "repos/torvalds/linux".
    cache_key = (path, tuple(sorted((params or {}).items())))
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"https://api.github.com/{path}{qs}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "GridOS-Plugin",
    }
    token = (_KERNEL.get_secret("github", "TOKEN", env_fallback="GITHUB_TOKEN").strip()
             if _KERNEL else "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"__error__": "#GITHUB_NOT_FOUND!"}
        if e.code in (401, 403):
            # 403 is usually rate-limiting with the anon key. Bump to 5000/hr
            # by setting GITHUB_TOKEN.
            return {"__error__": "#GITHUB_RATE_LIMIT!" if e.code == 403 else "#GITHUB_AUTH!"}
        return {"__error__": f"#GITHUB_{e.code}!"}
    except urllib.error.URLError:
        return {"__error__": "#GITHUB_OFFLINE!"}
    except Exception:
        return {"__error__": "#GITHUB_ERROR!"}

    _CACHE[cache_key] = (time.time(), body)
    return body


def _normalize_repo(repo):
    if not isinstance(repo, str) or "/" not in repo:
        return None
    cleaned = repo.strip().strip("/")
    # Be permissive on URLs — user may paste "https://github.com/user/repo".
    if cleaned.startswith("http"):
        cleaned = cleaned.split("github.com/", 1)[-1]
    parts = cleaned.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}/{parts[1]}"


def register(kernel):
    global _KERNEL
    _KERNEL = kernel

    @kernel.formula("GITHUB_STARS")
    def github_stars(repo):
        slug = _normalize_repo(repo)
        if not slug:
            return "#GITHUB_BAD_REPO!"
        data = _get(f"repos/{slug}")
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        return int(data.get("stargazers_count") or 0)

    @kernel.formula("GITHUB_FORKS")
    def github_forks(repo):
        slug = _normalize_repo(repo)
        if not slug:
            return "#GITHUB_BAD_REPO!"
        data = _get(f"repos/{slug}")
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        return int(data.get("forks_count") or 0)

    @kernel.formula("GITHUB_OPEN_ISSUES")
    def github_open_issues(repo):
        slug = _normalize_repo(repo)
        if not slug:
            return "#GITHUB_BAD_REPO!"
        # open_issues_count on the repo endpoint includes PRs. Not worth a
        # second /issues query to separate them — most users treat the
        # combined count as "open work on the repo".
        data = _get(f"repos/{slug}")
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        return int(data.get("open_issues_count") or 0)

    @kernel.formula("GITHUB_COMMITS_LAST_N_DAYS")
    def github_commits(repo, days_back=30):
        slug = _normalize_repo(repo)
        if not slug:
            return "#GITHUB_BAD_REPO!"
        try:
            n = int(float(days_back))
        except (TypeError, ValueError):
            n = 30
        n = max(1, min(n, 365))
        since = (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()
        data = _get(f"repos/{slug}/commits", {"since": since, "per_page": 100})
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        # The /commits endpoint is paginated but the count of the first page
        # (up to 100) is good enough for a dashboard indicator. For repos
        # pushing >100 commits in the window, users can set the window shorter
        # or grab the full history via the REST API directly.
        return len(data) if isinstance(data, list) else 0

    kernel.agent({
        "id": "github",
        "display_name": "Open-Source Analyst",
        "router_description": "GitHub repo stats, stars, forks, commits, issue counts",
        "system_prompt": (
            "You are a GitHub repo analyst. When the user asks about repo "
            "traction, activity, or health, emit formulas that use: "
            "GITHUB_STARS(\"user/repo\"), GITHUB_FORKS(\"user/repo\"), "
            "GITHUB_OPEN_ISSUES(\"user/repo\"), GITHUB_COMMITS_LAST_N_DAYS(\"user/repo\", days). "
            "Prefer formulas over hardcoded numbers so the grid stays live. "
            "Lay out labels in column B and values in column C. If the user "
            "gives multiple repos, build a comparison table one row per repo. "
            "Return a 2D 'values' array and a top-left 'target_cell'. "
            "Do NOT invent numeric values — errors surface as #GITHUB_* "
            "sentinels the user can act on."
        ),
    })
