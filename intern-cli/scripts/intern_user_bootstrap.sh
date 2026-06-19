#!/usr/bin/env bash
set -euo pipefail

# User-side bootstrap for running Codex interns without the VS Code extension.
#
# Intended layout:
#   WORK_AGENTS_ROOT=$HOME
#   $HOME/<project>          -> the user's own fork/clone of the project repo
#   $HOME/<intern_name>/...  -> per-intern runtime state and logs
#
# By default each intern runs against --project-path directly, so code changes
# produced by different interns land in the user's single fork/clone repo. Use
# --clone-per-intern only when a separate per-intern code clone is intentional.
#
# Admins may prefill these defaults before distributing the script. Secrets are
# intentionally not hardcoded here; relay token is read from _owner.json, env, or
# an interactive prompt.
DEFAULT_RELAY_URL="${INTERN_DEFAULT_RELAY_URL:-}"
DEFAULT_RELAY_HTTP_URL="${INTERN_DEFAULT_RELAY_HTTP_URL:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INTERNCTL="${CLI_ROOT}/internctl.py"
START_CODEX="${SCRIPT_DIR}/intern_start_codex.sh"

ACTION="start"
WORK_ROOT="${WORK_AGENTS_ROOT:-${HOME}}"
PROJECT_PATH="${INTERN_PROJECT_PATH:-${REPO_ROOT}}"
PROJECT_NAME="${INTERN_PROJECT_NAME:-}"
INTERN_NAME="${INTERN_NAME:-}"
RELAY_URL="${INTERN_RELAY_URL:-${DEFAULT_RELAY_URL}}"
RELAY_HTTP_URL="${INTERN_RELAY_HTTP_URL:-${DEFAULT_RELAY_HTTP_URL}}"
RELAY_TOKEN="${INTERN_RELAY_TOKEN:-}"
OWNER_MOBILE="${INTERN_OWNER_MOBILE:-}"
PYTHON_BIN="${PYTHON:-}"
ATTACH=0
START_DAEMON=1
CHECK_PUSH=1
REUSE_PROJECT_REPO=1
NON_INTERACTIVE=0
WRITE_KEY=0
CONFIRM_DELETE=0
FEISHU_APP_ID="${FEISHU_APP_ID:-}"
FEISHU_APP_SECRET="${FEISHU_APP_SECRET:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $*"; }
ok() { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
die() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage:
  $(basename "$0") [start|setup|list|status|stop|delete] [options]

Common:
  --root PATH              WORK_AGENTS_ROOT. Default: WORK_AGENTS_ROOT env, else HOME
  --project-path PATH      User fork/clone repo path. Default: this repo
  --project NAME           Project id used by intern runtime. Default: <user>_project
  --python PATH            Python >= 3.10 interpreter. Default: PYTHON env, conda python,
                           then python3.12/python3.11/python3.10/python3
  --non-interactive        Do not prompt; fail when required values are missing

Relay / owner:
  --relay-url URL          Public relay WS URL. Can also prefill INTERN_DEFAULT_RELAY_URL
  --relay-http-url URL     Public relay HTTP URL. Usually inferred from --relay-url
  --relay-token TOKEN      Relay token. Prefer existing _owner.json or INTERN_RELAY_TOKEN
  --mobile PHONE           Owner mobile written to .feishu_registry/_owner.json
  --write-key              Prompt/write <root>/key.txt app_id/app_secret if missing

Start/stop/delete intern:
  --intern NAME            Intern name, e.g. intern_research_bot
  --attach                 Attach tmux after starting. Default leaves it detached
  --clone-per-intern       Clone one code repo per intern. Default runs against --project-path
  --no-daemon-start        Do not start daemon; only prepare project/intern/start Codex
  --skip-push-check        Skip git push dry-run check for the project repo
  --confirm-delete         Skip delete confirmation. Required with delete --non-interactive

Examples:
  # First run in the user's fork/clone repo:
  $(basename "$0") start --intern intern_research_bot --relay-url ws://relay.example:28081

  # Add another bot later:
  $(basename "$0") start --intern intern_coding_bot

  # Inspect local state:
  $(basename "$0") list
  $(basename "$0") status

  # Stop one running intern tmux session:
  $(basename "$0") stop --intern intern_research_bot

  # Delete a stopped intern:
  $(basename "$0") delete --intern intern_research_bot
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|setup|list|status|stop|delete)
      ACTION="$1"
      shift
      ;;
    --root)
      WORK_ROOT="${2:-}"
      shift 2
      ;;
    --root=*)
      WORK_ROOT="${1#--root=}"
      shift
      ;;
    --project-path)
      PROJECT_PATH="${2:-}"
      shift 2
      ;;
    --project-path=*)
      PROJECT_PATH="${1#--project-path=}"
      shift
      ;;
    --project)
      PROJECT_NAME="${2:-}"
      shift 2
      ;;
    --project=*)
      PROJECT_NAME="${1#--project=}"
      shift
      ;;
    --intern)
      INTERN_NAME="${2:-}"
      shift 2
      ;;
    --intern=*)
      INTERN_NAME="${1#--intern=}"
      shift
      ;;
    --relay-url)
      RELAY_URL="${2:-}"
      shift 2
      ;;
    --relay-url=*)
      RELAY_URL="${1#--relay-url=}"
      shift
      ;;
    --relay-http-url|--http-url)
      RELAY_HTTP_URL="${2:-}"
      shift 2
      ;;
    --relay-http-url=*|--http-url=*)
      RELAY_HTTP_URL="${1#*=}"
      shift
      ;;
    --relay-token|--token)
      RELAY_TOKEN="${2:-}"
      shift 2
      ;;
    --relay-token=*|--token=*)
      RELAY_TOKEN="${1#*=}"
      shift
      ;;
    --mobile|--owner-mobile)
      OWNER_MOBILE="${2:-}"
      shift 2
      ;;
    --mobile=*|--owner-mobile=*)
      OWNER_MOBILE="${1#*=}"
      shift
      ;;
    --python)
      PYTHON_BIN="${2:-}"
      shift 2
      ;;
    --python=*)
      PYTHON_BIN="${1#--python=}"
      shift
      ;;
    --attach)
      ATTACH=1
      shift
      ;;
    --detach)
      ATTACH=0
      shift
      ;;
    --clone-per-intern)
      REUSE_PROJECT_REPO=0
      shift
      ;;
    --no-daemon-start)
      START_DAEMON=0
      shift
      ;;
    --skip-push-check)
      CHECK_PUSH=0
      shift
      ;;
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    --write-key)
      WRITE_KEY=1
      shift
      ;;
    --confirm-delete)
      CONFIRM_DELETE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

abs_path() {
  local path="$1"
  if [[ -d "${path}" ]]; then
    (cd "${path}" && pwd -P)
  else
    local parent
    parent="$(dirname "${path}")"
    local base
    base="$(basename "${path}")"
    (cd "${parent}" && printf '%s/%s\n' "$(pwd -P)" "${base}")
  fi
}

safe_project_segment() {
  local value="$1"
  value="$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_]+/_/g; s/^_+//; s/_+$//; s/_+/_/g')"
  [[ -n "${value}" ]] || value="default"
  printf '%s\n' "${value}"
}

current_user_segment() {
  local value="${USER:-${LOGNAME:-}}"
  if [[ -z "${value}" ]] && command -v id >/dev/null 2>&1; then
    value="$(id -un 2>/dev/null || true)"
  fi
  value="$(safe_project_segment "${value}")"
  [[ "${value}" != "default" ]] || value="user"
  printf '%s\n' "${value}"
}

default_project_name() {
  local user_segment
  user_segment="$(current_user_segment)"
  printf '%s_project\n' "${user_segment}"
}

prompt_value() {
  local var_name="$1"
  local prompt="$2"
  local secret="${3:-0}"
  local current="${!var_name:-}"
  if [[ -n "${current}" ]]; then
    return 0
  fi
  if [[ "${NON_INTERACTIVE}" == "1" || ! -t 0 ]]; then
    die "${prompt} is required. Pass the matching option or prefill _owner.json."
  fi
  if [[ "${secret}" == "1" ]]; then
    read -r -s -p "${prompt}: " current
    echo
  else
    read -r -p "${prompt}: " current
  fi
  if [[ -z "${current}" ]]; then
    die "${prompt} cannot be empty"
  fi
  printf -v "${var_name}" '%s' "${current}"
}

resolve_python() {
  local candidate
  python_supported() {
    "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  }
  python_version() {
    "$1" - <<'PY' 2>/dev/null || true
import sys
print(".".join(map(str, sys.version_info[:3])))
PY
  }
  if [[ -n "${PYTHON_BIN}" ]]; then
    command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "Python not found: ${PYTHON_BIN}"
    if ! python_supported "${PYTHON_BIN}"; then
      die "Unsupported Python ${PYTHON_BIN} ($(python_version "${PYTHON_BIN}")). This repo requires Python >= 3.10. Pass --python /path/to/python3.10+."
    fi
    echo "${PYTHON_BIN}"
    return
  fi
  if [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/python" ]]; then
    candidate="${CONDA_PREFIX}/bin/python"
    if python_supported "${candidate}"; then
      echo "${candidate}"
      return
    fi
    warn "Skipping CONDA_PREFIX python ${candidate} ($(python_version "${candidate}")); need Python >= 3.10."
  fi
  for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1 && python_supported "${candidate}"; then
      command -v "${candidate}"
      return
    fi
  done
  die "No supported Python found. This repo requires Python >= 3.10. Activate a suitable env or pass --python /path/to/python3.10+."
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

json_get() {
  local path="$1"
  local key="$2"
  "${PYTHON_BIN}" - "${path}" "${key}" <<'PY'
import json
import sys
path, key = sys.argv[1:3]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    data = {}
value = data
for part in key.split("."):
    if not isinstance(value, dict):
        value = ""
        break
    value = value.get(part, "")
print(value if value is not None else "")
PY
}

write_key_if_requested() {
  local key_path="${WORK_ROOT}/key.txt"
  if [[ -f "${key_path}" ]]; then
    return 0
  fi
  cat > "${WORK_ROOT}/key.txt.example" <<'EOF'
cli_your_feishu_app_id
your_feishu_app_secret
EOF
  if [[ "${WRITE_KEY}" != "1" ]]; then
    warn "No ${key_path}. Public relay daemon mode normally fetches app credentials from relay."
    warn "If this deployment requires local Feishu credentials, rerun with --write-key or fill key.txt manually."
    return 0
  fi
  prompt_value FEISHU_APP_ID "Feishu app_id"
  prompt_value FEISHU_APP_SECRET "Feishu app_secret" 1
  local old_umask
  old_umask="$(umask)"
  umask 077
  {
    printf '%s\n' "${FEISHU_APP_ID}"
    printf '%s\n' "${FEISHU_APP_SECRET}"
  } > "${key_path}"
  umask "${old_umask}"
  ok "Wrote ${key_path}"
}

ensure_git_repo() {
  [[ -d "${PROJECT_PATH}/.git" ]] || die "Project path is not a git repo: ${PROJECT_PATH}"
  git -C "${PROJECT_PATH}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not inside git worktree: ${PROJECT_PATH}"
}

current_branch() {
  local branch
  branch="$(git -C "${PROJECT_PATH}" branch --show-current 2>/dev/null || true)"
  [[ -n "${branch}" ]] || die "Project repo is in detached HEAD. Checkout a branch before running this script."
  echo "${branch}"
}

project_remote_url() {
  local url
  url="$(git -C "${PROJECT_PATH}" remote get-url origin 2>/dev/null || true)"
  [[ -n "${url}" ]] || die "Project repo has no origin remote"
  echo "${url}"
}

check_push_access() {
  if [[ "${CHECK_PUSH}" != "1" ]]; then
    warn "Skipping git push permission check."
    return 0
  fi
  local branch="$1"
  info "Checking git read/push access for origin ${branch}..."
  git -C "${PROJECT_PATH}" ls-remote --exit-code origin HEAD >/dev/null
  git -C "${PROJECT_PATH}" push --dry-run origin "HEAD:${branch}" >/dev/null
  ok "Git origin is reachable and push dry-run succeeded."
}

ensure_project_link() {
  local root_project="${WORK_ROOT}/${PROJECT_NAME}"
  if [[ "${root_project}" == "${PROJECT_PATH}" ]]; then
    return 0
  fi
  if [[ -e "${root_project}" || -L "${root_project}" ]]; then
    local existing
    existing="$(abs_path "${root_project}")"
    if [[ "${existing}" != "${PROJECT_PATH}" ]]; then
      die "${root_project} already exists and points to ${existing}, not ${PROJECT_PATH}"
    fi
    return 0
  fi
  ln -s "${PROJECT_PATH}" "${root_project}"
  ok "Linked ${root_project} -> ${PROJECT_PATH}"
}

ensure_hook_links() {
  local hooks_src="${REPO_ROOT}/vscode-extension/hooks"
  [[ -d "${hooks_src}" ]] || die "Hooks directory not found: ${hooks_src}"
  mkdir -p "${WORK_ROOT}/.github"
  ln -sfn "${hooks_src}" "${WORK_ROOT}/.github/hooks"
  ln -sfn "${hooks_src}/codex_settings.toml" "${WORK_ROOT}/.github/codex_settings.toml"
  ok "Hook links ready under ${WORK_ROOT}/.github"
}

ensure_owner_and_relay() {
  local registry="${WORK_ROOT}/.feishu_registry"
  local owner="${registry}/_owner.json"
  mkdir -p "${registry}"
  if [[ -f "${owner}" ]]; then
    [[ -n "${RELAY_URL}" ]] || RELAY_URL="$(json_get "${owner}" relay_url)"
    [[ -n "${RELAY_HTTP_URL}" ]] || RELAY_HTTP_URL="$(json_get "${owner}" relay_http_url)"
    [[ -n "${RELAY_TOKEN}" ]] || RELAY_TOKEN="$(json_get "${owner}" relay_token)"
    [[ -n "${OWNER_MOBILE}" ]] || OWNER_MOBILE="$(json_get "${owner}" mobile)"
  fi
  prompt_value RELAY_URL "Relay WS URL"
  prompt_value RELAY_TOKEN "Relay token" 1
  prompt_value OWNER_MOBILE "Your Feishu mobile phone"

  local args=(setup connect-relay --json --relay-url "${RELAY_URL}" --token "${RELAY_TOKEN}" --owner-mobile "${OWNER_MOBILE}")
  if [[ -n "${RELAY_HTTP_URL}" ]]; then
    args+=(--relay-http-url "${RELAY_HTTP_URL}")
  fi
  info "Connecting local daemon config to relay ${RELAY_URL}..."
  local report="/tmp/intern_user_connect_relay.$$.json"
  if ! WORK_AGENTS_ROOT="${WORK_ROOT}" PYTHON="${PYTHON_BIN}" "${PYTHON_BIN}" "${INTERNCTL}" "${args[@]}" >"${report}"; then
    cat "${report}" >&2 2>/dev/null || true
    die "Failed to connect relay. Check relay URL/token and owner mobile."
  fi
  chmod 600 "${owner}" 2>/dev/null || true
  rm -f "${report}"
  ok "Relay config ready: ${owner}"
}

ensure_python_deps() {
  local missing
  missing="$("${PYTHON_BIN}" - <<'PY'
import importlib.util
missing = []
for module, package in [("websockets", "websockets"), ("lark_oapi", "lark-oapi")]:
    if importlib.util.find_spec(module) is None:
        missing.append(package)
print(" ".join(missing))
PY
)"
  if [[ -z "${missing}" ]]; then
    return 0
  fi
  die "Python package(s) missing in ${PYTHON_BIN}: ${missing}. Install them first, e.g. ${PYTHON_BIN} -m pip install ${missing}"
}

start_daemon_if_needed() {
  if [[ "${START_DAEMON}" != "1" ]]; then
    warn "Skipping daemon start."
    return 0
  fi
  ensure_python_deps
  info "Starting local daemon..."
  WORK_AGENTS_ROOT="${WORK_ROOT}" PYTHON="${PYTHON_BIN}" "${PYTHON_BIN}" "${INTERNCTL}" daemon start || {
    warn "daemon start failed; showing recent wrapper log if available"
    tail -n 80 "${WORK_ROOT}/llm_intern_logs/_daemon/feishu_daemon.wrapper.log" 2>/dev/null || true
    return 1
  }
  WORK_AGENTS_ROOT="${WORK_ROOT}" PYTHON="${PYTHON_BIN}" "${PYTHON_BIN}" "${INTERNCTL}" daemon status --json
}

validate_intern_name() {
  local name="$1"
  [[ "${name}" =~ ^intern_[a-z0-9_]+$ ]] || die "Invalid intern name '${name}'. New interns must match ^intern_[a-z0-9_]+$"
}

ensure_intern_metadata() {
  local branch="$1"
  validate_intern_name "${INTERN_NAME}"

  local intern_dir="${PROJECT_PATH}/workspace/interns/${INTERN_NAME}"
  local status_path="${intern_dir}/status.md"
  local knowledge_path="${intern_dir}/knowledge.md"
  local changed=0

  mkdir -p "${intern_dir}"
  if [[ ! -f "${status_path}" ]]; then
    cat > "${status_path}" <<EOF
# ${INTERN_NAME} - 状态

<!-- METADATA:STATUS=Idle,TASK=,ROLE=independent,TEAM_ID= -->

| 字段 | 值 |
|------|-----|
| Name | ${INTERN_NAME} |
| Status | Idle |
| Role | independent |
| Team | N/A |
| Current Task |  |
| PR | N/A |
| Session | 0 |
EOF
    changed=1
  fi
  if [[ ! -f "${knowledge_path}" ]]; then
    cat > "${knowledge_path}" <<EOF
# ${INTERN_NAME} - 个人知识库

<!-- METADATA:SESSION=0 -->

---

## 知识条目

EOF
    changed=1
  fi

  if [[ "${changed}" == "0" ]]; then
    ok "Intern metadata already exists: workspace/interns/${INTERN_NAME}"
    return 0
  fi

  info "Creating intern metadata in project repo..."
  if ! git -C "${PROJECT_PATH}" diff --cached --quiet; then
    die "Project repo has staged changes. Commit/unstage them first to avoid mixing with intern metadata."
  fi
  git -C "${PROJECT_PATH}" add "workspace/interns/${INTERN_NAME}"
  if git -C "${PROJECT_PATH}" diff --cached --quiet -- "workspace/interns/${INTERN_NAME}"; then
    ok "Intern metadata was already tracked."
    return 0
  fi
  git -C "${PROJECT_PATH}" commit -m "[${INTERN_NAME}] intern: create metadata" -- "workspace/interns/${INTERN_NAME}"
  git -C "${PROJECT_PATH}" push origin "${branch}"
  ok "Created and pushed intern metadata for ${INTERN_NAME}"
}

refresh_project_repo() {
  local branch="$1"
  info "Refreshing project repo ${PROJECT_PATH} (${branch})..."
  if ! git -C "${PROJECT_PATH}" ls-remote --exit-code --heads origin "${branch}" >/dev/null 2>&1; then
    warn "Remote branch origin/${branch} does not exist; skipping pull. A later push may create it."
    return 0
  fi
  git -C "${PROJECT_PATH}" pull --rebase --autostash origin "${branch}"
}

start_intern() {
  local repo_url="$1"
  local no_attach=1
  [[ "${ATTACH}" == "1" ]] && no_attach=0
  require_command codex
  info "Starting Codex intern ${INTERN_NAME}..."
  if [[ "${REUSE_PROJECT_REPO}" == "1" ]]; then
    info "Reusing project repo for intern code: ${PROJECT_PATH}"
    WORK_AGENTS_ROOT="${WORK_ROOT}" \
      PYTHON="${PYTHON_BIN}" \
      PROJECT_REPO_URL="${repo_url}" \
      INTERN_CODE_REPO_PATH="${PROJECT_PATH}" \
      INTERN_METADATA_INTERN_DIR="${PROJECT_PATH}/workspace/interns/${INTERN_NAME}" \
      INTERN_START_NO_ATTACH="${no_attach}" \
      bash "${START_CODEX}" "${INTERN_NAME}" "${PROJECT_NAME}"
  else
    warn "clone-per-intern mode enabled; intern_start_codex.sh may clone ${repo_url} into the intern runtime dir."
    WORK_AGENTS_ROOT="${WORK_ROOT}" \
      PYTHON="${PYTHON_BIN}" \
      PROJECT_REPO_URL="${repo_url}" \
      INTERN_START_NO_ATTACH="${no_attach}" \
      bash "${START_CODEX}" "${INTERN_NAME}" "${PROJECT_NAME}"
  fi
}

list_interns() {
  local interns_dir="${PROJECT_PATH}/workspace/interns"
  echo "WORK_AGENTS_ROOT: ${WORK_ROOT}"
  echo "Project:          ${PROJECT_NAME} (${PROJECT_PATH})"
  echo
  echo "Registered interns:"
  if [[ ! -d "${interns_dir}" ]]; then
    echo "  (none)"
  else
    local found=0
    while read -r path; do
      [[ -n "${path}" ]] || continue
      found=1
      local name
      name="$(basename "${path}")"
      local state="-"
      if [[ -f "${path}/status.md" ]]; then
        state="$(sed -n 's/.*METADATA:STATUS=\([^,]*\).*/\1/p' "${path}/status.md" | head -n 1)"
        [[ -n "${state}" ]] || state="-"
      fi
      printf '  %-32s %s\n' "${name}" "${state}"
    done < <(find "${interns_dir}" -mindepth 1 -maxdepth 1 -type d -print | sort)
    [[ "${found}" == "1" ]] || echo "  (none)"
  fi
  echo
  echo "tmux sessions:"
  tmux list-sessions -F '  #{session_name}' 2>/dev/null | sed -n '/intern_/p' || echo "  (tmux unavailable or no sessions)"
}

show_status() {
  list_interns
  echo
  echo "Daemon:"
  WORK_AGENTS_ROOT="${WORK_ROOT}" PYTHON="${PYTHON_BIN}" "${PYTHON_BIN}" "${INTERNCTL}" daemon status --json || true
  echo
  echo "Relay config:"
  local owner="${WORK_ROOT}/.feishu_registry/_owner.json"
  if [[ -f "${owner}" ]]; then
    "${PYTHON_BIN}" - "${owner}" <<'PY' || true
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception as exc:
    print(f"  unreadable: {exc}")
    raise SystemExit(0)

print(f"  relay_url:      {data.get('relay_url') or '-'}")
print(f"  relay_http_url: {data.get('relay_http_url') or '-'}")
print(f"  mobile:         {data.get('mobile') or data.get('owner_mobile') or '-'}")
print(f"  relay_token:    {'configured' if data.get('relay_token') else 'missing'}")
PY
  else
    echo "  missing: ${owner}"
  fi
}

notify_daemon_intern_offline() {
  local addr_file="${FEISHU_DAEMON_ADDR_FILE:-/tmp/feishu_daemon.json}"
  if [[ ! -f "${addr_file}" ]]; then
    warn "Daemon address file not found: ${addr_file}. Group light may remain stale."
    return 0
  fi
  if WORK_AGENTS_ROOT="${WORK_ROOT}" "${PYTHON_BIN}" - "${addr_file}" "${INTERN_NAME}" "${PROJECT_NAME}" <<'PY'
import json
import sys
import urllib.error
import urllib.request

addr_path, intern_name, project = sys.argv[1:4]
try:
    with open(addr_path, "r", encoding="utf-8") as f:
        addr = json.load(f)
    port = int(addr.get("http_port") or 0)
    if not port:
        raise RuntimeError(f"{addr_path} missing http_port")
    payload = json.dumps({
        "intern_name": intern_name,
        "project": project,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/intern/offline",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}: {body}")
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")
    print(f"daemon offline notify failed: HTTP {exc.code}: {body}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"daemon offline notify failed: {exc}", file=sys.stderr)
    sys.exit(1)
PY
  then
    ok "Daemon notified that ${INTERN_NAME} (${PROJECT_NAME}) is offline."
  else
    warn "Failed to notify daemon that ${INTERN_NAME} is offline. Group light may remain stale."
  fi
}

stop_intern() {
  if [[ -z "${INTERN_NAME}" ]]; then
    prompt_value INTERN_NAME "Intern name to stop, e.g. intern_research_bot"
  fi
  validate_intern_name "${INTERN_NAME}"
  require_command tmux
  info "Stopping intern ${INTERN_NAME}..."
  WORK_AGENTS_ROOT="${WORK_ROOT}" PYTHON="${PYTHON_BIN}" "${PYTHON_BIN}" "${INTERNCTL}" session stop "${INTERN_NAME}"
  notify_daemon_intern_offline
  ok "Stop command completed for ${INTERN_NAME}. Daemon and relay config were left unchanged."
}

tmux_session_running() {
  tmux has-session -t "=${1}" 2>/dev/null
}

print_delete_scope() {
  warn "Delete will remove intern ${INTERN_NAME} from project ${PROJECT_NAME}:"
  warn "  - project metadata: ${PROJECT_PATH}/workspace/interns/${INTERN_NAME}/"
  warn "  - local runtime directory: ${WORK_ROOT}/${INTERN_NAME}/"
  warn "  - session registry entry: ${WORK_ROOT}/.intern_sessions.json"
  warn "  - Feishu group and relay/local chat registry entry, if daemon/relay are reachable"
  warn "It will not delete the project repo, relay owner config, Codex auth, or the daemon process."
}

confirm_delete_intern() {
  if [[ "${CONFIRM_DELETE}" == "1" ]]; then
    return 0
  fi
  if [[ "${NON_INTERACTIVE}" == "1" || ! -t 0 ]]; then
    die "Delete requires --confirm-delete in non-interactive mode."
  fi
  local answer
  read -r -p "Type '${INTERN_NAME}' to delete it permanently: " answer
  [[ "${answer}" == "${INTERN_NAME}" ]] || die "Delete cancelled."
}

delete_intern() {
  if [[ -z "${INTERN_NAME}" ]]; then
    prompt_value INTERN_NAME "Intern name to delete, e.g. intern_research_bot"
  fi
  validate_intern_name "${INTERN_NAME}"
  require_command git
  require_command tmux
  ensure_git_repo
  if tmux_session_running "${INTERN_NAME}"; then
    die "${INTERN_NAME} still has a tmux session. Stop it first: $(basename "$0") stop --intern ${INTERN_NAME}"
  fi
  print_delete_scope
  confirm_delete_intern
  mkdir -p "${WORK_ROOT}"
  ensure_project_link
  local branch
  branch="$(current_branch)"
  check_push_access "${branch}"
  refresh_project_repo "${branch}"
  info "Deleting stopped intern ${INTERN_NAME} from project ${PROJECT_NAME}..."
  WORK_AGENTS_ROOT="${WORK_ROOT}" PYTHON="${PYTHON_BIN}" "${PYTHON_BIN}" "${INTERNCTL}" delete "${INTERN_NAME}" --local-project --project "${PROJECT_NAME}" --branch "${branch}" --confirm
  ok "Deleted ${INTERN_NAME}. Daemon process was left running."
}

main() {
  WORK_ROOT="$(abs_path "${WORK_ROOT}")"
  PROJECT_PATH="$(abs_path "${PROJECT_PATH}")"
  if [[ -z "${PROJECT_NAME}" ]]; then
    PROJECT_NAME="$(default_project_name)"
  fi
  PYTHON_BIN="$(resolve_python)"

  if [[ "${ACTION}" == "stop" ]]; then
    stop_intern
    return 0
  fi
  if [[ "${ACTION}" == "delete" ]]; then
    delete_intern
    return 0
  fi

  require_command git
  ensure_git_repo
  case "${ACTION}" in
    list)
      list_interns
      return 0
      ;;
    status)
      show_status
      return 0
      ;;
  esac

  require_command tmux
  mkdir -p "${WORK_ROOT}" "${WORK_ROOT}/llm_intern_logs/_daemon"
  ensure_project_link
  ensure_hook_links

  local branch
  branch="$(current_branch)"
  local repo_url
  repo_url="$(project_remote_url)"
  check_push_access "${branch}"
  refresh_project_repo "${branch}"
  write_key_if_requested
  ensure_owner_and_relay
  start_daemon_if_needed

  if [[ "${ACTION}" == "setup" ]]; then
    ok "Setup complete."
    return 0
  fi

  if [[ -z "${INTERN_NAME}" ]]; then
    prompt_value INTERN_NAME "Intern name, e.g. intern_research_bot"
  fi
  ensure_intern_metadata "${branch}"
  refresh_project_repo "${branch}"
  start_intern "${repo_url}"
  ok "Bootstrap finished."
}

main
