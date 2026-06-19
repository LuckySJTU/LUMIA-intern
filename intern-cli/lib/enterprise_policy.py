"""Enterprise policy and secret bundle loading helpers.

The setup CLI, future admin tooling, daemon bootstrap, and VS Code bridge must
share these helpers instead of each parsing policy files independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat


POLICY_SCHEMA = "intern-agents.enterprise-policy.v1"
SECRET_SCHEMA = "intern-agents.enterprise-secrets.v1"
POLICY_STATES = {"required", "optional", "disabled", "admin_only"}
SECRET_KEY_MARKERS = ("secret", "token", "password", "access_key", "app_secret", "relay_token")


class EnterprisePolicyError(ValueError):
    """Raised when enterprise policy input is structurally invalid."""


@dataclass(frozen=True)
class CapabilityPolicy:
    id: str
    state: str


@dataclass(frozen=True)
class EnterprisePolicy:
    schema: str
    deployment_id: str
    capabilities: dict[str, CapabilityPolicy]
    raw: dict

    @classmethod
    def from_dict(cls, data: dict) -> "EnterprisePolicy":
        if not isinstance(data, dict):
            raise EnterprisePolicyError("policy must be a JSON object")
        if data.get("schema") != POLICY_SCHEMA:
            raise EnterprisePolicyError(f"unsupported policy schema: {data.get('schema')!r}")
        deployment_id = data.get("deployment_id")
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise EnterprisePolicyError("deployment_id must be a non-empty string")
        capabilities = data.get("capabilities")
        if not isinstance(capabilities, dict):
            raise EnterprisePolicyError("capabilities must be an object")

        parsed: dict[str, CapabilityPolicy] = {}
        for capability, raw in capabilities.items():
            if not isinstance(capability, str) or not capability.strip():
                raise EnterprisePolicyError("capability id must be a non-empty string")
            state = normalize_policy_state(raw)
            if state not in POLICY_STATES:
                raise EnterprisePolicyError(f"invalid state for capability {capability}: {state!r}")
            parsed[capability] = CapabilityPolicy(capability, state)
        return cls(POLICY_SCHEMA, deployment_id, parsed, data)


@dataclass
class PolicyLoadResult:
    state: str
    path: str
    data: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.state in {"loaded", "builtin_user_bootstrap"}


@dataclass
class SecretLoadResult:
    state: str
    path: str
    data: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "loaded"

    def has_secret(self, key: str) -> bool:
        if not self.ok:
            return False
        entry = self.data.get("secrets", {}).get(key)
        if not isinstance(entry, dict):
            return False
        return bool(resolve_secret_value(entry))


def default_policy_path(work_root: Path) -> Path:
    explicit = (
        os.environ.get("INTERN_ENTERPRISE_POLICY")
        or os.environ.get("INTERN_ENTERPRISE_POLICY_PATH")
        or os.environ.get("ENTERPRISE_POLICY_PATH")
    )
    if explicit:
        return Path(explicit).expanduser()
    candidates = [
        work_root / "enterprise" / "policy.json",
        work_root / ".intern" / "enterprise-policy.json",
        work_root / "enterprise-policy.json",
        work_root / ".feishu_registry" / "enterprise_policy.json",
        work_root / "enterprise_policy.json",
        work_root / "axis_intern_agents" / "workspace" / "enterprise_policy.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def default_secret_path(work_root: Path) -> Path:
    explicit = (
        os.environ.get("INTERN_ENTERPRISE_SECRETS")
        or os.environ.get("INTERN_ENTERPRISE_SECRET_PATH")
    )
    if explicit:
        return Path(explicit).expanduser()
    candidates = [
        work_root / "enterprise" / "secrets.json",
        work_root / ".intern" / "enterprise-secrets.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def enterprise_policy_exists(work_root: str | os.PathLike[str] | None = None) -> bool:
    root = Path(work_root or os.environ.get("WORK_AGENTS_ROOT") or "/work-agents")
    return default_policy_path(root).is_file()


def load_enterprise_policy(path: Path) -> PolicyLoadResult:
    if not path.is_file():
        return PolicyLoadResult("missing", str(path), error=f"policy file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return PolicyLoadResult("invalid", str(path), error=str(exc))
    try:
        EnterprisePolicy.from_dict(data)
    except EnterprisePolicyError as exc:
        return PolicyLoadResult("invalid", str(path), data=data, error=str(exc))
    return PolicyLoadResult("loaded", str(path), data=data)


def load_enterprise_policy_for_root(work_root: str | os.PathLike[str] | None = None) -> PolicyLoadResult:
    root = Path(work_root or os.environ.get("WORK_AGENTS_ROOT") or "/work-agents")
    return load_enterprise_policy(default_policy_path(root))


def load_enterprise_secrets(path: Path, *, required: bool) -> SecretLoadResult:
    if not path.is_file():
        state = "missing_required" if required else "missing_optional"
        return SecretLoadResult(state, str(path), error=f"secret bundle not found: {path}")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return SecretLoadResult("invalid", str(path), error=str(exc))
    if mode & 0o077:
        return SecretLoadResult("invalid_permissions", str(path), error=f"secret bundle permissions must be 0600, got {oct(mode)}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return SecretLoadResult("invalid", str(path), error=str(exc))
    if data.get("schema") != SECRET_SCHEMA:
        return SecretLoadResult("invalid", str(path), data=data, error=f"unsupported secret schema: {data.get('schema')!r}")
    secrets = data.get("secrets")
    if not isinstance(secrets, dict):
        return SecretLoadResult("invalid", str(path), data=data, error="secrets must be an object")
    return SecretLoadResult("loaded", str(path), data=data)


def normalize_policy_state(raw: object) -> str:
    if isinstance(raw, str):
        state = raw
    elif isinstance(raw, dict):
        state = str(raw.get("state") or "optional")
    else:
        state = "optional"
    if state == "admin_managed":
        state = "admin_only"
    return state


def resolve_secret_value(entry: dict) -> str:
    secret_type = str(entry.get("type") or "")
    if secret_type == "env_or_file":
        ref = str(entry.get("value_ref") or "")
        if ref and os.environ.get(ref):
            return os.environ[ref]
        file_ref = str(entry.get("file_ref") or "")
        if file_ref:
            try:
                return Path(file_ref).expanduser().read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                return ""
        return str(entry.get("value") or "")
    if secret_type in {"sealed_value", "user_secret"}:
        return str(entry.get("value") or "")
    return str(entry.get("value") or "")


def redact_secrets(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict = {}
        for key, child in value.items():
            if _is_secret_key(key):
                redacted[key] = "***"
            else:
                redacted[key] = redact_secrets(child)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _is_secret_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    if normalized in {"secrets", "secret_key", "secret_keys", "contains_secrets", "secret_values_in_report"}:
        return False
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)
