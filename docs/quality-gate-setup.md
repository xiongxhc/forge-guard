# Install the quality gate on one pilot project

The gate runs in GitLab CI; it does not need ForgeGuard's scheduled reviewer or
Feishu. It checks for test-file changes alongside feature work, not whether the
tests are meaningful or pass. Keep the project's real test jobs.

The engine does not publish the checker, inject the include, change the project's
pipeline-success merge setting, or send Feishu alerts for `Gate-Skip`. Both
`inject-gate` and `inject-gate --apply` return an unsupported-command error. The
following is an operator-run deployment, first on one pilot, then separately
reviewed for each additional project.

## 1. Prepare a private ci-tools project and reviewed commit

Create a private GitLab project such as `your-group/ci-tools` with its package
registry enabled. Give the operators who maintain it appropriate repository and
package publication rights. In its checkout, copy these two files from the
reviewed ForgeGuard checkout, preserving the destination layout:

```text
ci-tools/
└── ci/
    ├── quality-gate.gitlab-ci.yml
    └── check_mr.py
```

Review and commit those files on a ci-tools branch, then publish that branch
normally (no force push). The Bash block below pushes and verifies its remote
head before any package upload. Record the **full 40-character commit SHA**.
Both the consumer include's `ref` and the package version below will use that
SHA. The template retains placeholders; each consumer supplies its numeric
ci-tools project ID, this commit SHA, and the checker's SHA-256 digest.

A commit pin identifies the template, while a registry package is separate
storage and can otherwise be replaced or duplicated. Disable duplicate generic
package uploads for this package under the group's package registry settings
(Owner access may be needed), restrict publishing rights, and never republish a
version. The consumer also pins the checker digest, so different bytes cannot
silently replace a previously reviewed package.

## 2. Publish and verify the checker package

Use Bash for this block. Set the URL to your GitLab's HTTPS URL, the numeric
ci-tools project ID, and its checkout path. For a private CA, export
`CURL_CA_BUNDLE` with the path to a trusted PEM bundle before running curl. Otherwise curl uses
system trust. Never disable certificate verification.

```bash
(
set -euo pipefail
GATE_GITLAB_URL='https://gitlab.example.com'
GATE_TOOL_PROJECT='123'                         # your ci-tools numeric project ID
GATE_TOOLS_CHECKOUT='/absolute/path/to/ci-tools'
test -z "$(git -C "$GATE_TOOLS_CHECKOUT" status --porcelain)" || { echo 'Commit/reconcile ci-tools changes first'; exit 1; }
GATE_TOOL_BRANCH=$(git -C "$GATE_TOOLS_CHECKOUT" symbolic-ref --short HEAD)
GATE_TOOL_COMMIT=$(git -C "$GATE_TOOLS_CHECKOUT" rev-parse HEAD)
git -C "$GATE_TOOLS_CHECKOUT" push origin "HEAD:refs/heads/$GATE_TOOL_BRANCH"
GATE_REMOTE_HEAD=$(git -C "$GATE_TOOLS_CHECKOUT" ls-remote --exit-code origin "refs/heads/$GATE_TOOL_BRANCH" | cut -f1)
test "$GATE_REMOTE_HEAD" = "$GATE_TOOL_COMMIT" || { echo 'Published ci-tools head differs; reconcile before continuing'; exit 1; }
GATE_PUBLISH_DIR=$(mktemp -d)
git -C "$GATE_TOOLS_CHECKOUT" show "$GATE_TOOL_COMMIT:ci/check_mr.py" > "$GATE_PUBLISH_DIR/check_mr.py"
GATE_TOOL_SHA256=$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$GATE_PUBLISH_DIR/check_mr.py")

# Dedicated publishing PAT with api scope and access to publish in ci-tools.
# Enter it at the prompt, not as a literal in the command or repository.
read -r -s -p 'ci-tools publishing PAT: ' GATE_PUBLISH_TOKEN
printf '\n'
GATE_PACKAGE_URL="$GATE_GITLAB_URL/api/v4/projects/$GATE_TOOL_PROJECT/packages/generic/quality-gate/$GATE_TOOL_COMMIT/check_mr.py"
GATE_STATUS=$(curl --silent --show-error \
  --header "PRIVATE-TOKEN: $GATE_PUBLISH_TOKEN" \
  --upload-file "$GATE_PUBLISH_DIR/check_mr.py" \
  --output "$GATE_PUBLISH_DIR/upload-response.json" --write-out '%{http_code}' \
  "$GATE_PACKAGE_URL")
test "$GATE_STATUS" = 201 || { echo "Package upload failed: HTTP $GATE_STATUS"; unset GATE_PUBLISH_TOKEN; exit 1; }
curl --fail --silent --show-error --header "PRIVATE-TOKEN: $GATE_PUBLISH_TOKEN" \
  --output "$GATE_PUBLISH_DIR/downloaded.py" "$GATE_PACKAGE_URL"
unset GATE_PUBLISH_TOKEN
cmp "$GATE_PUBLISH_DIR/check_mr.py" "$GATE_PUBLISH_DIR/downloaded.py"
printf 'Project: %s\nInclude ref/package version: %s\nChecker SHA-256: %s\n' \
  "$GATE_TOOL_PROJECT" "$GATE_TOOL_COMMIT" "$GATE_TOOL_SHA256"
)
```

Confirm `origin` names the same ci-tools project represented by
`GATE_TOOL_PROJECT` before running. This block stops on any failed command;
do not proceed after a failed push, upload, download, or comparison.
The local temporary directory contains checker bytes and upload
response only; remove it after verification. This PAT is for publication only;
do not put it into consumer CI variables. Jobs fetch using `CI_JOB_TOKEN`.
GitLab documents the [generic package authentication and publication API](https://docs.gitlab.com/user/packages/generic_packages/).

## 3. Grant both include access and package access

- Users who create/run consumer pipelines must be able to read the private
  ci-tools repository for `include:project`. Membership must also permit those
  users' jobs to read its packages (normally Reporter or greater on ci-tools).
- In **ci-tools → Settings → CI/CD → Job token permissions**, add the pilot
  consumer project to the inbound allowlist. This permits that consumer's job
  token to request the package; it does not grant its triggering user new
  privileges. If fine-grained job-token permissions are enabled, permit package
  reads as well.
- Keep the registry enabled and its visibility compatible with those members.
  Test using an ordinary developer's MR pipeline, not only an administrator's.

See [GitLab job-token access and allowlisting](https://docs.gitlab.com/ci/jobs/ci_job_token/).
403/404 can indicate permissions, allowlisting, visibility or an unpublished
package/version; the HTTP status alone does not identify the cause.

## 4. Add the include and pins to the pilot

In the consumer's `.gitlab-ci.yml`, substitute the three values printed above
and the actual ci-tools project path. Keep the same full commit SHA in both
places. Merge this with existing jobs/stages rather than replacing them.

```yaml
include:
  - project: your-group/ci-tools
    ref: '<full-reviewed-ci-tools-commit-sha>'
    file: /ci/quality-gate.gitlab-ci.yml

quality-gate:
  stage: test
  # Keep these rules in the consumer file: include-only rules are insufficient
  # to enable merge request pipelines. Integrate existing workflow rules too.
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
  variables:
    QUALITY_GATE_TOOL_PROJECT: '123'
    QUALITY_GATE_TOOL_REF: '<same-full-reviewed-ci-tools-commit-sha>'
    QUALITY_GATE_TOOL_SHA256: '<verified-sha256-of-check_mr.py>'
```

If the project declares stages without `test`, choose an existing appropriate
stage for this job. If it has `workflow: rules`, those must permit MR pipelines
too; don't discard existing branch/tag pipeline policy. The job must not be
optional, manual or `allow_failure: true`. Check the merged CI configuration.
See [GitLab's MR pipeline prerequisites](https://docs.gitlab.com/ci/pipelines/merge_request_pipelines/).

Provide a runner that can run the Alpine image, install its packages, and reach
GitLab over HTTPS. For private PKI, configure the runner to trust the CA for the
initial Git clone and expose its CA bundle as `CI_SERVER_TLS_CA_FILE` in the job.
The checker download uses that readable file or system trust and rejects TLS
errors; neither it nor the setup instructions use `-k`. Installing a CA only in
ForgeGuard's `REQUESTS_CA_BUNDLE` does not configure runner or curl trust.

## 5. Prove behavior, then opt into merge enforcement

Before changing merge settings, run real pilot MR pipelines:

1. A `feat:` commit changing a source file without any test-file changes must
   create an MR pipeline and fail **quality-gate**. Confirm it downloaded the
   expected package version, passed the digest check, and failed the feature
   rule, rather than failing due to network or setup errors.
2. Add the appropriate test-file changes and run the project's real tests. The
   quality-gate job and the required test jobs must pass on the current MR head.
3. In a disposable pilot MR, use `Gate-Skip: <reason>` deliberately and confirm
   the CI warning. It bypasses this gate; **no Feishu bypass notification is
   implemented**. The checker recognizes a `Gate-Skip:` substring; it does not
   validate the reason or caller authority. Set your team's authorization and
   log-retention policy.
4. Confirm an ordinary developer can run the same jobs and that a bad digest,
   inaccessible package or untrusted CA fails the gate.

Only after those checks, explicitly enable **Settings → Merge requests → Merge
checks → Pipelines must succeed** (`only_allow_merge_if_pipeline_succeeds`) on
the pilot project. Leave **Skipped pipelines are considered successful** off.
Repeat the failing/passing MR checks and verify the actual merge button is
blocked on failure and available only when the intended current-head pipeline
succeeds. GitLab prioritizes MR pipelines over branch pipelines; make sure the
gate is present in the pipeline GitLab uses for mergeability. See
[GitLab merge checks](https://docs.gitlab.com/user/project/merge_requests/auto_merge/).

Record the include SHA, package version/digest, pilot MR/pipeline URLs and
observed merge behavior before expanding to another project. If an installation
problem blocks work, revert the consumer include/pins or explicitly restore the
previous merge-check setting as an operator decision; ForgeGuard will not
automatically change it. For upgrades, publish a new reviewed version and update
include SHA, package version and digest together, repeating the pilot checks.
