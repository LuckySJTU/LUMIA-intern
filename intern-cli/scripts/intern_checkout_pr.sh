#!/usr/bin/env bash
# ============================================================
# intern_checkout_pr.sh — PR-aware 分支检出
#
# 用法:
#   ./intern_checkout_pr.sh <intern_name> <repo_path>
#
# 功能:
#   检查指定 intern 是否有 Open PR（分支名以 intern_name/ 开头）。
#   - 有 PR → fetch + checkout PR 分支（或 pull 如果已在该分支上）
#   - 无 PR → checkout 默认分支 + pull
#
# 退出码:
#   0 = 成功
#   1 = 参数错误或 repo 不存在
#
# 输出（stdout）:
#   最终所在的分支名
# ============================================================

set -euo pipefail

info()  { echo "[checkout-pr] $*" >&2; }
warn()  { echo "[checkout-pr] WARN: $*" >&2; }

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <intern_name> <repo_path>" >&2
    exit 1
fi

INTERN_NAME="$1"
REPO_PATH="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -d "${REPO_PATH}/.git" ]]; then
    echo "Error: ${REPO_PATH} is not a git repo" >&2
    exit 1
fi

cd "${REPO_PATH}"

# ── 查询 Open PR ──────────────────────────────────
OPEN_PR=""
_REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"

if [[ "${_REMOTE_URL}" =~ github\.com[:/]([^/]+)/([^/.]+) ]]; then
    _GH_REPO="${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    OPEN_PR="$(gh pr list --repo "${_GH_REPO}" \
        --head "${INTERN_NAME}/" --state open \
        --json number,headRefName --jq '.[0].headRefName' 2>/dev/null || true)"
elif [[ "${_REMOTE_URL}" =~ codeup\.aliyun\.com ]]; then
    _CODEUP_CLI="${SCRIPT_DIR}/../codeup_pr.py"
    if [[ -f "${_CODEUP_CLI}" ]]; then
        OPEN_PR="$(python3 "${_CODEUP_CLI}" list --state opened --head "${INTERN_NAME}/" 2>/dev/null \
            | head -1 | awk '{print $3}' || true)"
    fi
fi

# ── 分支检出 ──────────────────────────────────────
CURRENT_BRANCH="$(git branch --show-current 2>/dev/null || true)"
DEFAULT_BRANCH="$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)"
if [[ -z "${DEFAULT_BRANCH}" ]]; then
    DEFAULT_BRANCH="${CURRENT_BRANCH}"
fi
if [[ -z "${DEFAULT_BRANCH}" ]]; then
    DEFAULT_BRANCH="$(git for-each-ref --format='%(refname:short)' refs/remotes/origin 2>/dev/null \
        | sed '/^origin\/HEAD$/d; s|^origin/||' \
        | head -n 1)"
fi
if [[ -z "${DEFAULT_BRANCH}" ]]; then
    echo "Error: unable to determine default branch for ${REPO_PATH}" >&2
    exit 1
fi

if [[ -n "${OPEN_PR}" ]]; then
    info "Found open PR on branch '${OPEN_PR}'"
    if [[ "${CURRENT_BRANCH}" == "${OPEN_PR}" ]]; then
        info "Already on PR branch, pulling latest..."
        git pull origin "${OPEN_PR}" --ff-only 2>/dev/null || warn "git pull failed, using existing"
    elif [[ "${CURRENT_BRANCH}" == "${DEFAULT_BRANCH}" ]]; then
        info "On ${DEFAULT_BRANCH}, checking out PR branch '${OPEN_PR}'..."
        git fetch origin "${OPEN_PR}" 2>/dev/null || true
        git checkout "${OPEN_PR}" 2>/dev/null || warn "checkout failed, staying on ${DEFAULT_BRANCH}"
    else
        info "On branch '${CURRENT_BRANCH}' (PR branch is '${OPEN_PR}'), fetching latest..."
        git fetch origin 2>/dev/null || true
    fi
else
    info "No open PR found, checking out ${DEFAULT_BRANCH}"
    git checkout "${DEFAULT_BRANCH}" 2>/dev/null || true
    git pull origin "${DEFAULT_BRANCH}" --ff-only 2>/dev/null || warn "git pull failed, using existing"
fi

# 输出最终分支名到 stdout
git branch --show-current 2>/dev/null || echo "${DEFAULT_BRANCH}"
