"""internctl setup — 环境校验与自动配置。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from lib.enterprise_boundary import emit_admin_rejection, enterprise_mode_active
from lib.enterprise_setup import EnterpriseSetupEngine, print_json_report, write_export
from lib.user_env import load_enterprise_user_env

WORK_AGENTS_ROOT: str = os.environ.get("WORK_AGENTS_ROOT") or "/work-agents"
SHARED_REPO: str = os.path.join(WORK_AGENTS_ROOT, "axis_intern_agents")
REPO_URL: str = "git@codeup.aliyun.com:finalsystems/chlxydl/axis_intern_agents.git"
FEISHU_REGISTRY_DIR: str = os.path.join(WORK_AGENTS_ROOT, ".feishu_registry")


def _load_enterprise_user_env(work_root: str) -> None:
    load_enterprise_user_env(work_root)


def setup_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("setup", help="校验环境并自动配置")
    p.add_argument("--check", action="store_true", help="仅检查，不修复")
    p.add_argument("--auto", action="store_true", help="仅执行自动配置")
    p.set_defaults(func=run)

    setup_sub = p.add_subparsers(dest="setup_command")

    status = setup_sub.add_parser("status", help="输出企业 setup 状态 JSON contract")
    status.add_argument("--json", action="store_true", required=True, help="输出机器可读 JSON")
    status.add_argument("--policy", help="企业策略文件路径")
    status.add_argument("--secrets", help="企业 secret bundle 路径")
    status.set_defaults(func=run_enterprise_status)

    doctor = setup_sub.add_parser("doctor", help="执行企业 setup 深度诊断 JSON contract")
    doctor.add_argument("--json", action="store_true", required=True, help="输出机器可读 JSON")
    doctor.add_argument("--policy", help="企业策略文件路径")
    doctor.add_argument("--secrets", help="企业 secret bundle 路径")
    doctor.set_defaults(func=run_enterprise_doctor)

    apply = setup_sub.add_parser("apply", help="执行用户侧可自动修复项并输出 JSON contract")
    apply.add_argument("--json", action="store_true", required=True, help="输出机器可读 JSON")
    apply.add_argument("--policy", help="企业策略文件路径")
    apply.add_argument("--secrets", help="企业 secret bundle 路径")
    apply.add_argument(
        "--install-runtime",
        action="store_true",
        help="安装必需的本机 runtime 依赖（如 tmux、Codex CLI、daemon Python 包）",
    )
    apply.set_defaults(func=run_enterprise_apply)

    connect = setup_sub.add_parser("connect-relay", help="配置用户侧 daemon relay 并从 relay 拉取 daemon policy")
    connect.add_argument("--json", action="store_true", required=True, help="输出机器可读 JSON")
    connect.add_argument("--relay-url", help="Relay server URL, e.g. ws://10.0.0.1:28081 or http://10.0.0.1:28080; defaults to .feishu_registry/_owner.json")
    connect.add_argument("--relay-http-url", help="Optional Relay HTTP URL override; otherwise inferred from --relay-url")
    connect.add_argument("--token", help="Relay token; defaults to .feishu_registry/_owner.json")
    connect.add_argument("--owner-mobile", help="当前用户手机号；默认读取 .feishu_registry/_owner.json 的 mobile")
    connect.add_argument("--owner-open-id", help="当前用户 open_id；默认读取 .feishu_registry/_owner.json 的 owner_open_id/open_id")
    connect.add_argument("--machine-id", help="本机 machine_id；默认由 daemon 生成")
    connect.set_defaults(func=run_enterprise_connect_relay)

    export = setup_sub.add_parser("export", help="导出脱敏企业 setup report")
    export.add_argument("--json", action="store_true", required=True, help="输出机器可读 JSON")
    export.add_argument("--policy", help="企业策略文件路径")
    export.add_argument("--secrets", help="企业 secret bundle 路径")
    export.add_argument("--output", help="同时写入指定 JSON 文件")
    export.set_defaults(func=run_enterprise_export)


def _enterprise_engine(args: argparse.Namespace) -> EnterpriseSetupEngine:
    work_root = os.environ.get("WORK_AGENTS_ROOT") or WORK_AGENTS_ROOT
    _load_enterprise_user_env(work_root)
    return EnterpriseSetupEngine(
        work_root,
        policy_path=getattr(args, "policy", None),
        secret_path=getattr(args, "secrets", None),
    )


def run_enterprise_status(args: argparse.Namespace) -> int:
    report = _enterprise_engine(args).status()
    print_json_report(report)
    return 0 if report["ready"] else 1


def run_enterprise_doctor(args: argparse.Namespace) -> int:
    report = _enterprise_engine(args).doctor()
    print_json_report(report)
    return 0 if report["ready"] else 1


def run_enterprise_apply(args: argparse.Namespace) -> int:
    report = _enterprise_engine(args).apply(install_runtime=bool(getattr(args, "install_runtime", False)))
    print_json_report(report)
    return 0 if report["ready"] else 1


def _relay_netloc(hostname: str, port: int | None) -> str:
    netloc = hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return netloc


def _normalize_relay_urls(relay_url: str, relay_http_url: str = "") -> tuple[str, str]:
    parsed = urllib.parse.urlparse(relay_url)
    if parsed.scheme not in {"ws", "wss", "http", "https"} or not parsed.hostname:
        raise ValueError("relay-url must be ws://, wss://, http://, or https:// with a host")
    port = parsed.port
    if parsed.scheme in {"ws", "wss"}:
        ws_scheme = parsed.scheme
        http_scheme = "https" if parsed.scheme == "wss" else "http"
        ws_port = port
        http_port = port - 1 if port and port > 1 else port
    else:
        http_scheme = parsed.scheme
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        http_port = port
        ws_port = port + 1 if port else port
    ws_url = urllib.parse.urlunparse((ws_scheme, _relay_netloc(parsed.hostname, ws_port), "", "", "", ""))
    inferred_http_url = urllib.parse.urlunparse((http_scheme, _relay_netloc(parsed.hostname, http_port), "", "", "", ""))
    return ws_url, (relay_http_url.strip().rstrip("/") if relay_http_url else inferred_http_url)


def _write_json_atomic(path: Path, data: dict, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)
    if mode is not None:
        path.chmod(mode)


def _fetch_daemon_policy(relay_http_url: str, token: str) -> dict:
    url = relay_http_url.rstrip("/") + "/api/enterprise/daemon-policy"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"relay daemon policy fetch failed: HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"relay daemon policy fetch failed: {exc}") from exc


def _load_owner_defaults(owner_path: Path) -> dict:
    if not owner_path.is_file():
        return {}
    try:
        data = json.loads(owner_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def run_enterprise_connect_relay(args: argparse.Namespace) -> int:
    work_root = Path(os.environ.get("WORK_AGENTS_ROOT") or WORK_AGENTS_ROOT)
    _load_enterprise_user_env(os.fspath(work_root))
    owner_path = work_root / ".feishu_registry" / "_owner.json"
    owner = _load_owner_defaults(owner_path)
    raw_relay_url = str(args.relay_url or owner.get("relay_url") or owner.get("relay_http_url") or "").strip()
    raw_relay_http_url = str(args.relay_http_url or owner.get("relay_http_url") or "").strip()
    token = str(args.token or owner.get("relay_token") or "").strip()
    owner_mobile = str(args.owner_mobile or owner.get("mobile") or owner.get("owner_mobile") or "").strip()
    owner_open_id = str(args.owner_open_id or owner.get("owner_open_id") or owner.get("open_id") or "").strip()
    if not raw_relay_url:
        print(json.dumps({
            "schema": "intern-agents.setup-connect-relay.v1",
            "ok": False,
            "error": "relay_url is required",
            "defaults_path": os.fspath(owner_path),
            "next_actions": [
                "Pass --relay-url, or run from a WORK_AGENTS_ROOT containing .feishu_registry/_owner.json.",
            ],
        }, ensure_ascii=False, indent=2))
        return 1
    if not token:
        print(json.dumps({
            "schema": "intern-agents.setup-connect-relay.v1",
            "ok": False,
            "relay_url": raw_relay_url,
            "defaults_path": os.fspath(owner_path),
            "error": "relay token is required",
            "next_actions": [
                "Pass --token, or ensure .feishu_registry/_owner.json contains relay_token.",
            ],
        }, ensure_ascii=False, indent=2))
        return 1
    try:
        relay_url, relay_http_url = _normalize_relay_urls(
            raw_relay_url,
            raw_relay_http_url,
        )
    except ValueError as exc:
        print(json.dumps({
            "schema": "intern-agents.setup-connect-relay.v1",
            "ok": False,
            "relay_url": raw_relay_url,
            "defaults_path": os.fspath(owner_path),
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        return 1
    if not (owner_mobile or owner_open_id):
        report = {
            "schema": "intern-agents.setup-connect-relay.v1",
            "ok": False,
            "error": "owner identity is required",
            "defaults_path": os.fspath(owner_path),
            "next_actions": [
                "Pass --owner-mobile or --owner-open-id for this daemon machine, or add mobile/owner_open_id to .feishu_registry/_owner.json.",
            ],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    try:
        fetched = _fetch_daemon_policy(relay_http_url, token)
        daemon_policy = fetched.get("policy") if isinstance(fetched, dict) else None
        if not isinstance(daemon_policy, dict):
            raise RuntimeError("relay response missing policy object")
    except Exception as exc:
        print(json.dumps({
            "schema": "intern-agents.setup-connect-relay.v1",
            "ok": False,
            "relay_url": relay_url,
            "relay_http_url": relay_http_url,
            "error": str(exc),
        }, ensure_ascii=False, indent=2))
        return 1

    owner.update({
        "relay_url": relay_url,
        "relay_http_url": relay_http_url,
        "relay_token": token,
    })
    if owner_mobile:
        owner["mobile"] = owner_mobile
    if owner_open_id:
        owner["owner_open_id"] = owner_open_id
    if getattr(args, "machine_id", None):
        owner["machine_id"] = str(args.machine_id).strip()
    owner.pop("relay_ws_port", None)
    owner.pop("relay_http_port", None)

    policy_path = work_root / ".feishu_registry" / "enterprise_policy.json"
    _write_json_atomic(owner_path, owner)
    _write_json_atomic(policy_path, daemon_policy)
    report = {
        "schema": "intern-agents.setup-connect-relay.v1",
        "ok": True,
        "work_agents_root": os.fspath(work_root),
        "owner_path": os.fspath(owner_path),
        "daemon_policy_path": os.fspath(policy_path),
        "relay_url": relay_url,
        "relay_http_url": relay_http_url,
        "policy": {
            "schema": daemon_policy.get("schema", ""),
            "deployment_id": daemon_policy.get("deployment_id", ""),
        },
        "next_actions": ["Run `internctl setup apply --json --install-runtime` or use the setup GUI Apply button."],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_enterprise_export(args: argparse.Namespace) -> int:
    report = _enterprise_engine(args).export()
    if getattr(args, "output", None):
        write_export(report, args.output)
        report["export"]["output"] = args.output
    print_json_report(report)
    return 0 if report["ready"] else 1


# ── 校验项 ──────────────────────────────────────

class CheckItem:
    def __init__(self, name: str, category: str, check_fn, fix_fn=None, hint: str = ""):
        self.name = name
        self.category = category  # "manual" or "auto"
        self.check_fn = check_fn
        self.fix_fn = fix_fn
        self.hint = hint
        self.passed = False
        self.message = ""

    def check(self) -> bool:
        try:
            ok, msg = self.check_fn()
            self.passed = ok
            self.message = msg
            return ok
        except Exception as e:
            self.passed = False
            self.message = str(e)
            return False

    def fix(self) -> bool:
        if not self.fix_fn:
            return False
        try:
            ok, msg = self.fix_fn()
            self.message = msg
            if ok:
                self.passed = True
            return ok
        except Exception as e:
            self.message = f"修复失败: {e}"
            return False


def _check_work_agents_dir():
    p = WORK_AGENTS_ROOT
    if os.path.isdir(p) and os.access(p, os.W_OK):
        return True, f"{p} 存在且可写"
    return False, f"{p} 不存在或不可写"


def _check_github_ssh():
    try:
        r = subprocess.run(
            ["ssh", "-T", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "git@github.com"],
            capture_output=True, text=True, timeout=10
        )
        # ssh -T git@github.com returns 1 on success ("Hi xxx!")
        if r.returncode in (0, 1) and "successfully authenticated" in r.stderr.lower():
            return True, "SSH 认证成功"
        return False, f"SSH 认证失败 (exit {r.returncode}): {r.stderr[:100]}"
    except FileNotFoundError:
        return False, "ssh 命令不存在"
    except subprocess.TimeoutExpired:
        return False, "SSH 连接超时"


def _check_github_cli():
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return True, "gh 已认证"
        return False, f"gh 未认证: {r.stderr[:100]}"
    except FileNotFoundError:
        return False, "gh 命令不存在，请安装 GitHub CLI"
    except subprocess.TimeoutExpired:
        return False, "gh auth status 超时"


def _check_codeup_ssh():
    try:
        r = subprocess.run(
            ["ssh", "-T", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "git@codeup.aliyun.com"],
            capture_output=True, text=True, timeout=10
        )
        # codeup returns 0 on success
        if r.returncode == 0 or "welcome to codeup" in (r.stdout + r.stderr).lower():
            return True, "Codeup SSH 认证成功"
        return False, f"Codeup SSH 认证失败 (exit {r.returncode}): {(r.stdout + r.stderr)[:100]}"
    except FileNotFoundError:
        return False, "ssh 命令不存在"
    except subprocess.TimeoutExpired:
        return False, "SSH 连接超时"


def _detect_providers() -> set[str]:
    """从 .intern-config.json 检测项目使用的 provider 集合。"""
    config_path = os.path.join(SHARED_REPO, "workspace", ".intern-config.json")
    if not os.path.isfile(config_path):
        return {"github"}  # 默认
    try:
        with open(config_path) as f:
            data = json.load(f)
        providers = set()
        for proj in data.get("projects", []):
            p = proj.get("provider", "github")
            if p:
                providers.add(p)
        return providers if providers else {"github"}
    except (json.JSONDecodeError, OSError):
        return {"github"}


def _check_feishu_key():
    key_path = os.path.join(WORK_AGENTS_ROOT, "key.txt")
    if not os.path.isfile(key_path):
        return False, f"{key_path} 不存在"
    lines = [l.strip() for l in Path(key_path).read_text().splitlines() if l.strip()]
    if len(lines) < 2:
        return False, "key.txt 需要 2 行（第一行 app_id，第二行 app_secret）"
    # 实际调 API 验证
    import urllib.request
    import urllib.error
    app_id, app_secret = lines[0], lines[1]
    try:
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if data.get("code") == 0:
            return True, "飞书凭据有效（API 验证通过）"
        return False, f"飞书 API 返回错误: code={data.get('code')}, msg={data.get('msg','')}"
    except Exception as e:
        return False, f"飞书 API 调用失败: {e}"


def _check_feishu_owner():
    owner_path = os.path.join(FEISHU_REGISTRY_DIR, "_owner.json")
    if not os.path.isfile(owner_path):
        return False, "未获取 owner ID（请向飞书 BOT 发一条消息）"
    try:
        data = json.loads(Path(owner_path).read_text())
        if data.get("openId"):
            return True, f"owner ID: {data['openId'][:10]}..."
        return False, "_owner.json 缺少 openId"
    except Exception:
        return False, "_owner.json 格式错误"


def _check_shared_repo():
    git_dir = os.path.join(SHARED_REPO, ".git")
    if os.path.isdir(git_dir):
        return True, "共享 repo 已 clone"
    return False, f"{SHARED_REPO} 不存在"


def _fix_shared_repo():
    try:
        subprocess.run(["git", "clone", REPO_URL, SHARED_REPO], check=True, timeout=120)
        return True, "clone 完成"
    except Exception as e:
        return False, f"clone 失败: {e}"


def _check_shared_repo_uptodate():
    if not os.path.isdir(os.path.join(SHARED_REPO, ".git")):
        return False, "共享 repo 不存在"
    try:
        # Resolve default branch from origin/HEAD (typically origin/master or origin/main)
        head_ref = subprocess.check_output(
            ["git", "-C", SHARED_REPO, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        default_branch = head_ref.split("/", 1)[1] if "/" in head_ref else "master"
        subprocess.run(["git", "fetch", "origin"], cwd=SHARED_REPO, capture_output=True, timeout=30)
        r = subprocess.run(
            ["git", "rev-list", "--count", f"HEAD..origin/{default_branch}"],
            cwd=SHARED_REPO, capture_output=True, text=True, timeout=10
        )
        behind = int(r.stdout.strip() or "0")
        if behind == 0:
            return True, "已是最新"
        return False, f"落后 origin/{default_branch} {behind} 个 commit"
    except Exception as e:
        return False, f"检查失败: {e}"


def _fix_shared_repo_pull():
    try:
        subprocess.run(
            ["git", "checkout", "master"], cwd=SHARED_REPO, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash", "origin", "master"],
            cwd=SHARED_REPO, check=True, capture_output=True, timeout=60
        )
        sync_ok, sync_msg = _update_skill_sources_and_sync_interns()
        return sync_ok, f"pull 完成；{sync_msg}"
    except Exception as e:
        return False, f"pull 失败: {e}"


def _update_skill_sources_and_sync_interns() -> tuple[bool, str]:
    if not os.path.isdir(os.path.join(SHARED_REPO, ".git")):
        return False, "共享 repo 不存在，无法同步 skill sources"
    try:
        subprocess.run(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=SHARED_REPO, check=True, capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        return False, f"skill source submodule 更新失败: {e}"

    project = os.path.basename(SHARED_REPO.rstrip(os.sep))
    interns_root = os.path.join(SHARED_REPO, "workspace", "interns")
    if not os.path.isdir(interns_root):
        return True, "skill sources 已更新，无 intern 需要同步"

    try:
        from commands import skill as skill_cmd
        skill_cmd.WORK_AGENTS_ROOT = WORK_AGENTS_ROOT
    except Exception as e:
        return False, f"skill sync 模块加载失败: {e}"

    synced = 0
    failed: list[str] = []
    skipped = 0
    for entry in sorted(os.listdir(interns_root)):
        if entry.startswith("."):
            continue
        intern_project = os.path.join(WORK_AGENTS_ROOT, entry, project)
        if not os.path.isdir(intern_project):
            skipped += 1
            continue
        result = skill_cmd.skill_sync(entry, [project])
        if result.get("ok"):
            synced += 1
        else:
            errors = result.get("errors") or ["unknown error"]
            if any("only supported for claude/codex" in error for error in errors):
                skipped += 1
                continue
            failed.append(f"{entry}: {'; '.join(errors)}")

    if failed:
        return False, f"skill sources 已更新，{synced} 个 intern 同步成功，{len(failed)} 个失败: {failed[0]}"
    return True, f"skill sources 已更新，{synced} 个 intern 已同步，{skipped} 个未在本机参与该项目"


def _check_symlink(name: str, target: str):
    link_path = os.path.join(WORK_AGENTS_ROOT, name)
    if os.path.islink(link_path):
        actual = os.readlink(link_path)
        if actual == target:
            return True, f"{name} → {target}"
        return False, f"{name} 指向 {actual}（应为 {target}）"
    if os.path.exists(link_path):
        return False, f"{name} 存在但不是软链接"
    return False, f"{name} 不存在"


def _fix_symlink(name: str, target: str):
    link_path = os.path.join(WORK_AGENTS_ROOT, name)
    try:
        if os.path.islink(link_path):
            os.remove(link_path)
        elif os.path.exists(link_path):
            return False, f"{name} 不是软链接，请手动删除"
        os.symlink(target, link_path)
        return True, f"创建 {name} → {target}"
    except Exception as e:
        return False, f"创建失败: {e}"


def _check_log_dir():
    p = os.path.join(WORK_AGENTS_ROOT, "llm_intern_logs")
    if os.path.isdir(p):
        return True, "日志目录已存在"
    return False, "llm_intern_logs/ 不存在"


def _fix_log_dir():
    p = os.path.join(WORK_AGENTS_ROOT, "llm_intern_logs")
    os.makedirs(p, exist_ok=True)
    return True, "已创建"


_DAEMON_PIP_PACKAGES = ["websockets", "lark-oapi"]


def _check_daemon_pip_packages():
    missing = []
    for pkg in _DAEMON_PIP_PACKAGES:
        import_name = pkg.replace("-", "_")
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return True, "daemon 依赖已安装"
    return False, f"缺少 Python 包: {', '.join(missing)}"


def _fix_daemon_pip_packages():
    try:
        env = {**os.environ, "PIP_BREAK_SYSTEM_PACKAGES": "1"}
        subprocess.run(
            [sys.executable, "-m", "pip", "install", *_DAEMON_PIP_PACKAGES],
            check=True, capture_output=True, timeout=120, env=env,
        )
        return True, f"已安装 {', '.join(_DAEMON_PIP_PACKAGES)}"
    except Exception as e:
        return False, f"安装失败: {e}"


def build_checks() -> list[CheckItem]:
    providers = _detect_providers()
    checks = [
        CheckItem(f"{WORK_AGENTS_ROOT} 目录", "manual", _check_work_agents_dir,
                   hint=f"请创建 {WORK_AGENTS_ROOT} 目录并确保可写"),
    ]
    if "github" in providers:
        checks.append(CheckItem("GitHub SSH", "manual", _check_github_ssh,
                       hint="运行: ssh-keygen && gh ssh-key add ~/.ssh/id_rsa.pub"))
        checks.append(CheckItem("GitHub CLI", "manual", _check_github_cli,
                       hint="运行: gh auth login"))
    if "codeup" in providers:
        checks.append(CheckItem("Codeup SSH", "manual", _check_codeup_ssh,
                       hint="运行: ssh-keygen -t ed25519 && 在 Codeup 个人设置中添加 SSH 公钥"))
    checks.extend([
        CheckItem("飞书凭据", "manual", _check_feishu_key,
                   hint=f"将 app_id 和 app_secret 写入 {os.path.join(WORK_AGENTS_ROOT, 'key.txt')}"),
        CheckItem("飞书 Owner ID", "manual", _check_feishu_owner,
                   hint="向飞书 BOT APP 发任意消息，daemon 自动保存"),
        CheckItem("共享 repo", "auto", _check_shared_repo, _fix_shared_repo),
        CheckItem("共享 repo 版本", "auto", _check_shared_repo_uptodate, _fix_shared_repo_pull),
        CheckItem(".github 软链接", "auto",
                   lambda: _check_symlink(".github", "axis_intern_agents/.github"),
                   lambda: _fix_symlink(".github", "axis_intern_agents/.github")),
        CheckItem(".vscode 软链接", "auto",
                   lambda: _check_symlink(".vscode", "axis_intern_agents/shared-vscode-config"),
                   lambda: _fix_symlink(".vscode", "axis_intern_agents/shared-vscode-config")),
        CheckItem("日志目录", "auto", _check_log_dir, _fix_log_dir),
        CheckItem("Daemon Python 依赖", "auto", _check_daemon_pip_packages, _fix_daemon_pip_packages),
    ])
    return checks


def run(args: argparse.Namespace) -> int:
    if enterprise_mode_active():
        return emit_admin_rejection(
            "setup",
            detail="Use `internctl setup status|doctor|apply|export --json` for the enterprise setup contract.",
        )

    checks = build_checks()
    check_only = args.check
    auto_only = args.auto

    # Run checks
    for c in checks:
        c.check()

    if check_only:
        _print_report(checks)
        return 0 if all(c.passed for c in checks) else 1

    if auto_only:
        auto_checks = [c for c in checks if c.category == "auto" and not c.passed]
        if not auto_checks:
            print("✅ 自动配置项全部已通过")
            return 0
        for c in auto_checks:
            print(f"🔧 修复: {c.name} ...")
            c.fix()
            print(f"   {'✅' if c.passed else '❌'} {c.message}")
        return 0 if all(c.passed for c in checks if c.category == "auto") else 1

    # Default: check all + auto-fix
    _print_report(checks)

    manual_failed = [c for c in checks if c.category == "manual" and not c.passed]
    if manual_failed:
        print(f"\n⚠️ {len(manual_failed)} 项手动配置未完成：")
        for c in manual_failed:
            print(f"   • {c.name}: {c.hint}")
        print("\n请先完成手动配置，然后重新运行 internctl setup")
        return 1

    auto_failed = [c for c in checks if c.category == "auto" and not c.passed]
    if auto_failed:
        print(f"\n🔧 自动修复 {len(auto_failed)} 项...")
        for c in auto_failed:
            print(f"   修复: {c.name} ...")
            c.fix()
            print(f"   {'✅' if c.passed else '❌'} {c.message}")

    # Final report
    all_passed = all(c.passed for c in checks)
    if all_passed:
        print("\n🎉 环境校验全部通过！")
    else:
        print("\n❌ 仍有未通过项，请检查上方输出")
    return 0 if all_passed else 1


def _print_report(checks: list[CheckItem]):
    print("=" * 50)
    print("  Intern Agent 环境校验")
    print("=" * 50)
    print()
    print("【手动配置】")
    for c in checks:
        if c.category == "manual":
            icon = "✅" if c.passed else "❌"
            print(f"  {icon} {c.name}: {c.message}")
    print()
    print("【自动配置】")
    for c in checks:
        if c.category == "auto":
            icon = "✅" if c.passed else "❌"
            print(f"  {icon} {c.name}: {c.message}")
    print()
