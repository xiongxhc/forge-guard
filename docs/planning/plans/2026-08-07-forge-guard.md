# forge-guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce MR-only merges on protected branches instance-wide on the org GitLab (CE), detect force pushes and merges-without-MR, hard-gate features-without-tests via CI, and post advisory AI reviews + Feishu alerts.

**Architecture:** One fleet agent `forge-guard/` in the monorepo: deterministic Python modules (GitLab client, state store, protection sweep, violation detection, Feishu notifier) plus one `claude -p` seam for MR review. Two launchd ticks (hourly sweep, 15-min review). A CI template (shipped in-repo, deployed later to `forge/quality-gate` on the forge) provides the hard test-presence gate.

**Tech Stack:** Python 3.12, `requests`, `pytest` + `responses` (HTTP mocking), `claude` CLI (subscription), GitLab REST API v4 (CE endpoints only), Feishu open-api, launchd.

## Global Constraints

- **CE-only GitLab APIs** — no approval rules, push rules, or group protections (spec: instance is CE/free).
- **LLM calls via `claude -p` on the Max subscription — never API keys** (standing user rule).
- **Every surfaced URL is rebased**: take the forge path, force scheme+host to `FORGEGUARD_GITLAB_URL` (`https://gitlab.example.com`) — the forge's own `external_url` is a dead hostname.
- **Report, never revert** — violations alert; no branch mutation, no MR closing.
- **Idempotent ticks** — a second run with unchanged remote state makes zero changes and sends zero alerts.
- **Atomic state writes** — tmp file + `os.replace` (fleet convention).
- **Secrets only in `~/.config/forge-guard/forge-guard.env`** (dir 700, file 600); never in the repo.
- Test style per `acme-qa:qa-pytest-discipline`: no `time.sleep`, assert exact values, one behavior per test.

## File Structure

```
forge-guard/
  forgeguard/
    __init__.py
    config.py        # env → Config dataclass
    gitlab.py        # thin REST client + rebase_url
    state.py         # per-project tips/cursors, atomic JSON
    protect.py       # ensure_protection + classify_move (force-push / no-MR merge)
    feishu.py        # tenant token, chat post, @mention, usermap
    review.py        # MR poll, diff cap, claude seam, note upsert, approve
    cli.py           # argparse: sweep | review | inject-gate
  tests/
    test_config.py test_gitlab.py test_state.py test_protect.py
    test_feishu.py test_review.py test_cli.py test_gate_script.py
  ci-template/
    gate.yml         # CI include deployed to forge/quality-gate later
    check_mr.py      # the gate script gate.yml runs
  launchd/
    com.forgeguard.sweep.plist.example
    com.forgeguard.review.plist.example
  requirements.txt
  README.md
```

---

### Task 1: Scaffold + config module

**Files:**
- Create: `forge-guard/forgeguard/__init__.py` (empty), `forge-guard/forgeguard/config.py`, `forge-guard/requirements.txt`, `forge-guard/tests/test_config.py`

**Interfaces:**
- Produces: `Config` dataclass with fields `gitlab_url: str`, `token: str`, `branches: list[str]`, `exclude: list[str]`, `feishu_app_id: str`, `feishu_app_secret: str`, `feishu_chat_id: str`, `usermap_path: str`, `state_path: str`, `diff_cap_bytes: int`; `load_config(env: Mapping[str,str]) -> Config` raising `SystemExit` with a clear message on missing required vars.

- [ ] **Step 1: Write the failing test**

```python
# forge-guard/tests/test_config.py
import pytest
from forgeguard.config import load_config

BASE = {
    "FORGEGUARD_GITLAB_URL": "https://gitlab.example.com",
    "FORGEGUARD_GITLAB_TOKEN": "tok",
    "FORGEGUARD_FEISHU_APP_ID": "cli_x",
    "FORGEGUARD_FEISHU_APP_SECRET": "sec",
    "FORGEGUARD_FEISHU_CHAT_ID": "oc_x",
}

def test_defaults_and_required():
    cfg = load_config(BASE)
    assert cfg.gitlab_url == "https://gitlab.example.com"
    assert cfg.branches == ["main", "master", "prod", "production", "develop", "uat"]
    assert cfg.exclude == []
    assert cfg.diff_cap_bytes == 300_000
    assert cfg.state_path.endswith("forge-guard/state.json")

def test_trailing_slash_stripped_and_overrides():
    env = dict(BASE, FORGEGUARD_GITLAB_URL="https://gitlab.example.com/",
               FORGEGUARD_BRANCHES="main, uat", FORGEGUARD_EXCLUDE="sandbox/x")
    cfg = load_config(env)
    assert cfg.gitlab_url == "https://gitlab.example.com"
    assert cfg.branches == ["main", "uat"]
    assert cfg.exclude == ["sandbox/x"]

def test_missing_token_exits():
    env = {k: v for k, v in BASE.items() if k != "FORGEGUARD_GITLAB_TOKEN"}
    with pytest.raises(SystemExit):
        load_config(env)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd forge-guard && python3 -m pytest tests/test_config.py -q`
Expected: FAIL (`ModuleNotFoundError: forgeguard`)

- [ ] **Step 3: Write minimal implementation**

```python
# forge-guard/forgeguard/config.py
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Mapping

_DEFAULT_BRANCHES = "main,master,prod,production,develop,uat"

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
```

```
# forge-guard/requirements.txt
requests>=2.32
pytest>=8.0
responses>=0.25
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd forge-guard && pip install -r requirements.txt && python3 -m pytest tests/test_config.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add forge-guard/forgeguard forge-guard/tests/test_config.py forge-guard/requirements.txt
git commit -m "feat(forge-guard): scaffold + env config"
```

---

### Task 2: GitLab client + URL rebasing

**Files:**
- Create: `forge-guard/forgeguard/gitlab.py`, `forge-guard/tests/test_gitlab.py`

**Interfaces:**
- Consumes: `Config` (Task 1).
- Produces: `class GitLab(cfg: Config)` with `get(path, **params) -> object` (single JSON), `get_all(path, **params) -> list` (follows `X-Next-Page` pagination, `per_page=100`), `post(path, **data) -> object`, `delete(path) -> None`; module function `rebase_url(url: str, base: str) -> str`. All requests: header `PRIVATE-TOKEN`, timeout 30s. HTTP >= 400 raises `GitLabError(status, path)` **except** `get` with `ok404=True` returns `None` on 404.

- [ ] **Step 1: Write the failing test**

```python
# forge-guard/tests/test_gitlab.py
import responses
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab, rebase_url
from tests.test_config import BASE

def _gl():
    return GitLab(load_config(BASE))

def test_rebase_url_forces_configured_host():
    assert rebase_url("https://gitlab.internal.example/g/p/-/commit/abc",
                      "https://gitlab.example.com") == "https://gitlab.example.com/g/p/-/commit/abc"

@responses.activate
def test_get_all_paginates():
    responses.get("https://gitlab.example.com/api/v4/projects", json=[{"id": 1}],
                  headers={"X-Next-Page": "2"},
                  match=[responses.matchers.query_param_matcher(
                      {"per_page": "100", "page": "1"}, strict_match=False)])
    responses.get("https://gitlab.example.com/api/v4/projects", json=[{"id": 2}],
                  headers={"X-Next-Page": ""})
    assert [p["id"] for p in _gl().get_all("/projects")] == [1, 2]
    assert responses.calls[0].request.headers["PRIVATE-TOKEN"] == "tok"

@responses.activate
def test_get_404_ok_returns_none_and_raises_otherwise():
    responses.get("https://gitlab.example.com/api/v4/projects/1/protected_branches/main",
                  json={"message": "404"}, status=404)
    assert _gl().get("/projects/1/protected_branches/main", ok404=True) is None
    responses.get("https://gitlab.example.com/api/v4/boom", json={}, status=500)
    import pytest
    from forgeguard.gitlab import GitLabError
    with pytest.raises(GitLabError):
        _gl().get("/boom")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd forge-guard && python3 -m pytest tests/test_gitlab.py -q`
Expected: FAIL (`ModuleNotFoundError` / `ImportError`)

- [ ] **Step 3: Write minimal implementation**

```python
# forge-guard/forgeguard/gitlab.py
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
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers["PRIVATE-TOKEN"] = cfg.token

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd forge-guard && python3 -m pytest tests/test_gitlab.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add forge-guard/forgeguard/gitlab.py forge-guard/tests/test_gitlab.py
git commit -m "feat(forge-guard): GitLab client with pagination and URL rebasing"
```

---

### Task 3: State store

**Files:**
- Create: `forge-guard/forgeguard/state.py`, `forge-guard/tests/test_state.py`

**Interfaces:**
- Produces: `class State` with `load(path) -> State` (missing file → empty state), `save(path)` (atomic: `<path>.tmp` + `os.replace`, parent dirs created), `get_tip(project_id: int, branch: str) -> str | None`, `set_tip(project_id, branch, sha)`, `get_cursor(name: str) -> str | None`, `set_cursor(name, value)`, `flag_once(key: str) -> bool` (True the first time a key is seen, False after — used for once-per-user "missing mapping" alerts).

- [ ] **Step 1: Write the failing test**

```python
# forge-guard/tests/test_state.py
from forgeguard.state import State

def test_roundtrip_and_atomic_write(tmp_path):
    p = str(tmp_path / "deep" / "state.json")
    st = State.load(p)
    assert st.get_tip(1, "main") is None
    st.set_tip(1, "main", "abc")
    st.set_cursor("mr_updated_after", "2026-08-07T00:00:00Z")
    st.save(p)
    st2 = State.load(p)
    assert st2.get_tip(1, "main") == "abc"
    assert st2.get_cursor("mr_updated_after") == "2026-08-07T00:00:00Z"
    assert not list(tmp_path.glob("**/*.tmp"))

def test_flag_once():
    st = State.load("/nonexistent/x.json")
    assert st.flag_once("usermap:alice") is True
    assert st.flag_once("usermap:alice") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd forge-guard && python3 -m pytest tests/test_state.py -q`
Expected: FAIL (import error)

- [ ] **Step 3: Write minimal implementation**

```python
# forge-guard/forgeguard/state.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd forge-guard && python3 -m pytest tests/test_state.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add forge-guard/forgeguard/state.py forge-guard/tests/test_state.py
git commit -m "feat(forge-guard): atomic state store"
```

---

### Task 4: Protection enforcement + violation classification

**Files:**
- Create: `forge-guard/forgeguard/protect.py`, `forge-guard/tests/test_protect.py`

**Interfaces:**
- Consumes: `GitLab` (Task 2), `State` (Task 3).
- Produces:
  - `ensure_protection(gl, project_id: int, branch: str) -> str | None` — returns `"protected"` if it changed anything, `None` if already correct. Correct = `push_access_levels == [0]`, `merge_access_levels == [30]`, `allow_force_push is False`. Fix = DELETE + POST (CE protected-branch API has no partial update).
  - `classify_move(gl, project_id: int, branch: str, old: str, new: str) -> dict` — `{"kind": "force_push"}` when `GET /projects/:id/repository/merge_base?refs[]=old&refs[]=new` id ≠ old; else `{"kind": "ok", "unmr_commits": [sha, ...]}` where `unmr_commits` are commits from `GET /repository/compare?from=old&to=new` (capped at 50) whose `GET /repository/commits/:sha/merge_requests` has **no** entry with `state == "merged"` and `target_branch == branch`.

- [ ] **Step 1: Write the failing test**

```python
# forge-guard/tests/test_protect.py
import responses
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab
from forgeguard.protect import ensure_protection, classify_move
from tests.test_config import BASE

API = "https://gitlab.example.com/api/v4"

def _gl():
    return GitLab(load_config(BASE))

@responses.activate
def test_ensure_protection_fixes_wrong_levels():
    responses.get(f"{API}/projects/7/protected_branches/develop", json={
        "name": "develop", "allow_force_push": True,
        "push_access_levels": [{"access_level": 40}],
        "merge_access_levels": [{"access_level": 30}]})
    responses.delete(f"{API}/projects/7/protected_branches/develop")
    responses.post(f"{API}/projects/7/protected_branches", json={"name": "develop"})
    assert ensure_protection(_gl(), 7, "develop") == "protected"

@responses.activate
def test_ensure_protection_noop_when_correct():
    responses.get(f"{API}/projects/7/protected_branches/main", json={
        "name": "main", "allow_force_push": False,
        "push_access_levels": [{"access_level": 0}],
        "merge_access_levels": [{"access_level": 30}]})
    assert ensure_protection(_gl(), 7, "main") is None
    assert len(responses.calls) == 1  # read-only when already correct

@responses.activate
def test_classify_force_push():
    responses.get(f"{API}/projects/7/repository/merge_base", json={"id": "aaa"})
    out = classify_move(_gl(), 7, "main", "old", "new")
    assert out == {"kind": "force_push"}

@responses.activate
def test_classify_unmr_commit():
    responses.get(f"{API}/projects/7/repository/merge_base", json={"id": "old"})
    responses.get(f"{API}/projects/7/repository/compare",
                  json={"commits": [{"id": "c1"}, {"id": "c2"}]})
    responses.get(f"{API}/projects/7/repository/commits/c1/merge_requests",
                  json=[{"state": "merged", "target_branch": "main"}])
    responses.get(f"{API}/projects/7/repository/commits/c2/merge_requests", json=[])
    out = classify_move(_gl(), 7, "main", "old", "new")
    assert out == {"kind": "ok", "unmr_commits": ["c2"]}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd forge-guard && python3 -m pytest tests/test_protect.py -q`
Expected: FAIL (import error)

- [ ] **Step 3: Write minimal implementation**

```python
# forge-guard/forgeguard/protect.py
from __future__ import annotations
from .gitlab import GitLab

def _levels(entries) -> list[int]:
    return sorted(e["access_level"] for e in entries or [])

def ensure_protection(gl: GitLab, project_id: int, branch: str):
    cur = gl.get(f"/projects/{project_id}/protected_branches/{branch}", ok404=True)
    if cur and _levels(cur["push_access_levels"]) == [0] \
           and _levels(cur["merge_access_levels"]) == [30] \
           and cur.get("allow_force_push") is False:
        return None
    if cur:
        gl.delete(f"/projects/{project_id}/protected_branches/{branch}")
    gl.post(f"/projects/{project_id}/protected_branches",
            name=branch, push_access_level=0, merge_access_level=30,
            allow_force_push=False)
    return "protected"

def classify_move(gl: GitLab, project_id: int, branch: str, old: str, new: str) -> dict:
    base = gl.get(f"/projects/{project_id}/repository/merge_base",
                  **{"refs[]": [old, new]})
    if base["id"] != old:
        return {"kind": "force_push"}
    cmp = gl.get(f"/projects/{project_id}/repository/compare",
                 **{"from": old, "to": new})
    unmr = []
    for c in cmp.get("commits", [])[:50]:
        mrs = gl.get(f"/projects/{project_id}/repository/commits/{c['id']}/merge_requests")
        if not any(m.get("state") == "merged" and m.get("target_branch") == branch
                   for m in mrs):
            unmr.append(c["id"])
    return {"kind": "ok", "unmr_commits": unmr}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd forge-guard && python3 -m pytest tests/test_protect.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add forge-guard/forgeguard/protect.py forge-guard/tests/test_protect.py
git commit -m "feat(forge-guard): protection enforcement and violation classification"
```

---

### Task 5: Feishu notifier

**Files:**
- Create: `forge-guard/forgeguard/feishu.py`, `forge-guard/tests/test_feishu.py`

**Interfaces:**
- Consumes: `Config` (Task 1).
- Produces: `class Feishu(cfg)` with `notify(text: str, at_gitlab_user: str | None = None) -> None`: fetches tenant token (`POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal`, cached per instance), loads usermap JSON `{gitlab_username: open_id}` from `cfg.usermap_path` (missing file → empty map), prepends `<at user_id="{open_id}"></at> ` when the user maps, sends `POST /open-apis/im/v1/messages?receive_id_type=chat_id` with `msg_type="text"`, `content=json.dumps({"text": ...})`, `receive_id=cfg.feishu_chat_id`. Returns silently on success; raises `RuntimeError` on non-0 Feishu `code`. Never raises on unmapped user.

- [ ] **Step 1: Write the failing test**

```python
# forge-guard/tests/test_feishu.py
import json, responses
from forgeguard.config import load_config
from forgeguard.feishu import Feishu
from tests.test_config import BASE

FA = "https://open.feishu.cn/open-apis"

def _feishu(tmp_path, usermap):
    p = tmp_path / "usermap.json"
    p.write_text(json.dumps(usermap))
    cfg = load_config(dict(BASE, FORGEGUARD_USERMAP=str(p)))
    return Feishu(cfg)

@responses.activate
def test_notify_with_mention(tmp_path):
    responses.post(f"{FA}/auth/v3/tenant_access_token/internal",
                   json={"code": 0, "tenant_access_token": "t-x"})
    responses.post(f"{FA}/im/v1/messages?receive_id_type=chat_id",
                   json={"code": 0})
    _feishu(tmp_path, {"alice": "ou_123"}).notify("MR needs tests", at_gitlab_user="alice")
    body = json.loads(responses.calls[1].request.body)
    assert body["receive_id"] == "oc_x"
    assert '<at user_id="ou_123"></at>' in json.loads(body["content"])["text"]
    assert responses.calls[1].request.headers["Authorization"] == "Bearer t-x"

@responses.activate
def test_notify_unmapped_user_sends_without_at(tmp_path):
    responses.post(f"{FA}/auth/v3/tenant_access_token/internal",
                   json={"code": 0, "tenant_access_token": "t-x"})
    responses.post(f"{FA}/im/v1/messages?receive_id_type=chat_id", json={"code": 0})
    _feishu(tmp_path, {}).notify("hello", at_gitlab_user="ghost")
    assert "<at" not in json.loads(
        json.loads(responses.calls[1].request.body)["content"])["text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd forge-guard && python3 -m pytest tests/test_feishu.py -q`
Expected: FAIL (import error)

- [ ] **Step 3: Write minimal implementation**

```python
# forge-guard/forgeguard/feishu.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd forge-guard && python3 -m pytest tests/test_feishu.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add forge-guard/forgeguard/feishu.py forge-guard/tests/test_feishu.py
git commit -m "feat(forge-guard): Feishu notifier with @mention usermap"
```

---

### Task 6: Sweep orchestrator + CLI

**Files:**
- Create: `forge-guard/forgeguard/cli.py`, `forge-guard/tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `run_sweep(gl, state, feishu, cfg) -> dict` summary `{"projects": int, "protected": int, "force_push": int, "unmr": int}`; CLI entrypoints `python3 -m forgeguard.cli sweep` and `... review` (review wired in Task 7). Sweep flow per project (from `gl.get_all("/projects", archived=False)`, skipping `cfg.exclude` by `path_with_namespace`):
  1. list branches once (`/projects/:id/repository/branches`), intersect with `cfg.branches`;
  2. `ensure_protection` each; on change → `feishu.notify` "🔒 protected {project}/{branch}";
  3. tip moved vs state → `classify_move`; `force_push` → notify "⚠️ FORCE PUSH on {project} {branch} by {commit author}: {rebased url}"; `unmr_commits` → notify per commit "⚠️ direct merge without MR on {project} {branch}: {rebased url}" with `at_gitlab_user=` the commit's `author_name`-mapped username from the branch commit payload (`committer` fallback);
  4. always `state.set_tip` to the new tip; save state at end.
  First-ever sighting of a branch (no stored tip) records the tip **without** classifying — no alert storm on first run.

- [ ] **Step 1: Write the failing test**

```python
# forge-guard/tests/test_cli.py
import responses
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab
from forgeguard.state import State
from forgeguard.cli import run_sweep
from tests.test_config import BASE

API = "https://gitlab.example.com/api/v4"

class FakeFeishu:
    def __init__(self): self.sent = []
    def notify(self, text, at_gitlab_user=None): self.sent.append((text, at_gitlab_user))

def _project_fixtures(tip):
    responses.get(f"{API}/projects", json=[
        {"id": 7, "path_with_namespace": "g/app",
         "web_url": "https://gitlab.internal.example/g/app"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7/repository/branches", json=[
        {"name": "main", "commit": {"id": tip, "author_name": "alice"}},
        {"name": "feature/x", "commit": {"id": "zzz"}}])
    responses.get(f"{API}/projects/7/protected_branches/main", json={
        "name": "main", "allow_force_push": False,
        "push_access_levels": [{"access_level": 0}],
        "merge_access_levels": [{"access_level": 30}]})

@responses.activate
def test_first_run_records_tip_silently(tmp_path):
    _project_fixtures("t1")
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    out = run_sweep(GitLab(cfg), st, fk, cfg)
    assert out == {"projects": 1, "protected": 0, "force_push": 0, "unmr": 0}
    assert st.get_tip(7, "main") == "t1"
    assert fk.sent == []

@responses.activate
def test_force_push_alerts(tmp_path):
    _project_fixtures("t2")
    responses.get(f"{API}/projects/7/repository/merge_base", json={"id": "not-t1"})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st = State.load(cfg.state_path)
    st.set_tip(7, "main", "t1")
    fk = FakeFeishu()
    out = run_sweep(GitLab(cfg), st, fk, cfg)
    assert out["force_push"] == 1
    assert "FORCE PUSH" in fk.sent[0][0]
    assert "https://gitlab.example.com/" in fk.sent[0][0]
    assert st.get_tip(7, "main") == "t2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd forge-guard && python3 -m pytest tests/test_cli.py -q`
Expected: FAIL (import error)

- [ ] **Step 3: Write minimal implementation**

```python
# forge-guard/forgeguard/cli.py
from __future__ import annotations
import sys
from .config import Config, load_config
from .feishu import Feishu
from .gitlab import GitLab, rebase_url
from .protect import classify_move, ensure_protection
from .state import State

def _commit_url(project: dict, sha: str, base: str) -> str:
    return rebase_url(f"{project['web_url']}/-/commit/{sha}", base)

def run_sweep(gl: GitLab, state: State, feishu, cfg: Config) -> dict:
    out = {"projects": 0, "protected": 0, "force_push": 0, "unmr": 0}
    for project in gl.get_all("/projects", archived=False):
        if project["path_with_namespace"] in cfg.exclude:
            continue
        out["projects"] += 1
        pid, path = project["id"], project["path_with_namespace"]
        branches = gl.get(f"/projects/{pid}/repository/branches")
        for b in branches:
            name = b["name"]
            if name not in cfg.branches:
                continue
            if ensure_protection(gl, pid, name):
                out["protected"] += 1
                feishu.notify(f"🔒 protected {path}/{name} (MR-only, no force push)")
            new_tip, old_tip = b["commit"]["id"], state.get_tip(pid, name)
            if old_tip and old_tip != new_tip:
                move = classify_move(gl, pid, name, old_tip, new_tip)
                if move["kind"] == "force_push":
                    out["force_push"] += 1
                    feishu.notify(
                        f"⚠️ FORCE PUSH on {path} {name} "
                        f"(last author {b['commit'].get('author_name', '?')}): "
                        f"{_commit_url(project, new_tip, cfg.gitlab_url)}")
                else:
                    for sha in move["unmr_commits"]:
                        out["unmr"] += 1
                        feishu.notify(
                            f"⚠️ merge without MR on {path} {name}: "
                            f"{_commit_url(project, sha, cfg.gitlab_url)}",
                            at_gitlab_user=b["commit"].get("author_name"))
            state.set_tip(pid, name, new_tip)
    state.save(cfg.state_path)
    return out

def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in {"sweep", "review", "inject-gate"}:
        print("usage: forgeguard sweep|review|inject-gate", file=sys.stderr)
        return 2
    cfg = load_config()
    gl, state = GitLab(cfg), State.load(cfg.state_path)
    if args[0] == "sweep":
        print(run_sweep(gl, state, Feishu(cfg), cfg))
        return 0
    if args[0] == "review":
        from .review import run_review_tick
        print(run_review_tick(gl, state, Feishu(cfg), cfg))
        return 0
    from .review import inject_gate
    return inject_gate(gl, cfg, apply="--apply" in args)

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd forge-guard && python3 -m pytest tests/test_cli.py -q`
Expected: 2 passed (review import is lazy, so its absence doesn't break sweep)

- [ ] **Step 5: Commit**

```bash
git add forge-guard/forgeguard/cli.py forge-guard/tests/test_cli.py
git commit -m "feat(forge-guard): sweep orchestrator and CLI"
```

---

### Task 7: AI review lane

**Files:**
- Create: `forge-guard/forgeguard/review.py`, `forge-guard/tests/test_review.py`

**Interfaces:**
- Consumes: `GitLab`, `State`, `Feishu`, `Config`; `rebase_url`.
- Produces:
  - `run_claude(prompt: str) -> dict` — subprocess `["claude", "-p", "--output-format", "text"]`, prompt on stdin, timeout 300s; extracts the first `{...}` JSON object from stdout (`json.loads` of the substring between the first `{` and the last `}`); raises `RuntimeError` on nonzero exit or unparseable output.
  - `run_review_tick(gl, state, feishu, cfg) -> dict` `{"reviewed": int, "skipped_large": int}`:
    1. `GET /merge_requests?scope=all&state=opened&updated_after={cursor}` (cursor `mr_updated_after`, default omitted on first run), client-filter `target_branch in cfg.branches` and project not excluded;
    2. skip MRs whose head `sha` was already reviewed (state cursor `reviewed:{project_id}:{iid}` == sha);
    3. diff via `GET /projects/:pid/merge_requests/:iid/changes`; total diff bytes > `cfg.diff_cap_bytes` → post marker note "too large for auto-review", count `skipped_large`, still record sha;
    4. else build prompt (below), `run_claude`, upsert marker note, `POST .../approve` when `verdict == "clean"` (swallow `GitLabError` — approvals may be restricted), Feishu verdict `@author` with MR URL + head-commit URL (both rebased);
    5. record sha; advance `mr_updated_after` to max `updated_at` seen **only after** all MRs processed without exception; save state.
  - Marker: notes begin `<!-- forge-guard-review -->`; existing own note → `PUT` update, else `POST`.
  - Verdict JSON schema (from prompt): `{"verdict": "clean"|"issues", "summary": str, "issues": [{"severity": "high"|"medium"|"low", "file": str, "note": str}], "tests_opinion": str}`.
  - Prompt template (module constant `PROMPT`):

```
You are reviewing a GitLab merge request for an internal team.
Respond with ONLY a JSON object, no prose, matching:
{"verdict":"clean"|"issues","summary":"...","issues":[{"severity":"high|medium|low","file":"...","note":"..."}],"tests_opinion":"..."}
Rules: verdict "issues" if any high/medium issue OR feature changes lack test changes.
Be terse. Max 6 issues, most severe first.

MR title: {title}
Target branch: {target}
Description:
{description}

Diff:
{diff}
```

  - `inject_gate(gl, cfg, apply: bool) -> int` — stub in this task: prints "inject-gate: implemented in ci-template rollout (Task 8)" and returns 0 when `apply` is False, returns 2 with an error message when `--apply` is passed before Task 8's deployment steps are verified.

- [ ] **Step 1: Write the failing test**

```python
# forge-guard/tests/test_review.py
import json, responses
from unittest.mock import patch
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab
from forgeguard.state import State
from forgeguard.review import run_claude, run_review_tick
from tests.test_config import BASE
from tests.test_cli import FakeFeishu

API = "https://gitlab.example.com/api/v4"

def test_run_claude_parses_json_from_noise():
    fake = type("R", (), {"returncode": 0,
                          "stdout": 'note\n{"verdict":"clean","summary":"ok","issues":[],"tests_opinion":"fine"}\n',
                          "stderr": ""})()
    with patch("forgeguard.review.subprocess.run", return_value=fake):
        assert run_claude("x")["verdict"] == "clean"

@responses.activate
def test_review_tick_posts_note_approves_and_notifies(tmp_path):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 5, "project_id": 7, "sha": "head1", "title": "feat: x",
        "description": "", "target_branch": "develop",
        "author": {"username": "alice"}, "updated_at": "2026-08-07T10:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/5"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/5/changes",
                  json={"changes": [{"diff": "+ hello"}]})
    responses.get(f"{API}/projects/7/merge_requests/5/notes", json=[],
                  headers={"X-Next-Page": ""})
    responses.post(f"{API}/projects/7/merge_requests/5/notes", json={"id": 1})
    responses.post(f"{API}/projects/7/merge_requests/5/approve", json={})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    st, fk = State.load(cfg.state_path), FakeFeishu()
    verdict = {"verdict": "clean", "summary": "ok", "issues": [], "tests_opinion": "has tests"}
    with patch("forgeguard.review.run_claude", return_value=verdict):
        out = run_review_tick(GitLab(cfg), st, fk, cfg)
    assert out == {"reviewed": 1, "skipped_large": 0}
    assert fk.sent[0][1] == "alice"
    assert "https://gitlab.example.com/g/app/-/merge_requests/5" in fk.sent[0][0]
    assert "https://gitlab.example.com/g/app/-/commit/head1" in fk.sent[0][0]
    assert st.get_cursor("reviewed:7:5") == "head1"
    assert st.get_cursor("mr_updated_after") == "2026-08-07T10:00:00Z"

@responses.activate
def test_oversized_diff_skipped(tmp_path):
    responses.get(f"{API}/merge_requests", json=[{
        "iid": 6, "project_id": 7, "sha": "h2", "title": "big", "description": "",
        "target_branch": "main", "author": {"username": "bob"},
        "updated_at": "2026-08-07T11:00:00Z",
        "web_url": "https://gitlab.internal.example/g/app/-/merge_requests/6"}],
        headers={"X-Next-Page": ""})
    responses.get(f"{API}/projects/7", json={
        "path_with_namespace": "g/app",
        "web_url": "https://gitlab.internal.example/g/app"})
    responses.get(f"{API}/projects/7/merge_requests/6/changes",
                  json={"changes": [{"diff": "x" * 400_000}]})
    responses.get(f"{API}/projects/7/merge_requests/6/notes", json=[],
                  headers={"X-Next-Page": ""})
    responses.post(f"{API}/projects/7/merge_requests/6/notes", json={"id": 2})
    cfg = load_config(dict(BASE, FORGEGUARD_STATE=str(tmp_path / "s.json")))
    out = run_review_tick(GitLab(cfg), State.load(cfg.state_path), FakeFeishu(), cfg)
    assert out == {"reviewed": 0, "skipped_large": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd forge-guard && python3 -m pytest tests/test_review.py -q`
Expected: FAIL (import error)

- [ ] **Step 3: Write minimal implementation**

```python
# forge-guard/forgeguard/review.py
from __future__ import annotations
import json, subprocess
from .config import Config
from .gitlab import GitLab, GitLabError, rebase_url
from .state import State

MARKER = "<!-- forge-guard-review -->"

PROMPT = """You are reviewing a GitLab merge request for an internal team.
Respond with ONLY a JSON object, no prose, matching:
{{"verdict":"clean"|"issues","summary":"...","issues":[{{"severity":"high|medium|low","file":"...","note":"..."}}],"tests_opinion":"..."}}
Rules: verdict "issues" if any high/medium issue OR feature changes lack test changes.
Be terse. Max 6 issues, most severe first.

MR title: {title}
Target branch: {target}
Description:
{description}

Diff:
{diff}
"""

def run_claude(prompt: str) -> dict:
    r = subprocess.run(["claude", "-p", "--output-format", "text"],
                       input=prompt, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr[:200]}")
    out = r.stdout
    try:
        return json.loads(out[out.index("{"):out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"unparseable claude output: {out[:200]}") from e

def _upsert_note(gl: GitLab, pid: int, iid: int, body: str) -> None:
    for note in gl.get_all(f"/projects/{pid}/merge_requests/{iid}/notes"):
        if str(note.get("body", "")).startswith(MARKER):
            gl.put(f"/projects/{pid}/merge_requests/{iid}/notes/{note['id']}", body=body)
            return
    gl.post(f"/projects/{pid}/merge_requests/{iid}/notes", body=body)

def _render_note(v: dict) -> str:
    lines = [MARKER, f"**forge-guard review — {v['verdict']}**", "", v["summary"], ""]
    for i in v.get("issues", []):
        lines.append(f"- **{i['severity']}** `{i['file']}` — {i['note']}")
    lines += ["", f"_Tests: {v.get('tests_opinion', '')}_"]
    return "\n".join(lines)

def run_review_tick(gl: GitLab, state: State, feishu, cfg: Config) -> dict:
    out = {"reviewed": 0, "skipped_large": 0}
    params = {"scope": "all", "state": "opened"}
    cursor = state.get_cursor("mr_updated_after")
    if cursor:
        params["updated_after"] = cursor
    mrs = [m for m in gl.get_all("/merge_requests", **params)
           if m["target_branch"] in cfg.branches]
    max_updated = cursor or ""
    projects: dict[int, dict] = {}
    for mr in mrs:
        pid, iid, sha = mr["project_id"], mr["iid"], mr["sha"]
        project = projects.setdefault(pid, gl.get(f"/projects/{pid}"))
        if project["path_with_namespace"] in cfg.exclude:
            continue
        if state.get_cursor(f"reviewed:{pid}:{iid}") == sha:
            max_updated = max(max_updated, mr["updated_at"])
            continue
        changes = gl.get(f"/projects/{pid}/merge_requests/{iid}/changes")
        diff = "\n".join(c.get("diff", "") for c in changes.get("changes", []))
        mr_url = rebase_url(mr["web_url"], cfg.gitlab_url)
        commit_url = rebase_url(f"{project['web_url']}/-/commit/{sha}", cfg.gitlab_url)
        if len(diff.encode()) > cfg.diff_cap_bytes:
            _upsert_note(gl, pid, iid, f"{MARKER}\nMR too large for auto-review "
                                       f"({len(diff.encode())} bytes > cap).")
            out["skipped_large"] += 1
        else:
            v = run_claude(PROMPT.format(title=mr["title"], target=mr["target_branch"],
                                         description=mr.get("description") or "",
                                         diff=diff))
            _upsert_note(gl, pid, iid, _render_note(v))
            if v["verdict"] == "clean":
                try:
                    gl.post(f"/projects/{pid}/merge_requests/{iid}/approve")
                except GitLabError:
                    pass
            author = mr["author"]["username"]
            feishu.notify(
                f"📝 review {v['verdict']}: {mr['title']}\n{v['summary']}\n"
                f"MR: {mr_url}\ncommit: {commit_url}",
                at_gitlab_user=author)
            if author not in feishu.usermap and state.flag_once(f"usermap:{author}"):
                feishu.notify(f"ℹ️ no Feishu mapping for GitLab user '{author}' — "
                              f"add to forge-guard-usermap.json to enable @mentions")
            out["reviewed"] += 1
        state.set_cursor(f"reviewed:{pid}:{iid}", sha)
        max_updated = max(max_updated, mr["updated_at"])
    if max_updated:
        state.set_cursor("mr_updated_after", max_updated)
    state.save(cfg.state_path)
    return out

def inject_gate(gl: GitLab, cfg: Config, apply: bool) -> int:
    if apply:
        print("inject-gate --apply is gated on runner verification (see spec "
              "'To verify'); run without --apply for a dry-run list.")
        return 2
    print("inject-gate: dry-run — deployment steps land with the ci-template "
          "rollout (plan Task 8).")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd forge-guard && python3 -m pytest tests/test_review.py -q`
Expected: 3 passed

- [ ] **Step 5: Run the whole suite**

Run: `cd forge-guard && python3 -m pytest -q`
Expected: all tests from Tasks 1–7 pass

- [ ] **Step 6: Commit**

```bash
git add forge-guard/forgeguard/review.py forge-guard/tests/test_review.py
git commit -m "feat(forge-guard): advisory AI review lane"
```

---

### Task 8: Quality-gate CI template

**Files:**
- Create: `forge-guard/ci-template/gate.yml`, `forge-guard/ci-template/check_mr.py`, `forge-guard/tests/test_gate_script.py`

**Interfaces:**
- Produces: `check_mr.py` exit 0 = pass, exit 1 = fail. Pure stdlib; runs inside GitLab CI where it inspects `git diff` between `CI_MERGE_REQUEST_DIFF_BASE_SHA` and `HEAD`. Functions: `classify(changed_paths: list[str], commit_messages: list[str]) -> str` returning `"pass"`, `"fail-needs-tests"`, or `"skip"`; test-path convention: any path segment `tests/`, `test/`, `__tests__/`, or filename starting `test_` / ending `_test.py|.test.ts|.spec.ts|.test.js|.spec.js|Test.java`; feature signal: any commit message starting `feat` OR ≥3 changed non-test source files (`.py .ts .js .java .go .rb .php .vue .tsx`); `Gate-Skip:` trailer in any commit message → `"skip"` (gate passes; the sweep alerts on it separately via commit message scan in `classify_move` — out of scope here, the CI job prints a loud warning).

- [ ] **Step 1: Write the failing test**

```python
# forge-guard/tests/test_gate_script.py
import importlib.util, pathlib

spec = importlib.util.spec_from_file_location(
    "check_mr", pathlib.Path(__file__).parent.parent / "ci-template" / "check_mr.py")
check_mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_mr)

def test_feature_without_tests_fails():
    assert check_mr.classify(["src/api.py", "src/db.py", "src/models.py"],
                             ["feat: add billing"]) == "fail-needs-tests"

def test_feature_with_tests_passes():
    assert check_mr.classify(["src/api.py", "tests/test_api.py"],
                             ["feat: add billing"]) == "pass"

def test_non_feature_small_change_passes():
    assert check_mr.classify(["README.md"], ["docs: typo"]) == "pass"

def test_gate_skip_trailer_skips():
    assert check_mr.classify(["src/api.py"] * 5,
                             ["hotfix\n\nGate-Skip: prod incident"]) == "skip"

def test_three_source_files_is_feature_even_without_feat_prefix():
    assert check_mr.classify(["a.py", "b.py", "c.py"], ["update stuff"]) == "fail-needs-tests"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd forge-guard && python3 -m pytest tests/test_gate_script.py -q`
Expected: FAIL (file not found)

- [ ] **Step 3: Write the gate script and CI template**

```python
# forge-guard/ci-template/check_mr.py
"""forge-guard MR quality gate. Exit 0 pass, 1 fail. Stdlib only."""
from __future__ import annotations
import re, subprocess, sys

SOURCE_EXT = {".py", ".ts", ".js", ".java", ".go", ".rb", ".php", ".vue", ".tsx"}
_TEST_FILE = re.compile(r"(^test_|_test\.py$|\.test\.[jt]sx?$|\.spec\.[jt]sx?$|Test\.java$)")

def _is_test(path: str) -> bool:
    parts = path.split("/")
    if any(p in {"tests", "test", "__tests__"} for p in parts[:-1]):
        return True
    return bool(_TEST_FILE.search(parts[-1]))

def _is_source(path: str) -> bool:
    return any(path.endswith(ext) for ext in SOURCE_EXT) and not _is_test(path)

def classify(changed_paths: list[str], commit_messages: list[str]) -> str:
    if any("Gate-Skip:" in m for m in commit_messages):
        return "skip"
    feature = any(m.split(":")[0].strip().startswith("feat") for m in commit_messages) \
        or sum(1 for p in changed_paths if _is_source(p)) >= 3
    if not feature:
        return "pass"
    return "pass" if any(_is_test(p) for p in changed_paths) else "fail-needs-tests"

def main() -> int:
    base = subprocess.run(
        ["git", "merge-base", "origin/" + sys.argv[1], "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip() if len(sys.argv) > 1 \
        else subprocess.run(["git", "rev-parse", "HEAD~1"],
                            capture_output=True, text=True, check=True).stdout.strip()
    paths = subprocess.run(["git", "diff", "--name-only", base, "HEAD"],
                           capture_output=True, text=True, check=True).stdout.split()
    msgs = subprocess.run(["git", "log", "--format=%B%x00", f"{base}..HEAD"],
                          capture_output=True, text=True, check=True).stdout.split("\x00")
    verdict = classify(paths, [m for m in msgs if m.strip()])
    if verdict == "skip":
        print("⚠️ Gate-Skip trailer present — gate bypassed (audited).")
        return 0
    if verdict == "fail-needs-tests":
        print("❌ forge-guard gate: feature changes with no test changes.\n"
              "Add or update tests for the changed behavior, or (emergencies only)\n"
              "add a 'Gate-Skip: <reason>' trailer to a commit message.")
        return 1
    print("✅ forge-guard gate passed.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

```yaml
# forge-guard/ci-template/gate.yml
# Included per-project as:
#   include:
#     - project: forge/quality-gate
#       file: gate.yml
forge-guard-gate:
  stage: test
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  image: alpine:3.20
  before_script:
    - apk add --no-cache git python3
    - git fetch origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" --depth=200
  script:
    - python3 check_mr.py "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd forge-guard && python3 -m pytest tests/test_gate_script.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add forge-guard/ci-template forge-guard/tests/test_gate_script.py
git commit -m "feat(forge-guard): MR quality-gate CI template"
```

**Deployment note (NOT done by the implementer — gated on the spec's "To verify" items):** create `forge/quality-gate` project on the forge, push `gate.yml` + `check_mr.py`, verify a runner picks up an MR pipeline in one pilot project, then implement `inject_gate --apply` (adds the include + flips `only_allow_merge_if_pipeline_succeeds`) as a follow-up once runners are confirmed. The same follow-up adds the sweep-side Feishu alert when a merged MR's commits carry a `Gate-Skip:` trailer (spec's "Gate-Skip used" alert).

---

### Task 9: launchd plists, README, estate registration

**Files:**
- Create: `forge-guard/launchd/com.forgeguard.sweep.plist.example`, `forge-guard/launchd/com.forgeguard.review.plist.example`, `forge-guard/README.md`
- Modify: `docs/agent-estate-architecture.md` (registry table), `README.md` (monorepo layout list, one line)

**Interfaces:** none (deployment artifacts).

- [ ] **Step 1: Write the plists**

Both follow the fleet pattern (see `team-memory-agent/deploy/com.forgeguard.teammem-daily.plist`): `EnvironmentVariables` empty (env sourced by wrapper), `ProgramArguments` = `/bin/zsh -lc 'source ~/.config/forge-guard/forge-guard.env && cd __REPO__/forge-guard && python3 -m forgeguard.cli sweep'`, logs to `~/Library/Logs/ForgeGuard/{sweep,review}.{log,err}`. Sweep: `StartCalendarInterval` every hour at minute 10. Review: `StartInterval` 900.

```xml
<!-- forge-guard/launchd/com.forgeguard.sweep.plist.example -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.forgeguard.sweep</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>-lc</string>
    <string>source ~/.config/forge-guard/forge-guard.env &amp;&amp; cd __REPO__/forge-guard &amp;&amp; python3 -m forgeguard.cli sweep</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>10</integer></dict>
  <key>StandardOutPath</key><string>__HOME__/Library/Logs/ForgeGuard/sweep.log</string>
  <key>StandardErrorPath</key><string>__HOME__/Library/Logs/ForgeGuard/sweep.err</string>
</dict></plist>
```

```xml
<!-- forge-guard/launchd/com.forgeguard.review.plist.example -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.forgeguard.review</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>-lc</string>
    <string>source ~/.config/forge-guard/forge-guard.env &amp;&amp; cd __REPO__/forge-guard &amp;&amp; python3 -m forgeguard.cli review</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>__HOME__/Library/Logs/ForgeGuard/review.log</string>
  <key>StandardErrorPath</key><string>__HOME__/Library/Logs/ForgeGuard/review.err</string>
</dict></plist>
```

- [ ] **Step 2: Validate plists**

Run: `for f in forge-guard/launchd/*.plist.example; do sed 's/__REPO__/x/;s/__HOME__/y/' "$f" | plutil -lint -- -; done`
Expected: `-: OK` twice

- [ ] **Step 3: Write README**

`forge-guard/README.md` must open with the estate contract line, then: what it is (3 lanes), env var table (every `FORGEGUARD_*` var with default), setup runbook (create `forge-guard.env`, create Feishu group + get chat_id, build usermap JSON from the teammem roster, install plists by copying `.example` with `__REPO__`/`__HOME__` substituted, `launchctl bootstrap`), rollout order copied from the spec, and Limitations (hourly detection latency; CE = approvals not required; gate deployment pending runner verification; Mac-offline = lanes pause, protection persists).

```markdown
# forge-guard

> **Tier 2 · deployment overlay + agent source** — enforces MR-only merges,
> detects force pushes and merges-without-MR, and posts advisory AI reviews
> for the org GitLab. Estate map:
> [../docs/agent-estate-architecture.md](../docs/agent-estate-architecture.md).
```
(then the sections above — implementer writes full prose, no placeholders)

- [ ] **Step 4: Register in estate docs**

In `docs/agent-estate-architecture.md`, registry table, add after the dev-agent overlay row:
`| ~/Workspace/forge-guard/forge-guard/ | 2 | GitLab guard: instance-wide MR-only protection sweep, violation alerts, advisory AI review |`
In monorepo `README.md` layout section, add one line for `forge-guard/` matching the existing per-agent line style.

- [ ] **Step 5: Run the full suite, then commit**

Run: `cd forge-guard && python3 -m pytest -q`
Expected: all green

```bash
git add forge-guard/launchd forge-guard/README.md README.md docs/agent-estate-architecture.md
git commit -m "feat(forge-guard): launchd ticks, README, estate registration"
```

---

## Deployment runbook (operator, post-implementation — not implementer tasks)

1. Mint admin PAT on the forge (Maintainer token cannot protect foreign projects) → `forge-guard.env`.
2. Create Feishu group "Gitlab Review Notification", add the bot, capture chat_id.
3. Build `forge-guard-usermap.json` from the teammem roster (gitlab username → feishu open_id).
4. Install both plists; watch first sweep log; confirm Feishu messages.
5. Verify runners (`/api/v4/runners/all`), then deploy `ci-template/` to `forge/quality-gate` and pilot one project before wiring `inject_gate --apply`.
