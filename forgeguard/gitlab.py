from __future__ import annotations
from urllib.parse import urlsplit, urlunsplit
import requests
from .config import Config

class GitLabError(RuntimeError):
    def __init__(self, status: int, path: str):
        super().__init__(f"GitLab {status} on {path}")
        self.status = status

def rebase_url(url: str, base: str) -> str:
    b, u = urlsplit(base), urlsplit(url)
    return urlunsplit((b.scheme, b.netloc, u.path, u.query, u.fragment))

class GitLab:
    def __init__(self, cfg: Config, token: str | None = None):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers["PRIVATE-TOKEN"] = token or cfg.token

    def _url(self, path: str) -> str:
        return f"{self.cfg.gitlab_url}/api/v4{path}"

    def get(self, path: str, ok404: bool = False, **params):
        r = self.s.get(self._url(path), params=params, timeout=30)
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 400:
            raise GitLabError(r.status_code, path)
        return r.json()

    def get_all(self, path: str, **params) -> list:
        out, page = [], 1
        while True:
            r = self.s.get(self._url(path),
                           params={**params, "per_page": 100, "page": page}, timeout=30)
            if r.status_code >= 400:
                raise GitLabError(r.status_code, path)
            out.extend(r.json())
            nxt = r.headers.get("X-Next-Page", "")
            if not nxt:
                return out
            page = int(nxt)

    def post(self, path: str, **data):
        r = self.s.post(self._url(path), data=data, timeout=30)
        if r.status_code >= 400:
            raise GitLabError(r.status_code, path)
        return r.json() if r.text else None

    def put(self, path: str, **data):
        r = self.s.put(self._url(path), data=data, timeout=30)
        if r.status_code >= 400:
            raise GitLabError(r.status_code, path)
        return r.json() if r.text else None

    def delete(self, path: str) -> None:
        r = self.s.delete(self._url(path), timeout=30)
        if r.status_code >= 400 and r.status_code != 404:
            raise GitLabError(r.status_code, path)
