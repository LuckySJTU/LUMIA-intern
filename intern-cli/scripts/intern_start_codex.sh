#!/usr/bin/env bash
# ============================================================
# intern_start_codex.sh — 启动（或附着到）一个 intern 的 Codex CLI session
#
# 用法:
#   ./intern_start_codex.sh <intern_name> [project_name]
#
# 与 intern_start.sh（Claude 版）的差异：
#   - 启动 `codex` 而非 `claude`
#   - 跳过权限：`codex --dangerously-bypass-approvals-and-sandbox`（别名 --yolo）
#   - 配置：~/.codex/config.toml（用户级）+ <intern_dir>/.codex/config.toml（项目级 symlink）
#   - 项目 trust：必须在 ~/.codex/config.toml 中写 [projects."<intern_dir>"] trust_level="trusted"
#                 否则 intern 的项目级 hooks 配置不会被加载
#   - intern_sessions.json 中 type 写 'codex'
#   - 不写 ~/.claude.json hasCompletedOnboarding（Codex 无此概念）
# ============================================================

set -euo pipefail

# ── 常量 ──────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
WORK_ROOT="${WORK_AGENTS_ROOT:-/work-agents}"
SHARED_REPO="${INTERN_SHARED_REPO:-${REPO_ROOT}}"
HOOKS_DIR="${SHARED_REPO}/vscode-extension/hooks"
PROJECT_NAME="${2:-axis_intern_agents}"

# Ensure ~/.local/bin is in PATH (codex CLI may install there)
[[ ":${PATH}:" == *":${HOME}/.local/bin:"* ]] || export PATH="${HOME}/.local/bin:${PATH}"

# task205: 解析 Python 3 解释器 — 支持 conda-only 环境
_resolve_python() {
    if [[ -n "${PYTHON:-}" ]] && command -v "${PYTHON}" >/dev/null 2>&1; then
        echo "${PYTHON}"; return 0
    fi
    if [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/python" ]]; then
        echo "${CONDA_PREFIX}/bin/python"; return 0
    fi
    command -v python3 && return 0
    if command -v python >/dev/null 2>&1; then
        local ver
        ver="$(python --version 2>&1 | awk '{print $2}' | cut -d. -f1)"
        [[ "${ver}" == "3" ]] && { command -v python; return 0; }
    fi
    return 1
}
PYTHON="$(_resolve_python)" || { echo "[ERROR] No Python 3 interpreter found. Install python3 or activate a conda env first." >&2; exit 1; }
export PYTHON

_default_daemon_addr_file() {
    "${PYTHON}" - "${WORK_ROOT}" <<'PY'
import hashlib
import os
import sys

root = os.path.abspath(sys.argv[1])
uid = os.getuid() if hasattr(os, "getuid") else 0
digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:12]
print(f"/tmp/feishu_daemon_{uid}_{digest}.json")
PY
}

if [[ -z "${FEISHU_DAEMON_ADDR_FILE:-}" ]]; then
    export FEISHU_DAEMON_ADDR_FILE="$(_default_daemon_addr_file)"
fi

# ── 颜色 ──────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

git_default_branch() {
    local repo="$1"
    local branch=""

    branch="$(git -C "${repo}" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    if [[ -n "${branch}" ]]; then
        echo "${branch#origin/}"
        return 0
    fi

    branch="$(git -C "${repo}" branch --show-current 2>/dev/null || true)"
    if [[ -n "${branch}" ]]; then
        echo "${branch}"
        return 0
    fi

    branch="$(git -C "${repo}" for-each-ref --format='%(refname:short)' refs/remotes/origin 2>/dev/null \
        | sed '/^origin\/HEAD$/d; s|^origin/||' \
        | head -n 1)"
    if [[ -n "${branch}" ]]; then
        echo "${branch}"
        return 0
    fi

    return 1
}

refresh_outer_repo() {
    local repo="$1"
    local branch=""
    local current=""

    branch="$(git_default_branch "${repo}")" || return 1
    current="$(git -C "${repo}" branch --show-current 2>/dev/null || true)"
    if [[ -n "${current}" && "${current}" != "${branch}" ]]; then
        git -C "${repo}" checkout "${branch}" >/dev/null
    fi
    git -C "${repo}" pull --rebase --autostash origin "${branch}"
}

ensure_feishu_group() {
    local intern_name="$1"
    local intern_type="$2"
    local project_name="${3:-}"
    local workspace_id="${4:-}"
    local daemon_addr="${FEISHU_DAEMON_ADDR_FILE}"

    if [[ ! -f "${daemon_addr}" ]]; then
        die "Feishu daemon address file not found: ${daemon_addr}"
    fi

    "${PYTHON}" - "${daemon_addr}" "${intern_name}" "${intern_type}" "${project_name}" "${workspace_id}" <<'PYEOF'
import json
import socket
import sys
import time
import urllib.error
import urllib.request

GROUP_CREATE_TIMEOUT_SECONDS = 60
GROUP_CREATE_ATTEMPTS = 2

addr_path, intern_name, intern_type, project_name, workspace_id = sys.argv[1:6]
try:
    with open(addr_path, "r", encoding="utf-8") as f:
        addr = json.load(f)
    port = int(addr["http_port"])
except Exception as exc:
    print(f"invalid daemon address file {addr_path}: {exc}", file=sys.stderr)
    sys.exit(1)

payload_data = {"intern_name": intern_name, "type": intern_type}
if project_name:
    payload_data["project"] = project_name
if workspace_id:
    payload_data["workspace_id"] = workspace_id
payload = json.dumps(payload_data).encode("utf-8")
url = f"http://127.0.0.1:{port}/api/group/create"

result = None
for attempt in range(1, GROUP_CREATE_ATTEMPTS + 1):
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GROUP_CREATE_TIMEOUT_SECONDS) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
            break
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"daemon /api/group/create HTTP {exc.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        if attempt >= GROUP_CREATE_ATTEMPTS:
            print(f"daemon /api/group/create failed after {GROUP_CREATE_ATTEMPTS} attempts: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"daemon /api/group/create timed out, retrying ({attempt + 1}/{GROUP_CREATE_ATTEMPTS})", file=sys.stderr)
        time.sleep(2)
    except Exception as exc:
        print(f"daemon /api/group/create failed: {exc}", file=sys.stderr)
        sys.exit(1)

chat_id = result.get("chat_id")
if not chat_id:
    print(f"daemon /api/group/create returned no chat_id: {result}", file=sys.stderr)
    sys.exit(1)

print(chat_id)
PYEOF
}

is_root_user() {
    [[ "$(id -u)" -eq 0 ]]
}

is_container_environment() {
    if [[ "${IS_SANDBOX:-}" == "1" ]]; then
        return 0
    fi

    if command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt -c >/dev/null 2>&1; then
        return 0
    fi

    if [[ -f "/.dockerenv" || -f "/run/.containerenv" ]]; then
        return 0
    fi

    if [[ -n "${container:-}" || -n "${KUBERNETES_SERVICE_HOST:-}" ]]; then
        return 0
    fi

    grep -qaE '(docker|containerd|kubepods|podman|lxc)' /proc/1/cgroup 2>/dev/null
}

# bypass approval+sandbox 是高危 flag（OpenAI CLI 文档明确警告）。
# 与 Claude 的 --permission-mode bypassPermissions 同等级别 — 仅在 root + container 场景启用，
# 其他场景（非 root 或非容器）保留 codex 默认权限提示，避免在主管开发机上误删文件。
should_enable_root_bypass() {
    is_root_user && is_container_environment
}

session_has_live_process() {
    local session_name="$1"
    local current_command=""

    current_command="$(tmux list-panes -t "=${session_name}" -F '#{pane_current_command}' 2>/dev/null | head -n 1 | tr '[:upper:]' '[:lower:]' | tr -d '\r')"

    case "${current_command}" in
        ""|bash|sh|zsh|fish|tmux)
            return 1
            ;;
        *)
            return 0
            ;;
    esac
}

get_tmux_env_value() {
    local session_name="$1"
    local var_name="$2"
    tmux show-environment -t "=${session_name}" 2>/dev/null | sed -n "s/^${var_name}=//p" | tail -n 1
}

wait_for_codex_prompt() {
    local session_name="$1"
    local timeout_seconds="${2:-30}"
    local deadline=$((SECONDS + timeout_seconds))
    local capture=""

    while (( SECONDS < deadline )); do
        capture="$(tmux capture-pane -p -J -t "=${session_name}:" -S -80 2>/dev/null || true)"
        if grep -q "› " <<<"${capture}" && grep -qi "codex" <<<"${capture}"; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

wait_for_live_process() {
    local session_name="$1"
    local timeout_seconds="${2:-20}"
    local deadline=$((SECONDS + timeout_seconds))

    while (( SECONDS < deadline )); do
        if session_has_live_process "${session_name}"; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

codex_auth_mode() {
    "${PYTHON}" - <<'PYEOF'
import json
import os

auth_path = os.path.expanduser("~/.codex/auth.json")
try:
    with open(auth_path, "r", encoding="utf-8") as f:
        print(json.load(f).get("auth_mode") or "")
except Exception:
    print("")
PYEOF
}

resolve_codex_command() {
    local hook_feature_arg=""
    local features=""
    local codex_runtime="codex"
    features="$(codex features list 2>/dev/null || true)"
    if grep -Eq '^hooks[[:space:]]' <<< "${features}"; then
        hook_feature_arg="--enable hooks"
    elif grep -Eq '^codex_hooks[[:space:]]' <<< "${features}"; then
        hook_feature_arg="--enable codex_hooks"
    fi
    if [[ -n "${CODEX_PROFILE:-}" ]]; then
        codex_runtime="codex --profile ${CODEX_PROFILE}"
    fi

    # --dangerously-bypass-approvals-and-sandbox（别名 --yolo）整体放开权限+沙箱。
    # 与 Claude 对齐：仅在 root + container 时启用 bypass；其他场景使用默认 codex（带权限提示）。
    # OpenAI Codex CLI 文档：https://developers.openai.com/codex/cli/reference 中该 flag 标注为 dangerously。
    if should_enable_root_bypass; then
        echo "${codex_runtime} ${hook_feature_arg} --dangerously-bypass-approvals-and-sandbox"
        return
    fi
    if is_root_user; then
        warn "Detected root without sandbox/container markers; running plain 'codex' (approvals required for write/exec)." >&2
        echo "${codex_runtime} ${hook_feature_arg}"
        return
    fi
    # 非 root 用户：仍允许 bypass（与 Claude 的非 root 行为一致 — 用户对自己 home 的内容负责）
    echo "${codex_runtime} ${hook_feature_arg} --dangerously-bypass-approvals-and-sandbox"
}

# ── 参数检查 ──────────────────────────────────
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <intern_name> [project_name]"
    echo ""
    echo "  启动（或附着到）一个 intern 的 Codex CLI session。"
    echo ""
    echo "  project_name 默认为 axis_intern_agents。"
    echo "  可通过 PROJECT_REPO_URL 环境变量指定 repo URL。"
    exit 1
fi

INTERN_NAME="$1"

if ! [[ "$INTERN_NAME" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    die "Invalid intern name: '$INTERN_NAME' (must be [a-zA-Z0-9_-]+)"
fi

INTERN_DIR="${INTERN_DIR:-${WORK_ROOT}/${INTERN_NAME}}"
INTERN_REPO="${INTERN_CODE_REPO_PATH:-${INTERN_DIR}/${PROJECT_NAME}}"
INTERN_WS="${INTERN_METADATA_INTERN_DIR:-${INTERN_REPO}/workspace/interns/${INTERN_NAME}}"

# ============================================================
# Step 1: 检查 intern 在 repo 中是否存在
# ============================================================
info "Step 1: 检查 intern '${INTERN_NAME}' 是否存在于 repo..."

if [[ ! -d "${SHARED_REPO}" ]]; then
    die "Shared repo not found: ${SHARED_REPO}"
fi

MASTER_INTERN_DIR="${INTERN_METADATA_INTERN_DIR:-${WORK_ROOT}/${PROJECT_NAME}/workspace/interns/${INTERN_NAME}}"
# 找不到时主动拉取默认分支再 retry，避免跨窗口创建后本地 outer repo 尚未刷新的 race。
if [[ ! -d "${MASTER_INTERN_DIR}" && -z "${INTERN_METADATA_INTERN_DIR:-}" ]]; then
    info "Intern dir not found, pulling outer repo default branch to refresh..."
    OUTER_REPO="${WORK_ROOT}/${PROJECT_NAME}"
    if [[ -d "${OUTER_REPO}/.git" ]]; then
        refresh_outer_repo "${OUTER_REPO}" || warn "git pull --rebase failed; using current state"
    fi
fi
if [[ ! -d "${MASTER_INTERN_DIR}" ]]; then
    die "Intern '${INTERN_NAME}' not found in project '${PROJECT_NAME}' (missing ${MASTER_INTERN_DIR})"
fi

if [[ ! -f "${MASTER_INTERN_DIR}/status.md" ]]; then
    die "Intern '${INTERN_NAME}' has no status.md (${MASTER_INTERN_DIR}/status.md)"
fi

ok "Intern '${INTERN_NAME}' found in repo."

# ============================================================
# Step 2: 检查 tmux session 是否存在
# ============================================================
info "Step 2: 检查 tmux session '${INTERN_NAME}'..."

SESSION_EXISTS=0
PROCESS_RUNNING=0

if tmux has-session -t "=${INTERN_NAME}" 2>/dev/null; then
    SESSION_EXISTS=1
    if session_has_live_process "${INTERN_NAME}"; then
        PROCESS_RUNNING=1
        info "tmux session '${INTERN_NAME}' exists and Codex is running. Checking runtime config..."
    fi
    if [[ "${PROCESS_RUNNING}" -eq 0 ]]; then
        warn "tmux session '${INTERN_NAME}' exists, but Codex is not running. Reusing the session..."
    fi
else
    info "tmux session '${INTERN_NAME}' not found. Creating new session..."
fi

# ============================================================
# Step 3: 初始化 intern 工作目录
# ============================================================
info "Step 3: 初始化 intern 工作目录 ${INTERN_DIR}..."

mkdir -p "${INTERN_DIR}"
mkdir -p "${INTERN_DIR}/debug"
mkdir -p "${INTERN_DIR}/outputs"
mkdir -p "${INTERN_DIR}/llm_intern_logs"

if [[ -n "${INTERN_CODE_REPO_PATH:-}" ]]; then
    if [[ ! -d "${INTERN_REPO}" ]]; then
        die "Workspace code repo not found: ${INTERN_REPO}"
    fi
    info "  Using workspace code repo: ${INTERN_REPO}"
elif [[ -d "${INTERN_REPO}/.git" ]]; then
    info "  Repo already exists, running PR-aware checkout..."
    bash "${SCRIPT_DIR}/intern_checkout_pr.sh" "${INTERN_NAME}" "${INTERN_REPO}"
    # task208: 已存在 repo 也要补齐 submodule（新挂的 submodule 在老 worktree 下是空的）
    if [[ -f "${INTERN_REPO}/.gitmodules" ]]; then
        info "  Updating submodules..."
        (cd "${INTERN_REPO}" && git submodule update --init --recursive)
    fi
else
    info "  Cloning repo..."
    _CLONE_URL="${PROJECT_REPO_URL:-}"
    if [[ -z "${_CLONE_URL}" ]]; then
        _OUTER_REPO="${WORK_ROOT}/${PROJECT_NAME}"
        if [[ -d "${_OUTER_REPO}/.git" ]]; then
            _CLONE_URL="$(git -C "${_OUTER_REPO}" remote get-url origin 2>/dev/null || true)"
        fi
    fi
    if [[ -z "${_CLONE_URL}" ]]; then
        die "Cannot determine repo URL for '${PROJECT_NAME}'. Set PROJECT_REPO_URL or ensure shared repo exists at ${WORK_ROOT}/${PROJECT_NAME}"
    fi
    # task208: --recurse-submodules 在 clone 时一并 init + update 所有 submodule（递归）
    git clone --recurse-submodules "${_CLONE_URL}" "${INTERN_REPO}"
    cd "${INTERN_REPO}"
fi

ok "Repo ready at ${INTERN_REPO}"

# 写入 .hook_state.json（hooks 依赖 project 字段）
_HOOK_STATE="${INTERN_DIR}/.hook_state.json"
if [[ ! -f "${_HOOK_STATE}" ]] || ! "${PYTHON}" -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('project') else 1)" "${_HOOK_STATE}" 2>/dev/null; then
    "${PYTHON}" -c "
import json, sys, os
p = sys.argv[1]; proj = sys.argv[2]
d = {}
if os.path.exists(p):
    try: d = json.load(open(p))
    except: pass
d['project'] = proj
with open(p + '.tmp', 'w') as f: json.dump(d, f, ensure_ascii=False, indent=2)
os.rename(p + '.tmp', p)
" "${_HOOK_STATE}" "${PROJECT_NAME}"
    info "  wrote .hook_state.json (project=${PROJECT_NAME})"
fi

# ============================================================
# Step 3.4: 确保飞书群存在
# ============================================================
info "Step 3.4: 确保飞书群存在..."
if [[ "${INTERN_START_SKIP_GROUP_CREATE:-0}" == "1" ]]; then
    CHAT_ID="${FEISHU_CHAT_ID:-}"
    info "Skipping Feishu group creation by request."
else
    CHAT_ID="$(ensure_feishu_group "${INTERN_NAME}" "codex" "${PROJECT_NAME}" "${INTERN_WORKSPACE_ID:-}")"
fi
ok "Feishu group ready: ${CHAT_ID:-skipped}"

# ============================================================
# Step 3.5: 用户级 ~/.codex/config.toml 配置项目 trust
# ============================================================
# Codex 项目级 .codex/config.toml 仅在项目被 trusted 时加载（包括 hooks）。
# trust grant 必须写在用户级 ~/.codex/config.toml 中：
#   [projects."<absolute-intern-dir>"]
#   trust_level = "trusted"
USER_CODEX_DIR="${HOME}/.codex"
USER_CODEX_CONFIG="${USER_CODEX_DIR}/config.toml"
mkdir -p "${USER_CODEX_DIR}"

info "Step 3.5: 在 ~/.codex/config.toml 中授信 intern 工作目录..."
"${PYTHON}" - "${USER_CODEX_CONFIG}" "${INTERN_DIR}" <<'PYEOF'
"""幂等地写入 Codex user config。

策略：先用 regex 删掉任何形态的旧 entry（含 header 和 trust_level 同行的 malformed 写法），
再追加一段干净的。避免被原文件状态污染。Python 标准库 tomllib 只读不写，不引入 tomli_w 依赖。
"""
import sys, os, re

config_path, intern_dir = sys.argv[1], sys.argv[2]
escaped_dir = re.escape(intern_dir)

text = ''
if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        text = f.read()

def upsert_top_level_bool(src, key, value):
    lines = src.splitlines(keepends=True)
    first_table = next((i for i, line in enumerate(lines) if line.lstrip().startswith('[')), len(lines))
    pattern = re.compile(r'^(\s*)' + re.escape(key) + r'\s*=')
    value_line = f'{key} = {str(value).lower()}\n'
    for i in range(first_table):
        if pattern.match(lines[i]):
            lines[i] = value_line
            return ''.join(lines)
    lines.insert(first_table, value_line)
    return ''.join(lines)

text = upsert_top_level_bool(text, 'suppress_unstable_features_warning', True)

# 删除任何形态（well-formed / inline header+key / header-only）的本 intern 旧 entry
pattern = re.compile(
    r'\n*\[projects\."' + escaped_dir + r'"\][^\n]*\n?'   # header 行（可含 inline trust_level）
    r'(?:[ \t]*trust_level\s*=\s*"[^"]*"\n?)*',           # 后续独立 trust_level 行
)
text = pattern.sub('\n', text)

# 追加干净 section（保留尾部空行作为 section 间分隔）
if text.strip():
    text = text.rstrip() + '\n\n'
else:
    text = ''
text += f'[projects."{intern_dir}"]\ntrust_level = "trusted"\n'

with open(config_path + '.tmp', 'w', encoding='utf-8') as f:
    f.write(text)
os.rename(config_path + '.tmp', config_path)
print('trust granted for ' + intern_dir)
print('suppressed unstable feature warning')
PYEOF
ok "User-level codex trust configured at ${USER_CODEX_CONFIG}"

# ============================================================
# Step 4: 确保 .codex/config.toml symlink 到共享模板
# ============================================================
info "Step 4: 确保 Codex 项目级 hooks 配置存在..."

CODEX_DIR="${INTERN_DIR}/.codex"
mkdir -p "${CODEX_DIR}"

CODEX_SETTINGS_TEMPLATE="${WORK_ROOT}/.github/codex_settings.toml"
if [[ ! -f "${CODEX_SETTINGS_TEMPLATE}" ]]; then
    die "Codex settings template not found: ${CODEX_SETTINGS_TEMPLATE}"
fi

CODEX_LB_ENABLED=0
CODEX_LB_API_KEY="${CODEX_LB_API_KEY:-${LB_API_KEY:-}}"
if grep -Eq '^[[:space:]]*model_provider[[:space:]]*=[[:space:]]*"lb"' "${USER_CODEX_CONFIG}" \
    && grep -Eq '^[[:space:]]*env_key[[:space:]]*=[[:space:]]*"LB_API_KEY"' "${USER_CODEX_CONFIG}"; then
    if [[ -n "${CODEX_LB_API_KEY}" ]]; then
        CODEX_LB_ENABLED=1
        export LB_API_KEY="${CODEX_LB_API_KEY}"
        info "Codex load balance enabled; LB_API_KEY injected from local environment."
    else
        warn "Codex config selects LB_API_KEY provider, but CODEX_LB_API_KEY/LB_API_KEY is not set. Codex will rely on its configured auth."
    fi
fi

# 替换旧文件为 symlink
if [[ -f "${CODEX_DIR}/config.toml" && ! -L "${CODEX_DIR}/config.toml" ]]; then
    warn "  Replacing plain config.toml with symlink to template..."
    rm -f "${CODEX_DIR}/config.toml"
fi
ln -sf "${CODEX_SETTINGS_TEMPLATE}" "${CODEX_DIR}/config.toml"

ok "Hooks config ready at ${CODEX_DIR}/config.toml"

# ============================================================
# Step 4.5: 同步 Codex hook review/trust state
# ============================================================
info "Step 4.5: 同步 Codex hook trust state..."
TRUST_STATE_CHANGED=0
TRUST_SYNC_OUTPUT="$("${PYTHON}" "${SCRIPT_DIR}/codex_trust_hooks.py" --config "${USER_CODEX_CONFIG}" --intern-dir "${INTERN_DIR}" --work-root "${WORK_ROOT}" 2>&1)" || die "Codex hook trust sync failed: ${TRUST_SYNC_OUTPUT}"
while IFS= read -r line; do
    [[ -n "${line}" ]] && info "  ${line}"
done <<< "${TRUST_SYNC_OUTPUT}"
if [[ "${TRUST_SYNC_OUTPUT}" == *"changed=1"* ]]; then
    TRUST_STATE_CHANGED=1
fi

# ============================================================
# Step 4.6: 注册 intern 类型为 codex（.intern_sessions.json）
# ============================================================
info "Step 4.6: 注册 intern 类型为 codex..."

"${PYTHON}" -c "
import json, os, fcntl
map_file = '${WORK_ROOT}/.intern_sessions.json'
lock_file = '${WORK_ROOT}/.intern_sessions.lock'
fd = open(lock_file, 'w')
fcntl.flock(fd, fcntl.LOCK_EX)
try:
    data = json.load(open(map_file)) if os.path.exists(map_file) else {}
    key = os.environ.get('INTERN_SESSION_REGISTRY_KEY') or '${INTERN_NAME}'
    entry = data.get(key, {})
    if not isinstance(entry, dict):
        entry = {}
    entry['type'] = 'codex'
    entry['intern_name'] = '${INTERN_NAME}'
    entry['project'] = '${PROJECT_NAME}'
    if os.environ.get('INTERN_DIR'):
        entry['intern_dir'] = os.environ['INTERN_DIR']
    if os.environ.get('INTERN_WORKSPACE_ID'):
        entry['workspace_id'] = os.environ['INTERN_WORKSPACE_ID']
    data[key] = entry
    tmp = map_file + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.rename(tmp, map_file)
finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()
"

ok "Registered type=codex for ${INTERN_NAME}"

SETTINGS_MTIME="$(stat -c %Y "${CODEX_DIR}/config.toml" 2>/dev/null || echo 0)"
CODEX_PROFILE_FILE="${CODEX_DIR}/profile"
ACTIVE_CODEX_PROFILE="${CODEX_PROFILE:-}"
if [[ -z "${ACTIVE_CODEX_PROFILE}" && -f "${CODEX_PROFILE_FILE}" ]]; then
    ACTIVE_CODEX_PROFILE="$(tr -d '[:space:]' < "${CODEX_PROFILE_FILE}")"
fi
if [[ -n "${ACTIVE_CODEX_PROFILE}" ]]; then
    if ! [[ "${ACTIVE_CODEX_PROFILE}" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
        die "Invalid Codex profile '${ACTIVE_CODEX_PROFILE}' in ${CODEX_PROFILE_FILE}"
    fi
    export CODEX_PROFILE="${ACTIVE_CODEX_PROFILE}"
fi

INTERN_STATUS_PAYLOAD="${INTERN_DIR}/.feishu_intern_status.json"
"${PYTHON}" - "${INTERN_STATUS_PAYLOAD}" "${INTERN_NAME}" "${PROJECT_NAME}" <<'PY'
import json
import os
import sys

path, intern_name, project = sys.argv[1:4]
payload = {"intern_name": intern_name, "project": project}
with open(path + ".tmp", "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False)
os.replace(path + ".tmp", path)
PY

DAEMON_ADDR_FILE="${FEISHU_DAEMON_ADDR_FILE}"
DAEMON_HTTP_PORT="$("${PYTHON}" - "${DAEMON_ADDR_FILE}" <<'PY' 2>/dev/null || true
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    print(json.load(f)["http_port"])
PY
)"

request_light_refresh() {
    if [ -n "${DAEMON_HTTP_PORT}" ]; then
        curl -s --connect-timeout 2 --max-time 5 -X POST "http://localhost:${DAEMON_HTTP_PORT}/api/intern/request_refresh" \
            -H 'Content-Type: application/json' \
            --data-binary @"${INTERN_STATUS_PAYLOAD}" > /dev/null 2>&1 || true
    fi
}

if [[ "${SESSION_EXISTS}" -eq 1 && "${PROCESS_RUNNING}" -eq 1 ]]; then
    APPLIED_SETTINGS_MTIME="$(get_tmux_env_value "${INTERN_NAME}" "CODEX_SETTINGS_MTIME")"
    APPLIED_SHARED_REPO="$(get_tmux_env_value "${INTERN_NAME}" "INTERN_SHARED_REPO")"
    APPLIED_CODEX_PROFILE="$(get_tmux_env_value "${INTERN_NAME}" "CODEX_PROFILE")"
    if [[ -n "${APPLIED_SETTINGS_MTIME}" && "${APPLIED_SETTINGS_MTIME}" == "${SETTINGS_MTIME}" && "${APPLIED_SHARED_REPO}" == "${SHARED_REPO}" && "${APPLIED_CODEX_PROFILE}" == "${ACTIVE_CODEX_PROFILE}" && "${TRUST_STATE_CHANGED}" -eq 0 ]]; then
        if [[ "${INTERN_START_NO_ATTACH:-0}" == "1" ]]; then
            ok "tmux session '${INTERN_NAME}' exists and Codex is already running with current hook/runtime config."
            request_light_refresh
            info "INTERN_START_NO_ATTACH=1; leaving session detached."
            exit 0
        fi
        ok "tmux session '${INTERN_NAME}' exists and Codex is already running with current hook/runtime config. Attaching..."
        exec tmux attach-session -t "=${INTERN_NAME}"
    fi
    warn "tmux session '${INTERN_NAME}' exists, but Codex is using stale hook/runtime config. Restarting in place..."
fi

# ============================================================
# Step 4.7: 同步 Skill 农场（task220）
# ============================================================
# 重建 ${INTERN_DIR}/.agents/skills/ 以反映 .intern_skill.json 的最新启用列表。
# 失败不阻断启动（保留上一次成功状态），错误写到 stderr 进 log。
SKILL_SYNC_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/internctl.py"
if [[ -f "${SKILL_SYNC_SCRIPT}" ]]; then
    info "Step 4.7: 同步 Codex Skill 农场..."
    WORK_AGENTS_ROOT="${WORK_ROOT}" "${PYTHON}" "${SKILL_SYNC_SCRIPT}" skill sync "${INTERN_NAME}" >/dev/null 2>&1 || \
        warn "skill sync 失败（不阻断启动），可手动执行 'internctl skill sync ${INTERN_NAME}' 排查"
fi

# ============================================================
# Step 5: 创建 tmux session 并启动 Codex
# ============================================================
CODEX_COMMAND="$(resolve_codex_command)"
if [[ "${CODEX_RESUME_ON_START:-0}" == "1" ]]; then
    CODEX_COMMAND="${CODEX_COMMAND} resume --last"
fi

if [[ "${SESSION_EXISTS}" -eq 1 ]]; then
    info "Step 5: 在现有 tmux session '${INTERN_NAME}' 中重启 Codex..."
    # 动态解析实际 pane target——历史 session 的唯一 window 可能不是 0（task253）
    PANE_INDEX="$(tmux list-panes -s -t "=${INTERN_NAME}" -F '#{window_index}.#{pane_index}' 2>/dev/null | head -n1)"
    if [[ -z "${PANE_INDEX}" ]]; then
        die "无法解析 tmux session '${INTERN_NAME}' 的 pane target（list-panes 返回空）"
    fi
    tmux respawn-pane -k -t "=${INTERN_NAME}:${PANE_INDEX}" -c "${INTERN_DIR}"
else
    info "Step 5: 创建 tmux session '${INTERN_NAME}' 并启动 Codex..."
    tmux new-session -d -s "${INTERN_NAME}" -c "${INTERN_DIR}"
fi

# 通知 VS Code 插件 tmux session 已就绪
tmux wait-for -S "session_ready_${INTERN_NAME}" 2>/dev/null || true

# 设置环境变量（在 tmux session 内）
tmux send-keys -t "=${INTERN_NAME}:" "source ~/.bashrc 2>/dev/null" Enter
tmux send-keys -t "=${INTERN_NAME}:" "set -a; [ -f \"${WORK_ROOT}/enterprise/user.env\" ] && . \"${WORK_ROOT}/enterprise/user.env\"; set +a" Enter
tmux send-keys -t "=${INTERN_NAME}:" "export INTERN_DIR=\"${INTERN_DIR}\"" Enter
tmux send-keys -t "=${INTERN_NAME}:" "export PROJECT_REPO=\"${INTERN_REPO}\"" Enter
tmux send-keys -t "=${INTERN_NAME}:" "export WORK_AGENTS_ROOT=\"${WORK_ROOT}\"" Enter
tmux send-keys -t "=${INTERN_NAME}:" "export FEISHU_DAEMON_ADDR_FILE=\"${FEISHU_DAEMON_ADDR_FILE}\"" Enter
tmux set-environment -t "=${INTERN_NAME}" INTERN_DIR "${INTERN_DIR}"
tmux set-environment -t "=${INTERN_NAME}" PROJECT_REPO "${INTERN_REPO}"
tmux set-environment -t "=${INTERN_NAME}" WORK_AGENTS_ROOT "${WORK_ROOT}"
tmux set-environment -t "=${INTERN_NAME}" FEISHU_DAEMON_ADDR_FILE "${FEISHU_DAEMON_ADDR_FILE}"
tmux set-environment -t "=${INTERN_NAME}" INTERN_SHARED_REPO "${SHARED_REPO}"
tmux set-environment -t "=${INTERN_NAME}" CODEX_SETTINGS_MTIME "${SETTINGS_MTIME}"
if [[ "${CODEX_LB_ENABLED}" -eq 1 ]]; then
    tmux send-keys -t "=${INTERN_NAME}:" "export LB_API_KEY=\"${CODEX_LB_API_KEY}\"" Enter
    tmux set-environment -t "=${INTERN_NAME}" LB_API_KEY "${CODEX_LB_API_KEY}"
else
    tmux set-environment -u -t "=${INTERN_NAME}" LB_API_KEY 2>/dev/null || true
fi
if [[ -n "${ACTIVE_CODEX_PROFILE}" ]]; then
    tmux send-keys -t "=${INTERN_NAME}:" "export CODEX_PROFILE=\"${ACTIVE_CODEX_PROFILE}\"" Enter
    tmux set-environment -t "=${INTERN_NAME}" CODEX_PROFILE "${ACTIVE_CODEX_PROFILE}"
else
    tmux set-environment -u -t "=${INTERN_NAME}" CODEX_PROFILE 2>/dev/null || true
fi

if [[ "$(codex_auth_mode)" == "chatgpt" ]]; then
    info "Codex auth cache is ChatGPT; unsetting OPENAI_API_KEY for the Codex process so ChatGPT auth is not shadowed."
    tmux send-keys -t "=${INTERN_NAME}:" "unset OPENAI_API_KEY" Enter
    tmux set-environment -u -t "=${INTERN_NAME}" OPENAI_API_KEY 2>/dev/null || true
fi

if should_enable_root_bypass; then
    tmux send-keys -t "=${INTERN_NAME}:" "export IS_SANDBOX=1" Enter
fi

# 启动 Codex CLI
if [ -n "${DAEMON_HTTP_PORT}" ]; then
    OFFLINE_NOTIFY="curl -s --connect-timeout 2 --max-time 5 -X POST http://localhost:${DAEMON_HTTP_PORT}/api/intern/offline -H 'Content-Type: application/json' --data-binary @\"${INTERN_STATUS_PAYLOAD}\" > /dev/null 2>&1"
else
    OFFLINE_NOTIFY="true"
fi
info "Using Codex launch command: ${CODEX_COMMAND} ; <offline notify>"
tmux send-keys -t "=${INTERN_NAME}:" "${CODEX_COMMAND} ; ${OFFLINE_NOTIFY}" Enter

ok "Codex session started in tmux '${INTERN_NAME}'."
if [[ "${INTERN_START_NO_ATTACH:-0}" == "1" ]]; then
    if wait_for_live_process "${INTERN_NAME}" "${INTERN_LIGHT_REFRESH_WAIT_SECONDS:-20}"; then
        ok "Codex process is running."
    else
        warn "Codex process was not confirmed within ${INTERN_LIGHT_REFRESH_WAIT_SECONDS:-20}s; requesting light refresh anyway."
    fi
    request_light_refresh
    info "INTERN_START_NO_ATTACH=1; leaving session detached."
    exit 0
fi

if wait_for_codex_prompt "${INTERN_NAME}" 30; then
    ok "Codex prompt is ready."
else
    warn "Codex prompt was not confirmed within 30s; session is running but first delivery may need retry."
fi
request_light_refresh
info ""
info "Attaching to tmux session..."

exec tmux attach-session -t "=${INTERN_NAME}"
