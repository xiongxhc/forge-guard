import pytest
import responses
from forgeguard.config import load_config
from forgeguard.gitlab import GitLab, GitLabError
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
                  json={"commits": [{"id": "c1", "title": "ok", "author_name": "alice"},
                                    {"id": "c2", "title": "sneaky", "author_name": "bob"}]})
    responses.get(f"{API}/projects/7/repository/commits/c1/merge_requests",
                  json=[{"state": "merged", "target_branch": "main"}])
    responses.get(f"{API}/projects/7/repository/commits/c2/merge_requests", json=[])
    out = classify_move(_gl(), 7, "main", "old", "new")
    assert out == {"kind": "ok", "unmr_commits": [
        {"id": "c2", "title": "sneaky", "author_name": "bob"}]}

@responses.activate
def test_disconnected_existing_commits_are_non_fast_forward():
    responses.get(f"{API}/projects/7/repository/merge_base", status=404,
                  json={"message": "404 Merge Base Not Found"})
    for sha in ("old", "new"):
        responses.get(f"{API}/projects/7/repository/commits/{sha}", json={"id": sha})
    assert classify_move(_gl(), 7, "main", "old", "new") == {"kind": "force_push"}
    assert len(responses.calls) == 3

@pytest.mark.parametrize("missing", ["old", "new"])
@responses.activate
def test_disconnected_history_requires_both_commits_to_resolve(missing):
    responses.get(f"{API}/projects/7/repository/merge_base", status=404,
                  json={"message": "404 Merge Base Not Found"})
    for sha in ("old", "new"):
        responses.get(f"{API}/projects/7/repository/commits/{sha}",
                      status=404 if sha == missing else 200,
                      json={"message": "404 Commit Not Found"} if sha == missing else {"id": sha})
    with pytest.raises(GitLabError, match=f"/commits/{missing}"):
        classify_move(_gl(), 7, "main", "old", "new")

@pytest.mark.parametrize("commit", [{"id": "different"}, {}, []])
@responses.activate
def test_disconnected_history_rejects_unverified_commit_response(commit):
    responses.get(f"{API}/projects/7/repository/merge_base", status=404,
                  json={"message": "404 Merge Base Not Found"})
    responses.get(f"{API}/projects/7/repository/commits/old", json=commit)
    with pytest.raises(GitLabError, match="/repository/merge_base"):
        classify_move(_gl(), 7, "main", "old", "new")

@pytest.mark.parametrize("status,message", [
    (404, "404 Not Found"), (403, "403 Forbidden"),
    (429, "Too Many Requests"), (500, "Internal Server Error"),
    (403, "404 Merge Base Not Found"),
])
@responses.activate
def test_other_merge_base_failures_remain_errors(status, message):
    responses.get(f"{API}/projects/7/repository/merge_base", status=status,
                  json={"message": message})
    with pytest.raises(GitLabError) as failure:
        classify_move(_gl(), 7, "main", "old", "new")
    assert failure.value.status == status
    assert len(responses.calls) == 1
