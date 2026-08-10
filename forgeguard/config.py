from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Mapping

_DEFAULT_BRANCHES = "main,master,prod,production,develop,dev,uat"

@dataclass(frozen=True)
class Config:
    gitlab_url: str
    token: str
    branches: list[str]
    exclude: list[str]
    feishu_app_id: str
    feishu_app_secret: str
    feishu_chat_id: str
    usermap_path: str
    state_path: str
    diff_cap_bytes: int

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
        branches=_csv(env.get("FORGEGUARD_BRANCHES", _DEFAULT_BRANCHES)),
        exclude=_csv(env.get("FORGEGUARD_EXCLUDE", "")),
        feishu_app_id=_require(env, "FORGEGUARD_FEISHU_APP_ID"),
        feishu_app_secret=_require(env, "FORGEGUARD_FEISHU_APP_SECRET"),
        feishu_chat_id=_require(env, "FORGEGUARD_FEISHU_CHAT_ID"),
        usermap_path=env.get("FORGEGUARD_USERMAP",
                             f"{home}/.config/forge-guard/forge-guard-usermap.json"),
        state_path=env.get("FORGEGUARD_STATE",
                           f"{home}/.local/share/forge-guard/state.json"),
        diff_cap_bytes=int(env.get("FORGEGUARD_DIFF_CAP", "300000")),
    )
