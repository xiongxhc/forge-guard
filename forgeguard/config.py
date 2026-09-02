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
    review_branches: list[str]
    exclude: list[str]
    feishu_app_id: str
    feishu_app_secret: str
    feishu_chat_id: str
    usermap_path: str

    @staticmethod
    def _match(name: str, patterns) -> bool:
        # Plain entries are exact; entries containing a glob char use
        # fnmatch (GitLab's own protected-branch wildcard convention).
        return any(fnmatchcase(name, b) if ("*" in b or "?" in b) else name == b
                   for b in patterns)

    def branch_match(self, name: str) -> bool:
        return self._match(name, self.branches)

    def review_match(self, name: str) -> bool:
        # Review lane covers the protection list plus review-only branches.
        return self._match(name, self.branches + self.review_branches)
    state_path: str
    diff_cap_bytes: int
    diff_cap_full: int
    full_review_label: str
    review_mode: str
    context_cap_bytes: int
    brief_dir: str
    limit_warn: float
    footer: str

def _require(env: Mapping[str, str], key: str) -> str:
    val = env.get(key, "").strip()
    if not val:
        raise SystemExit(f"forge-guard: missing required env {key}")
    return val

def _csv(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]

def load_config(env: Mapping[str, str] = os.environ) -> Config:
    home = env.get("HOME", os.path.expanduser("~"))
    mode = env.get("FORGEGUARD_REVIEW_MODE", "files").strip()
    if mode not in ("diff", "files"):
        raise SystemExit(f"forge-guard: FORGEGUARD_REVIEW_MODE must be "
                         f"'diff' or 'files', got {mode!r}")
    return Config(
        gitlab_url=_require(env, "FORGEGUARD_GITLAB_URL").rstrip("/"),
        token=_require(env, "FORGEGUARD_GITLAB_TOKEN"),
        extra_tokens=_csv(env.get("FORGEGUARD_GITLAB_EXTRA_TOKENS", "")),
        branches=_csv(env.get("FORGEGUARD_BRANCHES", _DEFAULT_BRANCHES)),
        review_branches=_csv(env.get("FORGEGUARD_REVIEW_BRANCHES", "")),
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
        review_mode=mode,
        context_cap_bytes=int(env.get("FORGEGUARD_CONTEXT_CAP", "600000")),
        brief_dir=env.get("FORGEGUARD_BRIEF_DIR",
                          f"{home}/.local/share/forge-guard/briefs"),
        limit_warn=float(env.get("FORGEGUARD_LIMIT_WARN", "0.95")),
        footer=env.get("FORGEGUARD_FOOTER", "⚙️ auto-review is advisory").strip(),
    )
