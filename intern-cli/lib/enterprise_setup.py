"""Enterprise setup contract for user-side ``internctl setup`` commands.

This module is intentionally CLI-only.  VS Code setup surfaces can render the
JSON report and trigger actions, but policy interpretation and permission
boundaries live here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from lib.daemon_addr import daemon_addr_file
from lib.enterprise_policy import (
    POLICY_SCHEMA,
    POLICY_STATES,
    PolicyLoadResult,
    SecretLoadResult,
    default_policy_path,
    default_secret_path,
    load_enterprise_policy,
    load_enterprise_secrets,
    normalize_policy_state,
    redact_secrets,
    resolve_secret_value,
)

REPORT_SCHEMA = "intern-agents.enterprise-setup-report.v1"
FEISHU_RUNTIME_EVENTS = ["im.message.receive_v1", "card.action.trigger"]
BOOTSTRAP_POLICY_DEPLOYMENT_ID = "local-bootstrap"
BUILTIN_USER_BOOTSTRAP_POLICY_PATH = "<builtin:user-bootstrap>"


def _feishu_event_config_url(app_id: str) -> str:
    return f"https://open.feishu.cn/app/{quote(app_id, safe='')}/event" if app_id else ""


@dataclass
class EnterpriseSetupReport:
    command: str
    mode: str
    ready: bool
    policy: dict
    secrets: dict
    summary: dict
    checks: list[dict]
    next_actions: list[str]
    webview_contract: dict
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {
            "schema": REPORT_SCHEMA,
            "command": self.command,
            "mode": self.mode,
            "ready": self.ready,
            "policy": self.policy,
            "secrets": self.secrets,
            "summary": self.summary,
            "checks": self.checks,
            "next_actions": self.next_actions,
            "webview_contract": self.webview_contract,
        }
        data.update(self.extra)
        return redact_secrets(data)


@dataclass
class SetupCheck:
    id: str
    scope: str
    label: str
    policy_state: str
    status: str
    passed: bool
    blocking: bool
    user_actionable: bool
    admin_managed: bool
    code: str
    message: str
    hint: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "label": self.label,
            "policy_state": self.policy_state,
            "status": self.status,
            "passed": self.passed,
            "blocking": self.blocking,
            "user_actionable": self.user_actionable,
            "admin_managed": self.admin_managed,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
        }


class EnterpriseSetupEngine:
    def __init__(
        self,
        work_root: str | os.PathLike[str] | None = None,
        policy_path: str | os.PathLike[str] | None = None,
        secret_path: str | os.PathLike[str] | None = None,
        home: str | os.PathLike[str] | None = None,
        command_runner=None,
        urlopen=None,
    ):
        self.home = Path(home or os.environ.get("HOME") or Path.home())
        self.work_root = Path(work_root or os.environ.get("WORK_AGENTS_ROOT") or "/work-agents")
        self._policy_explicit = bool(
            policy_path
            or os.environ.get("INTERN_ENTERPRISE_POLICY")
            or os.environ.get("INTERN_ENTERPRISE_POLICY_PATH")
            or os.environ.get("ENTERPRISE_POLICY_PATH")
        )
        self.policy_path = Path(policy_path) if policy_path else default_policy_path(self.work_root)
        self.secret_path = Path(secret_path) if secret_path else default_secret_path(self.work_root)
        self.command_runner = command_runner or subprocess.run
        self.urlopen = urlopen or urllib.request.urlopen
        self._policy = load_enterprise_policy(self.policy_path)
        if self._policy.state == "missing" and not self._policy_explicit:
            self._policy = PolicyLoadResult(
                "builtin_user_bootstrap",
                BUILTIN_USER_BOOTSTRAP_POLICY_PATH,
                data=self._bootstrap_policy_data(),
            )
        self._secrets = load_enterprise_secrets(self.secret_path, required=self._policy_requires_secret_bundle())

    @property
    def policy(self) -> PolicyLoadResult:
        return self._policy

    @property
    def secrets(self) -> SecretLoadResult:
        return self._secrets

    def status(self) -> dict:
        checks = self._build_checks(deep=False, apply=False)
        return self._report("status", checks)

    def doctor(self) -> dict:
        checks = self._build_checks(deep=True, apply=False)
        return self._report("doctor", checks)

    def apply(self, *, install_runtime: bool = False) -> dict:
        checks = self._build_checks(deep=True, apply=True, install_runtime=install_runtime)
        applied_actions = [item.details for item in checks if item.status == "applied"]
        report = self._report("apply", checks)
        report["applied_actions"] = applied_actions
        report["install_runtime"] = bool(install_runtime)
        return report

    def export(self) -> dict:
        report = self.doctor()
        report["command"] = "export"
        report["export"] = {
            "redacted": True,
            "contains_secrets": False,
        }
        return report

    def _build_checks(self, *, deep: bool, apply: bool, install_runtime: bool = False) -> list[SetupCheck]:
        checks = [self._policy_check()]
        if not self.policy.ok:
            return checks
        if self.policy.state == "builtin_user_bootstrap":
            checks.extend([
                self._work_root_check(apply=apply),
                self._relay_connection_bootstrap_check(),
            ])
            return checks

        checks.extend([
            self._work_root_check(apply=apply),
            self._log_dir_check(apply=apply),
            self._hook_env_check(apply=apply),
            self._secret_bundle_check(),
        ])
        if self._capability_state("feishu") in {"required", "admin_only"}:
            checks.append(self._feishu_owner_config_check(apply=apply))

        if self._requires_terminal_runtime():
            checks.append(self._tmux_check(apply=apply and install_runtime))
        if self._requires_python_runtime():
            checks.append(self._python_deps_check(apply=apply and install_runtime))

        for capability in sorted(self._capabilities()):
            state = self._capability_state(capability)
            if capability in {"codeup", "github", "gitlab"}:
                checks.extend(self._provider_checks(capability, state, deep=deep, apply=apply))
            elif capability == "feishu":
                checks.extend(self._feishu_checks(state, deep=deep))
            elif capability in {"claude", "codex", "copilot"}:
                checks.extend(self._agent_checks(capability, state, deep=deep, apply=apply and install_runtime))
            elif capability == "workspace":
                checks.extend(self._workspace_checks(state))
            else:
                checks.append(self._unknown_capability_check(capability, state))

        if "workspace" not in self._capabilities():
            checks.extend(self._workspace_checks("optional"))
        if self.policy.state == "builtin_user_bootstrap" or self._is_daemon_policy():
            checks.append(self._daemon_local_config_check())
        checks.append(self._daemon_check(deep=deep))
        return checks

    def _policy_check(self) -> SetupCheck:
        if self.policy.ok:
            is_builtin = self.policy.state == "builtin_user_bootstrap"
            return SetupCheck(
                "policy.loaded",
                "policy",
                "Enterprise policy" if not is_builtin else "User bootstrap policy",
                "required" if not is_builtin else "optional",
                "ok",
                True,
                False,
                False if is_builtin else False,
                False if is_builtin else True,
                "OK",
                "using built-in user bootstrap policy; no local policy file is required"
                if is_builtin else f"loaded {self.policy.path}",
                details={
                    "schema": self.policy.data.get("schema", ""),
                    "deployment_id": self.policy.data.get("deployment_id", ""),
                    "builtin": is_builtin,
                },
            )
        status = "missing" if self.policy.state == "missing" else "failed"
        user_actionable = status == "missing" and not self._policy_explicit
        return SetupCheck(
            "policy.loaded",
            "policy",
            "Enterprise policy",
            "required",
            status,
            False,
            True,
            user_actionable,
            not user_actionable,
            "POLICY_MISSING" if status == "missing" else "POLICY_INVALID",
            self.policy.error or f"policy not found at {self.policy.path}",
            "Run `internctl setup apply --json` to create a local bootstrap policy, or ask the enterprise administrator for a policy bundle."
            if user_actionable else
            "Ask the enterprise administrator for the policy bundle path.",
        )

    def _ensure_bootstrap_policy(self) -> None:
        return

    def _bootstrap_policy_data(self) -> dict:
        return {
            "schema": POLICY_SCHEMA,
            "deployment_id": BOOTSTRAP_POLICY_DEPLOYMENT_ID,
            "capabilities": {
                "codeup": "disabled",
                "github": "disabled",
                "workspace": "disabled",
                "codex": "disabled",
                "claude": "disabled",
                "copilot": "disabled",
                "feishu": "disabled",
            },
            "codeup": {
                "access_token_env": "CODEUP_ACCESS_TOKEN",
            },
            "workspace": {
                "allowed_modes": ["repo_dotdir", "metadata_branch", "local_only"],
                "default_mode": "metadata_branch",
                "metadata_branch": "intern_workspace",
            },
        }

    def _is_daemon_policy(self) -> bool:
        return bool(self.policy.data.get("role") == "daemon" or self.policy.data.get("daemon_policy") is True)

    def _relay_connection_bootstrap_check(self) -> SetupCheck:
        owner_path = self.work_root / ".feishu_registry" / "_owner.json"
        daemon_policy_path = self.work_root / ".feishu_registry" / "enterprise_policy.json"
        owner = self._read_json_file(owner_path)
        missing = []
        if not owner.get("relay_url"):
            missing.append("relay_url")
        if not owner.get("relay_token"):
            missing.append("relay_token")
        if not (owner.get("owner_open_id") or owner.get("open_id") or owner.get("mobile")):
            missing.append("owner_identity")
        if not daemon_policy_path.is_file():
            missing.append("daemon_policy")
        ok = not missing
        return SetupCheck(
            "relay.connection",
            "relay",
            "Enterprise relay connection",
            "required",
            "ok" if ok else "missing",
            ok,
            not ok,
            True,
            False,
            "OK" if ok else "RELAY_CONNECTION_REQUIRED",
            "relay connection and daemon policy are configured"
            if ok else "connect to the enterprise relay before continuing setup",
            "Run `internctl setup connect-relay --json`; pass --relay-url/--token/--owner-mobile only when they are not already in .feishu_registry/_owner.json.",
            {
                "owner_path": str(owner_path),
                "daemon_policy_path": str(daemon_policy_path),
                "missing": missing,
            },
        )

    def _work_root_check(self, *, apply: bool) -> SetupCheck:
        if apply:
            self.work_root.mkdir(parents=True, exist_ok=True)
        ok = self.work_root.is_dir() and os.access(self.work_root, os.W_OK)
        status = "ok" if ok else "missing"
        return SetupCheck(
            "core.work_root",
            "core",
            "Work agents root",
            "required",
            status,
            ok,
            not ok,
            True,
            False,
            "OK" if ok else "WORK_ROOT_MISSING",
            f"{self.work_root} is writable" if ok else f"{self.work_root} is missing or not writable",
            f"Create {self.work_root} or run `internctl setup apply --json`.",
        )

    def _log_dir_check(self, *, apply: bool) -> SetupCheck:
        log_dir = self.work_root / "llm_intern_logs"
        applied = False
        if apply and not log_dir.exists():
            log_dir.mkdir(parents=True, exist_ok=True)
            applied = True
        ok = log_dir.is_dir()
        return SetupCheck(
            "core.log_dir",
            "core",
            "Log directory",
            "required",
            "applied" if applied else ("ok" if ok else "missing"),
            ok,
            not ok,
            True,
            False,
            "OK" if ok else "LOG_DIR_MISSING",
            "llm_intern_logs exists" if ok else "llm_intern_logs is missing",
            "Run `internctl setup apply --json`.",
            {"action": "created_log_dir", "path": str(log_dir)} if applied else {},
        )

    def _hook_env_check(self, *, apply: bool) -> SetupCheck:
        target = self.work_root / ".github"
        source = self._bundled_hooks_dir()
        applied = False
        if apply and source and source.is_dir():
            self._copy_hook_env(source, target)
            applied = True
        ok = (
            (target / "hooks" / "user_prompt_hook.py").is_file()
            and (target / "hooks" / "stop_hook.py").is_file()
            and (target / "codex_settings.toml").is_file()
        )
        return SetupCheck(
            "core.hook_env",
            "core",
            "Hook runtime files",
            "required",
            "applied" if applied and ok else ("ok" if ok else "missing"),
            ok,
            not ok,
            True,
            False,
            "OK" if ok else "HOOK_ENV_MISSING",
            f"hook runtime files ready at {target}" if ok else f"hook runtime files missing under {target}",
            "Run `internctl setup apply --json` from the bundled CLI.",
            {"action": "synced_hook_env", "path": str(target)} if applied and ok else {},
        )

    def _secret_bundle_check(self) -> SetupCheck:
        if self._is_daemon_policy():
            return SetupCheck(
                "secrets.loaded",
                "secrets",
                "Enterprise secret bundle",
                "admin_only",
                "skipped",
                True,
                False,
                False,
                True,
                "DAEMON_POLICY_NO_LOCAL_SECRETS",
                "daemon policy uses relay-held secrets; no local secret bundle is required",
                "",
                {"path": self.secrets.path, "redacted": True},
            )
        required = self._policy_requires_secret_bundle()
        if self.secrets.ok:
            return SetupCheck(
                "secrets.bundle",
                "policy",
                "Enterprise secret bundle",
                "admin_only",
                "admin_managed",
                True,
                False,
                False,
                True,
                "OK",
                f"loaded {self.secrets.path}",
                details={"path": self.secrets.path, "redacted": True},
            )
        if self.secrets.state == "missing_optional":
            return SetupCheck(
                "secrets.bundle",
                "policy",
                "Enterprise secret bundle",
                "optional",
                "missing",
                True,
                False,
                False,
                True,
                "SECRET_BUNDLE_MISSING_OPTIONAL",
                self.secrets.error,
                "Secret bundle is optional for this policy.",
                {"path": self.secrets.path, "redacted": True},
            )
        return SetupCheck(
            "secrets.bundle",
            "policy",
            "Enterprise secret bundle",
            "admin_only",
            "admin_action_required",
            False,
            required,
            False,
            True,
            "SECRET_BUNDLE_PERMISSION_INVALID" if self.secrets.state == "invalid_permissions" else "SECRET_BUNDLE_MISSING",
            self.secrets.error,
            "Ask the enterprise administrator to install the secret bundle with 0600 permissions.",
            {"path": self.secrets.path, "redacted": True},
        )

    def _feishu_owner_config_check(self, *, apply: bool) -> SetupCheck:
        feishu = self.policy.data.get("feishu", {}) if self.policy.ok else {}
        relay_url = str(feishu.get("relay_url") or "")
        owner_mobile = str(feishu.get("owner_mobile") or feishu.get("mobile") or "")
        owner_open_id = str(feishu.get("owner_open_id") or "")
        relay_token = self._secret_value("relay.token")
        owner_path = self.work_root / ".feishu_registry" / "_owner.json"
        applied = False
        if apply and relay_url and relay_token and (owner_mobile or owner_open_id):
            owner_path.parent.mkdir(parents=True, exist_ok=True)
            existing = self._read_json_file(owner_path)
            data = dict(existing)
            data["relay_url"] = relay_url
            data["relay_token"] = relay_token
            if owner_mobile:
                data["mobile"] = owner_mobile
            if owner_open_id:
                data["owner_open_id"] = owner_open_id
                data["open_id"] = owner_open_id
            if data != existing:
                owner_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                try:
                    owner_path.chmod(0o600)
                except OSError:
                    pass
                applied = True

        current = self._read_json_file(owner_path)
        ok = bool(
            current.get("relay_url")
            and current.get("relay_token")
            and (current.get("owner_open_id") or current.get("open_id") or current.get("mobile"))
        )
        missing = []
        if not current.get("relay_url"):
            missing.append("relay_url")
        if not current.get("relay_token"):
            missing.append("relay_token")
        if not (current.get("owner_open_id") or current.get("open_id") or current.get("mobile")):
            missing.append("owner_identity")
        return SetupCheck(
            "feishu.local_owner_config",
            "feishu",
            "Feishu local owner config",
            self._capability_state("feishu"),
            "applied" if applied and ok else ("ok" if ok else "missing"),
            ok,
            not ok,
            False,
            True,
            "OK" if ok else "FEISHU_LOCAL_OWNER_CONFIG_MISSING",
            f"{owner_path} is ready" if ok else "local _owner.json is missing required relay or owner fields",
            "Ask the enterprise administrator to provide relay.token and feishu.owner_open_id or feishu.owner_mobile in the policy bundle.",
            {
                "path": str(owner_path),
                "missing_fields": missing,
                "has_owner_open_id": bool(current.get("owner_open_id") or current.get("open_id")),
                "has_owner_mobile": bool(current.get("mobile")),
                "action": "wrote_owner_json" if applied else "",
            },
        )

    def _secret_value(self, key: str) -> str:
        if not self.secrets.ok:
            return ""
        entry = self.secrets.data.get("secrets", {}).get(key)
        return resolve_secret_value(entry) if isinstance(entry, dict) else ""

    def _read_json_file(self, path: Path) -> dict:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    def _provider_checks(self, provider: str, state: str, *, deep: bool, apply: bool) -> list[SetupCheck]:
        if state == "disabled":
            return [self._disabled_check(provider, f"{provider.title()} provider")]
        if state == "admin_only":
            return [self._admin_only_check(provider, f"{provider.title()} provider")]

        checks: list[SetupCheck] = []
        if provider == "codeup":
            token_env = str(self.policy.data.get("codeup", {}).get("access_token_env") or "CODEUP_ACCESS_TOKEN")
            token_ok = bool(os.environ.get(token_env))
            checks.append(self._plain_check(
                "codeup.token",
                "codeup",
                "Codeup access token",
                state,
                token_ok,
                "OK" if token_ok else "CODEUP_TOKEN_MISSING",
                f"{token_env} is set" if token_ok else f"{token_env} is not set",
                f"Create a Codeup personal access token in the console and export {token_env}.",
                user_actionable=True,
            ))
            checks.append(self._ssh_check(
                "codeup.ssh",
                "codeup",
                "Codeup SSH",
                state,
                "git@codeup.aliyun.com",
                deep=deep,
                success_markers=["welcome to codeup"],
                hint="Add your SSH public key in Codeup personal settings.",
            ))
            checks.append(self._python_cli_wrapper_check(
                "codeup.pr_cli",
                "codeup",
                "Codeup MR CLI",
                state,
                "codeup_pr",
                self._bundled_cli_file("codeup_pr.py"),
                deep=deep,
                apply=apply,
                hint="Run `internctl setup apply --json`; interns use `codeup_pr` from PATH in the merge playbook.",
            ))
        elif provider == "github":
            checks.append(self._command_check(
                "github.cli",
                "github",
                "GitHub CLI",
                state,
                ["gh", "auth", "status", "--hostname", "github.com"],
                deep=deep,
                missing_command="gh",
                hint="Run `gh auth login --hostname github.com`.",
            ))
            checks.append(self._ssh_check(
                "github.ssh",
                "github",
                "GitHub SSH",
                state,
                "git@github.com",
                deep=deep,
                success_markers=["successfully authenticated"],
                hint="Upload an SSH public key to GitHub.",
            ))
        elif provider == "gitlab":
            checks.append(self._command_check(
                "gitlab.cli",
                "gitlab",
                "GitLab CLI",
                state,
                ["glab", "auth", "status"],
                deep=deep,
                missing_command="glab",
                hint="Run `glab auth login` or configure a GitLab token.",
            ))
            checks.append(self._ssh_check(
                "gitlab.ssh",
                "gitlab",
                "GitLab SSH",
                state,
                "git@gitlab.com",
                deep=deep,
                success_markers=["welcome to gitlab"],
                hint="Add your SSH public key to GitLab.",
            ))
        return checks

    def _feishu_checks(self, state: str, *, deep: bool) -> list[SetupCheck]:
        if state == "disabled":
            return [self._disabled_check("feishu", "Feishu")]
        feishu = self.policy.data.get("feishu", {}) if self.policy.ok else {}
        required_fields = ["relay_url", "app_id"]
        missing = [field for field in required_fields if not feishu.get(field)]
        checks = [
            SetupCheck(
                "feishu.enterprise_config",
                "feishu",
                "Feishu enterprise configuration",
                state,
                "admin_action_required" if missing else "admin_managed",
                not missing,
                bool(missing and state in {"required", "admin_only"}),
                False,
                True,
                "FEISHU_ADMIN_CONFIG_MISSING" if missing else "OK",
                "missing admin-managed fields: " + ", ".join(missing) if missing else "admin-managed Feishu fields are present",
                "Ask the enterprise administrator to update the policy bundle." if missing else "",
                {"missing_fields": missing},
            )
        ]
        if state in {"required", "admin_only"} and not self._is_daemon_policy():
            app_secret_ok = self.secrets.has_secret("feishu.app_secret")
            checks.append(SetupCheck(
                "feishu.app_secret",
                "feishu",
                "Feishu app secret",
                "admin_only",
                "admin_managed" if app_secret_ok else "admin_action_required",
                app_secret_ok,
                not app_secret_ok,
                False,
                True,
                "OK" if app_secret_ok else "FEISHU_APP_SECRET_MISSING",
                "Feishu app secret is present in the enterprise secret bundle" if app_secret_ok else "Feishu app secret is missing from the enterprise secret bundle",
                "" if app_secret_ok else "Ask the enterprise administrator to update the secret bundle.",
                {"secret_key": "feishu.app_secret", "redacted": True},
            ))
        checks.append(self._relay_endpoint_check(state, feishu, deep=deep))
        if deep and state in {"required", "admin_only"}:
            checks.append(self._feishu_inbound_check(state, feishu))
        return checks

    def _relay_endpoint_check(self, state: str, feishu: dict, *, deep: bool) -> SetupCheck:
        health_url = feishu.get("relay_health_url") or feishu.get("relay_http_url")
        relay_url = str(feishu.get("relay_url") or "")
        if not relay_url:
            return self._plain_check(
                "relay.endpoint",
                "relay",
                "Enterprise relay endpoint",
                state,
                False,
                "RELAY_ENDPOINT_MISSING",
                "relay_url is missing from policy",
                "Ask the enterprise administrator to provide relay_url.",
                admin_managed=True,
                user_actionable=False,
            )
        if not deep or not health_url:
            return SetupCheck(
                "relay.endpoint",
                "relay",
                "Enterprise relay endpoint",
                state,
                "configured",
                True,
                False,
                False,
                True,
                "OK",
                "relay endpoint is configured; no health URL was probed" if not health_url else "relay endpoint is configured",
                "",
                {"relay_url": _redact_url(relay_url), "health_url": _redact_url(str(health_url)) if health_url else ""},
            )
        try:
            with self.urlopen(str(health_url), timeout=3) as resp:
                body = resp.read(4096)
            ok = bool(body)
        except Exception as exc:
            ok = False
            body = str(exc).encode()
        return self._plain_check(
            "relay.endpoint",
            "relay",
            "Enterprise relay endpoint",
            state,
            ok,
            "OK" if ok else "RELAY_UNREACHABLE",
            "relay health endpoint responded" if ok else body.decode(errors="ignore")[:220],
            "Check network/VPN or ask the administrator to verify relay health.",
            admin_managed=True,
            user_actionable=False,
        )

    def _feishu_inbound_check(self, state: str, feishu: dict) -> SetupCheck:
        health_url = feishu.get("relay_health_url") or feishu.get("relay_http_url")
        app_id = str(feishu.get("app_id") or "")
        event_config_url = _feishu_event_config_url(app_id)
        blocking = state in {"required", "admin_only"}
        event_details = {
            "required_events": FEISHU_RUNTIME_EVENTS,
            "connection": "long_connection",
            "event_config_url": event_config_url,
        }
        if not health_url:
            return SetupCheck(
                "feishu.inbound",
                "feishu",
                "Feishu inbound events",
                "admin_only",
                "unverified",
                False,
                blocking,
                False,
                True,
                "FEISHU_INBOUND_HEALTH_URL_MISSING",
                "relay health URL is missing; cannot verify Feishu inbound events",
                "Ask the enterprise administrator to provide relay_health_url and verify long-connection event subscriptions.",
                event_details,
            )
        try:
            with self.urlopen(str(health_url), timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as exc:
            return SetupCheck(
                "feishu.inbound",
                "feishu",
                "Feishu inbound events",
                "admin_only",
                "failed",
                False,
                blocking,
                False,
                True,
                "FEISHU_INBOUND_STATUS_UNREACHABLE",
                f"cannot read relay Feishu status: {exc}",
                "Check network/VPN or ask the administrator to verify relay health.",
                {"health_url": _redact_url(str(health_url))},
            )
        ws_connected = bool(data.get("feishu_ws_connected"))
        msg_count = int(data.get("feishu_msg_count") or 0)
        im_message_count = int(data.get("feishu_im_message_count") or msg_count or 0)
        card_action_count = int(data.get("feishu_card_action_count") or 0)
        last_msg_ago = data.get("feishu_last_msg_ago")
        details = {
            "health_url": _redact_url(str(health_url)),
            "feishu_ws_connected": ws_connected,
            "feishu_msg_count": msg_count,
            "feishu_im_message_count": im_message_count,
            "feishu_card_action_count": card_action_count,
            "feishu_last_msg_ago": last_msg_ago,
            "feishu_card_action_last_ago": data.get("feishu_card_action_last_ago"),
            **event_details,
        }
        if not ws_connected:
            return SetupCheck(
                "feishu.inbound",
                "feishu",
                "Feishu inbound events",
                "admin_only",
                "failed",
                False,
                blocking,
                False,
                True,
                "FEISHU_WS_DISCONNECTED",
                "relay is not connected to Feishu WebSocket",
                "Ask the enterprise administrator to check App ID/Secret and relay logs.",
                details,
            )
        if msg_count <= 0:
            return SetupCheck(
                "feishu.inbound",
                "feishu",
                "Feishu inbound events",
                "admin_only",
                "unverified",
                False,
                blocking,
                False,
                True,
                "FEISHU_INBOUND_UNVERIFIED",
                "relay WebSocket is connected but no real Feishu message event has been received",
                "Send a message to the main bot. If this count does not increase, ask the administrator to configure long-connection event subscriptions for im.message.receive_v1 and card.action.trigger, then publish/approve the app."
                + (f" Event config: {event_config_url}" if event_config_url else ""),
                details,
            )
        if im_message_count <= 0 or card_action_count <= 0:
            return SetupCheck(
                "feishu.inbound",
                "feishu",
                "Feishu inbound events",
                "admin_only",
                "partial",
                False,
                blocking,
                False,
                True,
                "FEISHU_CARD_ACTION_UNVERIFIED" if card_action_count <= 0 else "FEISHU_MESSAGE_INBOUND_UNVERIFIED",
                "relay has received Feishu text messages, but card callbacks have not been observed" if card_action_count <= 0 else "relay has received Feishu callbacks, but text messages have not been observed",
                "Click a bot card such as `/helper start`. If the click reports an error and this count stays 0, ask the administrator to configure long-connection event subscription card.action.trigger, then publish/approve the app."
                + (f" Event config: {event_config_url}" if event_config_url else ""),
                details,
            )
        return SetupCheck(
            "feishu.inbound",
            "feishu",
            "Feishu inbound events",
            "admin_only",
            "ok",
            True,
            False,
            False,
            True,
            "OK",
            "relay has received Feishu message and card callback events",
            "",
            details,
        )

    def _agent_checks(self, provider: str, state: str, *, deep: bool, apply: bool = False) -> list[SetupCheck]:
        if state == "disabled":
            return [self._disabled_check(provider, f"{provider.title()} agent")]
        if provider == "copilot":
            return [SetupCheck(
                "agent.copilot_auth",
                "agent",
                "Copilot authentication",
                state,
                "vscode_only",
                state != "required",
                state == "required",
                True,
                False,
                "COPILOT_VSCODE_ONLY",
                "Copilot authentication is verified by VS Code; CLI can only report this boundary.",
                "Use VS Code GitHub Copilot sign-in, or mark copilot optional/disabled in policy.",
            )]

        command = self._agent_command(provider)
        version_args = self._agent_version_args(provider)
        if apply and state == "required" and provider == "codex" and not shutil.which(command):
            self._install_codex_cli()
        cli_check = self._command_check(
            f"agent.{provider}_cli",
            "agent",
            f"{provider.title()} CLI",
            state,
            [command, *version_args],
            deep=deep,
            missing_command=command,
            hint=f"Install {provider.title()} CLI.",
        )
        auth_check = self._agent_auth_check(provider, state)
        return [cli_check, auth_check]

    def _tmux_check(self, *, apply: bool) -> SetupCheck:
        if apply and not shutil.which("tmux"):
            self._install_tmux()
        if not shutil.which("tmux"):
            return self._plain_check(
                "runtime.tmux",
                "runtime",
                "tmux",
                "required",
                False,
                "COMMAND_MISSING",
                "tmux is not installed",
                "Run `internctl setup apply --install-runtime --json`.",
            )
        try:
            result = self.command_runner(["tmux", "-V"], capture_output=True, text=True, timeout=10)
            ok = result.returncode == 0
            message = _combined_output(result) if not ok else (_combined_output(result) or "tmux is installed")
        except Exception as exc:
            ok = False
            message = str(exc)
        check = self._plain_check(
            "runtime.tmux",
            "runtime",
            "tmux",
            "required",
            ok,
            "OK" if ok else "COMMAND_FAILED",
            message,
            "Install tmux or run `internctl setup apply --install-runtime --json`.",
        )
        if apply and ok:
            check.status = "applied"
            check.details = {"action": "verified_tmux"}
        return check

    def _python_deps_check(self, *, apply: bool) -> SetupCheck:
        missing = self._missing_python_deps()
        if apply and missing:
            self._install_python_deps(missing)
            missing = self._missing_python_deps()
        ok = not missing
        check = self._plain_check(
            "runtime.python_deps",
            "runtime",
            "Daemon Python dependencies",
            "required",
            ok,
            "OK" if ok else "PYTHON_DEPS_MISSING",
            "websockets and lark-oapi are importable" if ok else "missing Python packages: " + ", ".join(missing),
            "Run `internctl setup apply --install-runtime --json`.",
        )
        if apply and ok:
            check.status = "applied"
            check.details = {"action": "verified_python_deps"}
        return check

    def _agent_auth_check(self, provider: str, state: str) -> SetupCheck:
        if provider == "codex":
            auth_path = self.home / ".codex" / "auth.json"
            ok = False
            try:
                data = json.loads(auth_path.read_text(encoding="utf-8"))
                ok = bool(data.get("tokens", {}).get("access_token"))
            except Exception:
                ok = False
            return self._plain_check(
                "agent.codex_auth",
                "agent",
                "Codex auth",
                state,
                ok,
                "OK" if ok else "CODEX_AUTH_MISSING",
                "Codex auth token found" if ok else "Codex auth token missing",
                "Run `codex login`.",
            )
        auth_path = self.home / ".claude.json"
        token_envs = self._agent_env_names("claude", "access_token_env", ["ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"])
        secret_key = str(self._agent_config("claude").get("access_token_secret") or "claude.access_token")
        env_ok = any(os.environ.get(name) for name in token_envs)
        secret_ok = self.secrets.has_secret(secret_key)
        file_ok = False
        try:
            file_ok = _object_has_auth(json.loads(auth_path.read_text(encoding="utf-8")))
        except Exception:
            file_ok = False
        ok = env_ok or file_ok or secret_ok
        check = self._plain_check(
            "agent.claude_auth",
            "agent",
            "Claude auth",
            state,
            ok,
            "OK" if ok else "CLAUDE_AUTH_MISSING",
            "Claude auth token found" if ok else f"Claude access token/auth config missing from {', '.join(token_envs)} or {secret_key}",
            "Run `claude` to sign in or provide the enterprise-approved access token through policy/user config.",
        )
        check.details = {
            "env_names": token_envs,
            "secret_key": secret_key,
            "home_config": str(auth_path),
            "policy_configurable": True,
        }
        return check

    def _agent_config(self, provider: str) -> dict:
        raw = self.policy.data.get(provider, {}) if self.policy.ok else {}
        return raw if isinstance(raw, dict) else {}

    def _agent_command(self, provider: str) -> str:
        config = self._agent_config(provider)
        default = "codex" if provider == "codex" else "claude"
        command = str(config.get("command") or config.get("cli") or default).strip()
        return command or default

    def _agent_version_args(self, provider: str) -> list[str]:
        config = self._agent_config(provider)
        raw = config.get("version_args")
        if isinstance(raw, list):
            args = [str(item) for item in raw if str(item)]
            return args or ["--version"]
        if isinstance(raw, str) and raw.strip():
            return raw.split()
        return ["--version"]

    def _agent_env_names(self, provider: str, key: str, defaults: list[str]) -> list[str]:
        config = self._agent_config(provider)
        raw = config.get(key)
        names: list[str] = []
        if isinstance(raw, list):
            names = [str(item).strip() for item in raw if str(item).strip()]
        elif isinstance(raw, str) and raw.strip():
            names = [raw.strip()]
        return names or defaults

    def _workspace_checks(self, state: str) -> list[SetupCheck]:
        workspace = self.policy.data.get("workspace", {}) if self.policy.ok else {}
        return [
            SetupCheck(
                "workspace.contract",
                "workspace",
                "Workspace contract adapter",
                state,
                "adapter_pending",
                True,
                False,
                False,
                True,
                "WORKSPACE_CONTRACT_PENDING",
                "workspace enable/mode will be delegated to task342 relay/daemon contract",
                "",
                {
                    "allowed_modes": workspace.get("allowed_modes", []),
                    "default_mode": workspace.get("default_mode", ""),
                    "metadata_branch": workspace.get("metadata_branch", ""),
                },
            )
        ]

    def _daemon_check(self, *, deep: bool) -> SetupCheck:
        daemon_pid_file = Path(daemon_addr_file(self.work_root))
        if not daemon_pid_file.exists():
            return SetupCheck(
                "daemon.status",
                "daemon",
                "Local daemon",
                "required",
                "pending_activation",
                True,
                False,
                False,
                False,
                "DAEMON_PENDING_ACTIVATION",
                "daemon is not running yet; VS Code starts it after setup checks pass",
                "Reload VS Code after setup is ready.",
            )
        try:
            info = json.loads(daemon_pid_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return self._plain_check(
                "daemon.status",
                "daemon",
                "Local daemon",
                "required",
                False,
                "DAEMON_PID_INVALID",
                str(exc),
                f"Remove stale {daemon_pid_file} and restart daemon.",
            )
        port = info.get("http_port")
        if not deep:
            return self._plain_check(
                "daemon.status",
                "daemon",
                "Local daemon",
                "required",
                bool(port),
                "OK" if port else "DAEMON_PORT_MISSING",
                f"daemon pid file present on port {port}" if port else "daemon pid file has no http_port",
                "Restart daemon.",
            )
        try:
            with self.urlopen(f"http://localhost:{int(port)}/api/status", timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ok = data.get("running") is True
            msg = "daemon status endpoint is healthy" if ok else "daemon status did not report running=true"
        except Exception as exc:
            ok = False
            data = {}
            msg = str(exc)
        check = self._plain_check(
            "daemon.status",
            "daemon",
            "Local daemon",
            "required",
            ok,
            "OK" if ok else "DAEMON_UNREACHABLE",
            msg,
            "Restart daemon from VS Code or the enterprise daemon service.",
        )
        check.details = {"pid": info.get("pid"), "http_port": port, "status": data}
        return check

    def _daemon_local_config_check(self) -> SetupCheck:
        owner_path = self.work_root / ".feishu_registry" / "_owner.json"
        missing: list[str] = []
        owner = self._read_json_file(owner_path)
        if not owner:
            missing.append("_owner.json")
        else:
            if not owner.get("relay_url"):
                missing.append("relay_url")
            if not owner.get("relay_token"):
                missing.append("relay_token")
            if not (owner.get("owner_open_id") or owner.get("open_id") or owner.get("mobile")):
                missing.append("owner_identity")
        ok = not missing
        return SetupCheck(
            "daemon.local_config",
            "daemon",
            "Local daemon config",
            "required",
            "ok" if ok else "missing",
            ok,
            not ok,
            True,
            False,
            "OK" if ok else "DAEMON_LOCAL_CONFIG_MISSING",
            "daemon local config is ready"
            if ok else "missing daemon local config: " + ", ".join(missing),
            "Run setup connect-relay, then rerun setup doctor.",
            {
                "owner_path": str(owner_path),
                "missing": missing,
                "current_architecture_requires_key_txt": False,
            },
        )

    def _unknown_capability_check(self, capability: str, state: str) -> SetupCheck:
        return SetupCheck(
            f"{capability}.unknown",
            capability,
            capability,
            state,
            "skipped",
            state != "required",
            state == "required",
            False,
            False,
            "UNKNOWN_CAPABILITY",
            f"unknown enterprise setup capability: {capability}",
            "Upgrade internctl or remove the unknown required capability from policy.",
        )

    def _command_check(
        self,
        check_id: str,
        scope: str,
        label: str,
        state: str,
        command: list[str],
        *,
        deep: bool,
        missing_command: str,
        hint: str,
    ) -> SetupCheck:
        if not shutil.which(missing_command):
            return self._plain_check(check_id, scope, label, state, False, "COMMAND_MISSING", f"{missing_command} is not installed", hint)
        if not deep:
            return self._plain_check(check_id, scope, label, state, True, "OK", f"{missing_command} is installed", "")
        try:
            result = self.command_runner(command, capture_output=True, text=True, timeout=10)
            ok = result.returncode == 0
            msg = _combined_output(result) or ("command succeeded" if ok else f"exit {result.returncode}")
        except Exception as exc:
            ok = False
            msg = str(exc)
        return self._plain_check(check_id, scope, label, state, ok, "OK" if ok else "COMMAND_FAILED", msg, hint)

    def _python_cli_wrapper_check(
        self,
        check_id: str,
        scope: str,
        label: str,
        state: str,
        command_name: str,
        script_path: Path,
        *,
        deep: bool,
        apply: bool,
        hint: str,
    ) -> SetupCheck:
        if not script_path.is_file():
            return self._plain_check(
                check_id,
                scope,
                label,
                state,
                False,
                "CLI_SCRIPT_MISSING",
                f"{script_path} is missing",
                hint,
            )
        current = shutil.which(command_name)
        if apply and not self._wrapper_points_to(current, script_path):
            current = str(self._write_python_cli_wrapper(command_name, script_path))
        if not current:
            return self._plain_check(
                check_id,
                scope,
                label,
                state,
                False,
                "COMMAND_MISSING",
                f"{command_name} wrapper is not in PATH",
                hint,
            )
        if not deep:
            return self._plain_check(check_id, scope, label, state, True, "OK", f"{command_name} is installed", "")
        try:
            result = self.command_runner([current, "--help"], capture_output=True, text=True, timeout=10)
            ok = result.returncode == 0
            message = _combined_output(result) or ("command succeeded" if ok else f"exit {result.returncode}")
        except Exception as exc:
            ok = False
            message = str(exc)
        check = self._plain_check(check_id, scope, label, state, ok, "OK" if ok else "COMMAND_FAILED", message, hint)
        if apply and ok:
            check.status = "applied"
            check.details = {"action": "verified_cli_wrapper", "command": command_name, "path": current}
        return check

    def _ssh_check(
        self,
        check_id: str,
        scope: str,
        label: str,
        state: str,
        host: str,
        *,
        deep: bool,
        success_markers: list[str],
        hint: str,
    ) -> SetupCheck:
        if not shutil.which("ssh"):
            return self._plain_check(check_id, scope, label, state, False, "COMMAND_MISSING", "ssh is not installed", hint)
        if not deep:
            return self._plain_check(check_id, scope, label, state, True, "OK", "ssh command is installed", "")
        try:
            result = self.command_runner([
                "ssh",
                "-T",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=8",
                host,
            ], capture_output=True, text=True, timeout=10)
            text = _combined_output(result, max_len=500).lower()
            ok = any(marker in text for marker in success_markers)
            if host == "git@codeup.aliyun.com" and result.returncode == 0:
                ok = True
        except Exception as exc:
            ok = False
            text = str(exc)
        return self._plain_check(check_id, scope, label, state, ok, "OK" if ok else "SSH_AUTH_FAILED", "SSH authenticated" if ok else text, hint)

    def _plain_check(
        self,
        check_id: str,
        scope: str,
        label: str,
        state: str,
        ok: bool,
        code: str,
        message: str,
        hint: str,
        *,
        user_actionable: bool = True,
        admin_managed: bool = False,
    ) -> SetupCheck:
        return SetupCheck(
            check_id,
            scope,
            label,
            state,
            "ok" if ok else "missing",
            ok,
            (not ok) and state == "required",
            user_actionable,
            admin_managed,
            code,
            message,
            "" if ok else hint,
        )

    def _disabled_check(self, scope: str, label: str) -> SetupCheck:
        return SetupCheck(
            f"{scope}.policy_disabled",
            scope,
            label,
            "disabled",
            "disabled",
            True,
            False,
            False,
            False,
            "POLICY_DISABLED",
            "disabled by enterprise policy",
        )

    def _admin_only_check(self, scope: str, label: str) -> SetupCheck:
        return SetupCheck(
            f"{scope}.admin_only",
            scope,
            label,
            "admin_only",
            "admin_managed",
            True,
            False,
            False,
            True,
            "ADMIN_MANAGED",
            "managed by enterprise administrator; user CLI cannot configure it",
        )

    def _capabilities(self) -> set[str]:
        capabilities = self.policy.data.get("capabilities", {})
        if not isinstance(capabilities, dict):
            return set()
        return {str(key) for key in capabilities.keys()}

    def _requires_terminal_runtime(self) -> bool:
        return any(self._capability_state(capability) == "required" for capability in ("claude", "codex", "copilot"))

    def _requires_python_runtime(self) -> bool:
        return self._capability_state("feishu") in {"required", "admin_only"}

    def _capability_state(self, capability: str) -> str:
        raw = self.policy.data.get("capabilities", {}).get(capability, {})
        return normalize_policy_state(raw)

    def _policy_requires_secret_bundle(self) -> bool:
        if not self.policy.ok:
            return False
        for capability in self._capabilities():
            state = self._capability_state(capability)
            if capability == "feishu" and state in {"required", "admin_only"}:
                return True
            if state == "admin_only":
                return True
        return False

    def _report(self, command: str, checks: list[SetupCheck]) -> dict:
        ready = all(not item.blocking for item in checks)
        check_dicts = [item.to_dict() for item in checks]
        blocking_checks = [item for item in check_dicts if item.get("blocking")]
        summary = {
            "ok": sum(1 for item in checks if item.passed),
            "blocking": sum(1 for item in checks if item.blocking),
            "user_action_required": sum(1 for item in checks if item.blocking and item.user_actionable),
            "admin_action_required": sum(1 for item in checks if item.blocking and item.admin_managed),
        }
        report = EnterpriseSetupReport(
            command=command,
            mode="enterprise",
            ready=ready,
            policy={
                "state": self.policy.state,
                "path": "" if self.policy.state == "builtin_user_bootstrap" else self.policy.path,
                "schema": self.policy.data.get("schema", "") if self.policy.ok else "",
                "deployment_id": self.policy.data.get("deployment_id", "") if self.policy.ok else "",
                "error": self.policy.error,
                "builtin": self.policy.state == "builtin_user_bootstrap",
            },
            secrets={
                "state": self.secrets.state,
                "path": self.secrets.path,
                "error": self.secrets.error,
                "redacted": True,
            },
            summary=summary,
            checks=check_dicts,
            next_actions=self._next_actions(checks),
            webview_contract={
                "model": "capability_checks",
                "actions_are_cli_only": True,
                "secret_values_in_report": False,
            },
            extra={
                "blocking_count": len(blocking_checks),
                "blocking_checks": blocking_checks,
            },
        )
        return report.to_dict()

    def _next_actions(self, checks: list[SetupCheck]) -> list[str]:
        actions: list[str] = []
        if any(item.id == "policy.loaded" and not item.passed for item in checks):
            actions.append("Ask the enterprise administrator for a valid policy bundle.")
        if any(item.blocking and item.admin_managed for item in checks):
            actions.append("Ask the enterprise administrator to fix admin-managed policy or relay configuration.")
        if any(item.blocking and item.user_actionable for item in checks):
            actions.append("Run `internctl setup doctor --json` and complete user-actionable missing checks.")
        if any(item.status == "missing" and item.id in {"core.work_root", "core.log_dir"} for item in checks):
            actions.append("Run `internctl setup apply --json` to create local directories.")
        if any(item.status == "missing" and item.id.startswith("runtime.") for item in checks):
            actions.append("Run `internctl setup apply --install-runtime --json` to install required local runtime dependencies.")
        return actions

    def _bundled_hooks_dir(self) -> Path | None:
        cli_root = Path(__file__).resolve().parents[1]
        candidates = [
            cli_root.parent / "hooks",
            cli_root.parent / "vscode-extension" / "hooks",
        ]
        for candidate in candidates:
            if (candidate / "codex_settings.toml").is_file() and (candidate / "user_prompt_hook.py").is_file():
                return candidate
        return None

    def _copy_hook_env(self, source: Path, target: Path) -> None:
        hooks_target = target / "hooks"
        hooks_target.mkdir(parents=True, exist_ok=True)
        for entry in hooks_target.iterdir():
            if entry.name in {"hooks.json", ".version"}:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for entry in source.iterdir():
            if entry.name in {"claude_settings.json", "codex_settings.toml"}:
                continue
            dst = hooks_target / entry.name
            if entry.is_dir():
                if entry.name in {"__pycache__", "tests", ".pytest_cache", "llm_intern_logs"}:
                    continue
                shutil.copytree(
                    entry,
                    dst,
                    ignore=shutil.ignore_patterns("__pycache__", "tests", ".pytest_cache", "*.pyc"),
                )
            else:
                shutil.copy2(entry, dst)
        for name in ("claude_settings.json", "codex_settings.toml"):
            src = source / name
            if src.is_file():
                shutil.copy2(src, target / name)

    def _bundled_cli_file(self, name: str) -> Path:
        return Path(__file__).resolve().parents[1] / name

    def _wrapper_bin_dir(self) -> Path:
        explicit = os.environ.get("INTERN_CLI_BIN_DIR")
        if explicit:
            path = Path(explicit).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            return path
        usr_local = Path("/usr/local/bin")
        if usr_local.is_dir() and os.access(usr_local, os.W_OK):
            return usr_local
        local = self.home / ".local" / "bin"
        local.mkdir(parents=True, exist_ok=True)
        return local

    def _wrapper_points_to(self, wrapper: str | None, script_path: Path) -> bool:
        if not wrapper:
            return False
        try:
            text = Path(wrapper).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False
        return str(script_path) in text

    def _write_python_cli_wrapper(self, command_name: str, script_path: Path) -> Path:
        target = self._wrapper_bin_dir() / command_name
        python = sys.executable or "python3"
        target.write_text(f'#!/bin/sh\nexec "{python}" "{script_path}" "$@"\n', encoding="utf-8")
        target.chmod(0o755)
        bin_dir = str(target.parent)
        if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        return target

    def _missing_python_deps(self) -> list[str]:
        checks = [("websockets", "websockets"), ("lark_oapi", "lark-oapi")]
        missing: list[str] = []
        for module, package in checks:
            try:
                result = self.command_runner(
                    ["python3", "-c", f"import {module}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                ok = result.returncode == 0
            except Exception:
                ok = False
            if not ok:
                missing.append(package)
        return missing

    def _install_python_deps(self, packages: list[str]) -> None:
        if not packages:
            return
        pip_cmd = ["python3", "-m", "pip", "install", *packages]
        try:
            result = self.command_runner(pip_cmd, capture_output=True, text=True, timeout=180)
        except Exception:
            return
        if result.returncode == 0:
            return
        text = _combined_output(result, max_len=800)
        if "externally-managed-environment" in text:
            self.command_runner(
                ["python3", "-m", "pip", "install", *packages, "--break-system-packages"],
                capture_output=True,
                text=True,
                timeout=180,
            )

    def _install_codex_cli(self) -> None:
        if not self._node_runtime_supports_codex_install() and not self._install_node_runtime():
            return
        try:
            self.command_runner(
                ["npm", "install", "-g", "@openai/codex"],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except Exception:
            return

    def _node_runtime_supports_codex_install(self) -> bool:
        if not shutil.which("npm") or not shutil.which("node"):
            return False
        major = self._node_major_version()
        return major is None or major >= 18

    def _node_major_version(self) -> int | None:
        if not shutil.which("node"):
            return None
        try:
            result = self.command_runner(["node", "-v"], capture_output=True, text=True, timeout=10)
        except Exception:
            return None
        if result.returncode != 0:
            return None
        text = _combined_output(result, max_len=80).strip()
        if text.startswith("v"):
            text = text[1:]
        try:
            return int(text.split(".", 1)[0])
        except Exception:
            return None

    def _install_node_runtime(self) -> bool:
        try:
            if shutil.which("apt-get"):
                self.command_runner(["apt-get", "update", "-qq"], capture_output=True, text=True, timeout=180)
                # Distro nodejs packages can be too old for modern Codex CLI.
                # Prefer NodeSource LTS/current repo when curl is available.
                self.command_runner(
                    ["apt-get", "install", "-y", "-qq", "ca-certificates", "curl", "gnupg"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if shutil.which("curl"):
                    self.command_runner(
                        ["bash", "-lc", "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -"],
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                result = self.command_runner(
                    ["apt-get", "install", "-y", "-qq", "nodejs"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode == 0 and self._node_runtime_supports_codex_install():
                    return True
                fallback = self.command_runner(
                    ["apt-get", "install", "-y", "-qq", "nodejs", "npm"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                return fallback.returncode == 0 and self._node_runtime_supports_codex_install()
            if shutil.which("brew"):
                result = self.command_runner(["brew", "install", "node"], capture_output=True, text=True, timeout=300)
                return result.returncode == 0 and self._node_runtime_supports_codex_install()
            if shutil.which("dnf"):
                result = self.command_runner(["dnf", "install", "-y", "nodejs", "npm"], capture_output=True, text=True, timeout=300)
                return result.returncode == 0 and self._node_runtime_supports_codex_install()
            if shutil.which("yum"):
                result = self.command_runner(["yum", "install", "-y", "nodejs", "npm"], capture_output=True, text=True, timeout=300)
                return result.returncode == 0 and self._node_runtime_supports_codex_install()
            if shutil.which("apk"):
                result = self.command_runner(["apk", "add", "nodejs", "npm"], capture_output=True, text=True, timeout=300)
                return result.returncode == 0 and self._node_runtime_supports_codex_install()
        except Exception:
            return False
        return self._node_runtime_supports_codex_install()

    def _install_tmux(self) -> None:
        try:
            if shutil.which("apt-get"):
                self.command_runner(["apt-get", "update", "-qq"], capture_output=True, text=True, timeout=180)
                self.command_runner(["apt-get", "install", "-y", "-qq", "tmux"], capture_output=True, text=True, timeout=180)
                return
            if shutil.which("brew"):
                self.command_runner(["brew", "install", "tmux"], capture_output=True, text=True, timeout=180)
                return
            if shutil.which("dnf"):
                self.command_runner(["dnf", "install", "-y", "tmux"], capture_output=True, text=True, timeout=180)
                return
            if shutil.which("yum"):
                self.command_runner(["yum", "install", "-y", "tmux"], capture_output=True, text=True, timeout=180)
                return
            if shutil.which("apk"):
                self.command_runner(["apk", "add", "tmux"], capture_output=True, text=True, timeout=180)
        except Exception:
            return


def print_json_report(report: dict) -> None:
    print(json.dumps(redact_secrets(report), ensure_ascii=False, indent=2))


def write_export(report: dict, output: str | os.PathLike[str]) -> None:
    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact_secrets(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _combined_output(result: subprocess.CompletedProcess[str], max_len: int = 220) -> str:
    text = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part.strip())
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _object_has_auth(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for key, child in value.items():
        normalized = str(key).lower()
        if child and not isinstance(child, bool) and (
            "oauth" in normalized
            or "access_token" in normalized
            or "refresh_token" in normalized
            or "refreshtoken" in normalized
            or normalized == "account"
        ):
            return True
        if _object_has_auth(child):
            return True
    return False


def _redact_url(value: str) -> str:
    parsed = urlparse(value)
    query = parsed.query
    if query:
        redacted_query = []
        for key, child in parse_qsl(query, keep_blank_values=True):
            lower_key = key.lower().replace("-", "_")
            if any(marker in lower_key for marker in ("secret", "token", "password", "access_key")):
                redacted_query.append((key, "***"))
            else:
                redacted_query.append((key, child))
        query = urlencode(redacted_query)
    if not parsed.password:
        return parsed._replace(query=query).geturl()
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    if parsed.username:
        netloc = f"{parsed.username}:***@{netloc}"
    return parsed._replace(netloc=netloc, query=query).geturl()
