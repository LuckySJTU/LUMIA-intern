"""Codeup API helpers that run only on user-side machines."""

from __future__ import annotations

import json
import os
import re
import urllib.request


CODEUP_API_BASE = "https://openapi-rdc.aliyuncs.com"


def extract_codeup_repo_path(repo_url: str) -> str:
    match = re.search(r"codeup\.aliyun\.com/(.+?)(?:\.git)?$", repo_url or "")
    if match:
        return match.group(1)
    match = re.search(r"codeup\.aliyun\.com:(.+?)(?:\.git)?$", repo_url or "")
    if match:
        return match.group(1)
    return ""


def _codeup_json_get(path: str, token: str):
    req = urllib.request.Request(
        CODEUP_API_BASE + path,
        headers={"Content-Type": "application/json", "x-yunxiao-token": token},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _codeup_org_id(token: str) -> str:
    configured = (
        os.environ.get("CODEUP_ORG_ID", "").strip()
        or os.environ.get("CODEUP_ORGANIZATION_ID", "").strip()
    )
    if configured:
        return configured
    orgs = _codeup_json_get("/oapi/v1/platform/organizations", token)
    if not isinstance(orgs, list) or not orgs:
        raise RuntimeError("Codeup organization list is empty")
    return str(orgs[0].get("id") or "")


def _codeup_repository_id(token: str, org_id: str, repo_path: str) -> str:
    page = 1
    per_page = 50
    while True:
        data = _codeup_json_get(
            f"/oapi/v1/codeup/organizations/{org_id}/repositories?page={page}&perPage={per_page}",
            token,
        )
        repos = data if isinstance(data, list) else data.get("result", [])
        if not repos:
            return ""
        for repo in repos:
            path = str(repo.get("pathWithNamespace") or "")
            if path == repo_path or (repo_path and path.endswith(repo_path)):
                return str(repo.get("id") or "")
        if len(repos) < per_page:
            return ""
        page += 1


def codeup_branch_protection(repo_url: str) -> tuple[bool | None, str, str]:
    token = os.environ.get("CODEUP_ACCESS_TOKEN", "").strip()
    if not token:
        return None, "", "CODEUP_ACCESS_TOKEN is not set"
    repo_path = extract_codeup_repo_path(repo_url)
    if not repo_path:
        return None, "", "repo_url is not a Codeup URL"
    try:
        org_id = _codeup_org_id(token)
        repo_id = _codeup_repository_id(token, org_id, repo_path)
        if not repo_id:
            return None, "", f"repository id not found for {repo_path}"
        branches = _codeup_json_get(
            f"/oapi/v1/codeup/organizations/{org_id}/repositories/{repo_id}/branches",
            token,
        )
        if not isinstance(branches, list):
            return None, "", "Codeup branches response is not a list"
        default = next((item for item in branches if isinstance(item, dict) and item.get("defaultBranch")), None)
        if not default:
            default = branches[0] if branches and isinstance(branches[0], dict) else None
        if not default:
            return None, "", "Codeup branch list is empty"
        return bool(default.get("protected")), str(default.get("name") or ""), ""
    except Exception as exc:
        return None, "", str(exc)
