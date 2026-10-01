# Public onboarding repair

Another team needs runnable installation and CI gate instructions without private
deployment knowledge. The current templates append an obsolete nested checkout
directory, and the gate setup omits package publication while promising features
that the engine does not implement.

## Scope and acceptance

1. Render and execute Linux/macOS example commands from a standalone checkout,
   including a path with spaces; document fresh-host destination directories.
2. Provide a manual pilot gate runbook: private ci-tools repository, reviewed
   commit pins, authenticated generic package publication, consumer permissions,
   TLS trust, actual failing/passing MR validation, then explicit merge policy.
3. Fail closed when fetching the checker; verify its digest. Report unsupported
   inject-gate without requiring credentials or claiming a successful dry run.
4. Correct coverage and Gate-Skip claims; state engine/operator ownership.

Tests first: schedule command execution, unsupported CLI, checker fetch failure
and success, and honest bypass output. Run the full pytest suite after fixes.
No live GitLab or host changes, new policy automation, or state concurrency
refactor. Package registry publication and real runner/MR behavior remain the
installing operator's acceptance checks.

## Verification checkpoint

- Added regression cases failed against the original templates, unsupported
  CLI, TLS/digest behavior and bypass claim; the final Python 3.12 suite passes
  all 126 tests (14 new cases).
- Schedule tests execute the README rendering snippets and resulting shell
  commands with spaces, XML characters, percent/dollar signs and fresh homes.
- Documented Bash/YAML parse; an offline execution of the publication block
  with stand-in git/curl verifies success plus fail-fast upload, download and
  byte-comparison failures without printing successful pins on failure.
- No real GitLab project, package, pipeline, policy or service was changed.
