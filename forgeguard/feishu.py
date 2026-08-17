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

    def _send(self, msg_type: str, content: dict) -> None:
        r = requests.post(
            f"{_BASE}/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            json={"receive_id": self.cfg.feishu_chat_id,
                  "msg_type": msg_type,
                  "content": json.dumps(content)},
            timeout=30).json()
        if r.get("code") != 0:
            raise RuntimeError(f"feishu send error: {r}")

    def notify(self, text: str, at_gitlab_user: str | None = None) -> None:
        open_id = self.usermap.get(at_gitlab_user or "")
        if open_id:
            text = f'<at user_id="{open_id}"></at> {text}'
        self._send("text", {"text": text})

    def notify_post(self, title: str, lines: list,
                    at_gitlab_user: str | None = None) -> None:
        # lines: Feishu post content — a list of lines, each a list of
        # {"tag": "text"|"a", ...} segments.
        open_id = self.usermap.get(at_gitlab_user or "")
        if open_id:
            lines = [[{"tag": "at", "user_id": open_id}]] + lines
        elif at_gitlab_user:
            lines = [[{"tag": "text", "text": f"@{at_gitlab_user}"}]] + lines
        self._send("post", {"zh_cn": {"title": title, "content": lines}})
