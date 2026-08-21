from __future__ import annotations
from urllib.parse import quote

from .gitlab import GitLab

def _levels(entries) -> list[int]:
    return sorted(e["access_level"] for e in entries or [])

def ensure_protection(gl: GitLab, project_id: int, branch: str):
    enc = quote(branch, safe="")   # slashed names (adaa/uat) must be path-encoded
    cur = gl.get(f"/projects/{project_id}/protected_branches/{enc}", ok404=True)
    if cur and _levels(cur["push_access_levels"]) == [0] \
           and _levels(cur["merge_access_levels"]) == [30] \
           and cur.get("allow_force_push") is False:
        return None
    if cur:
        gl.delete(f"/projects/{project_id}/protected_branches/{enc}")
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
            unmr.append({"id": c["id"], "title": c.get("title", ""),
                         "author_name": c.get("author_name", "?")})
    return {"kind": "ok", "unmr_commits": unmr}
