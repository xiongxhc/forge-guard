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
