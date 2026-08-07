from __future__ import annotations
import json, os

class State:
    def __init__(self, data: dict | None = None):
        self.d = data or {"tips": {}, "cursors": {}, "flags": []}

    @classmethod
    def load(cls, path: str) -> "State":
        try:
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, path)

    def get_tip(self, project_id: int, branch: str):
        return self.d["tips"].get(f"{project_id}:{branch}")

    def set_tip(self, project_id: int, branch: str, sha: str) -> None:
        self.d["tips"][f"{project_id}:{branch}"] = sha

    def get_cursor(self, name: str):
        return self.d["cursors"].get(name)

    def set_cursor(self, name: str, value: str) -> None:
        self.d["cursors"][name] = value

    def flag_once(self, key: str) -> bool:
        if key in self.d["flags"]:
            return False
        self.d["flags"].append(key)
        return True
