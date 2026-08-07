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
