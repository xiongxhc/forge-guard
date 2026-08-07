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
