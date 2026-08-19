from __future__ import annotations
import os
from fnmatch import fnmatchcase
from dataclasses import dataclass
from typing import Mapping

_DEFAULT_BRANCHES = "main,master,prod,production,develop,dev,uat"

@dataclass(frozen=True)
class Config:
    gitlab_url: str
    token: str
    extra_tokens: list[str]
    branches: list[str]
    exclude: list[str]
    feishu_app_id: str
    feishu_app_secret: str
    feishu_chat_id: str
    usermap_path: str

    def branch_match(self, name: str) -> bool:
        # Plain entries are exact; entries containing a glob char use
        # fnmatch (GitLab's own protected-branch wildcard convention).
        return any(fnmatchcase(name, b) if ("*" in b or "?" in b) else name == b
                   for b in self.branches)
    state_path: str
    diff_cap_bytes: int
    diff_cap_full: int
    full_review_label: str

def _require(env: Mapping[str, str], key: str) -> str:
    val = env.get(key, "").strip()
    if not val:
        raise SystemExit(f"forge-guard: missing required env {key}")
    return val

def _csv(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]

def load_config(env: Mapping[str, str] = os.environ) -> Config:
    home = env.get("HOME", os.path.expanduser("~"))
    return Config(
        gitlab_url=_require(env, "FORGEGUARD_GITLAB_URL").rstrip("/"),
        token=_require(env, "FORGEGUARD_GITLAB_TOKEN"),
        extra_tokens=_csv(env.get("FORGEGUARD_GITLAB_EXTRA_TOKENS", "")),
        branches=_csv(env.get("FORGEGUARD_BRANCHES", _DEFAULT_BRANCHES)),
        exclude=_csv(env.get("FORGEGUARD_EXCLUDE", "")),
        feishu_app_id=_require(env, "FORGEGUARD_FEISHU_APP_ID"),
        feishu_app_secret=_require(env, "FORGEGUARD_FEISHU_APP_SECRET"),
        feishu_chat_id=_require(env, "FORGEGUARD_FEISHU_CHAT_ID"),
        usermap_path=env.get("FORGEGUARD_USERMAP",
                             f"{home}/.config/forge-guard/usermap.json"),
        state_path=env.get("FORGEGUARD_STATE",
                           f"{home}/.local/share/forge-guard/state.json"),
        diff_cap_bytes=int(env.get("FORGEGUARD_DIFF_CAP", "300000")),
        diff_cap_full=int(env.get("FORGEGUARD_DIFF_CAP_FULL", "1000000")),
        full_review_label=env.get("FORGEGUARD_FULL_REVIEW_LABEL", "forge-guard:full-review"),
    )
