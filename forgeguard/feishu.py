from __future__ import annotations
import json
import requests
from .config import Config

_BASE = "https://open.feishu.cn/open-apis"

class Feishu:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token: str | None = None
        try:
            with open(cfg.usermap_path, encoding="utf-8") as f:
                self.usermap: dict = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.usermap = {}

    def _tenant_token(self) -> str:
        if self._token is None:
            r = requests.post(f"{_BASE}/auth/v3/tenant_access_token/internal",
                              json={"app_id": self.cfg.feishu_app_id,
                                    "app_secret": self.cfg.feishu_app_secret},
                              timeout=30).json()
            if r.get("code") != 0:
                raise RuntimeError(f"feishu token error: {r}")
            self._token = r["tenant_access_token"]
        return self._token

    def notify(self, text: str, at_gitlab_user: str | None = None) -> None:
        open_id = self.usermap.get(at_gitlab_user or "")
        if open_id:
            text = f'<at user_id="{open_id}"></at> {text}'
        r = requests.post(
            f"{_BASE}/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            json={"receive_id": self.cfg.feishu_chat_id,
                  "msg_type": "text",
                  "content": json.dumps({"text": text})},
            timeout=30).json()
        if r.get("code") != 0:
            raise RuntimeError(f"feishu send error: {r}")
