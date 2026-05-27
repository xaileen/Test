from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _enabled() -> bool:
    return os.environ.get("GITHUB_SYNC_ENABLED", "").lower() in ("1", "true", "yes", "y")


def _github_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def sync_file_to_github(local_path: str | Path, repo_path: str | None = None, message: str | None = None) -> bool:
    """Update one repository file through the GitHub Contents API.

    Required env when enabled:
    - GITHUB_SYNC_ENABLED=1
    - GITHUB_TOKEN or GH_TOKEN
    - GITHUB_REPO, for example xaileen/Test
    Optional:
    - GITHUB_BRANCH, default main
    """
    if not _enabled():
        return False

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPO", "").strip()
    branch = os.environ.get("GITHUB_BRANCH", "main").strip() or "main"
    api_base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    path = Path(local_path)
    if not token or not repo:
        print("[GITHUB_SYNC] skip: set GITHUB_TOKEN/GH_TOKEN and GITHUB_REPO", flush=True)
        return False
    if not path.exists():
        print(f"[GITHUB_SYNC] skip: local file not found: {path}", flush=True)
        return False

    target = (repo_path or path.name).replace("\\", "/").lstrip("/")
    contents_url = f"{api_base}/repos/{repo}/contents/{target}"
    encoded_target = target.replace(" ", "%20")
    contents_url = f"{api_base}/repos/{repo}/contents/{encoded_target}"
    file_bytes = path.read_bytes()

    sha = None
    try:
        current = _github_request("GET", f"{contents_url}?ref={branch}", token)
        sha = current.get("sha")
    except HTTPError as exc:
        if exc.code != 404:
            print(f"[GITHUB_SYNC] get failed for {target}: HTTP {exc.code}", flush=True)
            return False
    except (URLError, TimeoutError) as exc:
        print(f"[GITHUB_SYNC] get failed for {target}: {exc}", flush=True)
        return False

    payload = {
        "message": message or f"Update {target}",
        "content": base64.b64encode(file_bytes).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    try:
        _github_request("PUT", contents_url, token, payload)
    except HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode("utf-8")[:240]
        except Exception:
            pass
        print(f"[GITHUB_SYNC] put failed for {target}: HTTP {exc.code} {details}", flush=True)
        return False
    except (URLError, TimeoutError) as exc:
        print(f"[GITHUB_SYNC] put failed for {target}: {exc}", flush=True)
        return False

    print(f"[GITHUB_SYNC] updated {repo}@{branch}:{target}", flush=True)
    return True
