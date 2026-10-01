"""Execute shipped installation commands with local stand-ins, never real services."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEDULES = sorted(ROOT.glob("systemd/*.service.example")) + sorted(
    ROOT.glob("launchd/*.plist.example"))


@pytest.mark.parametrize("template", SCHEDULES, ids=lambda p: p.name)
def test_schedule_executes_from_standalone_checkout(template, tmp_path):
    checkout = tmp_path / "checkout & space # 50% $literal"
    python = checkout / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$READY" "$@"\n')
    python.chmod(0o755)
    home = tmp_path / "home with spaces"
    envfile = home / ".config/forge-guard/forge-guard.env"
    envfile.parent.mkdir(parents=True)
    envfile.write_text("READY=loaded\n")
    is_plist = template.suffixes[-2] == ".plist"
    kind = "launchd" if is_plist else "systemd"
    (checkout / kind).mkdir()
    shutil.copy2(template, checkout / kind / template.name)
    destination = home / ("Library/LaunchAgents" if is_plist else ".config/systemd/user")
    destination.mkdir(parents=True)
    section = (ROOT / "README.md").read_text().split(
        "**macOS (launchd):**" if is_plist else "**Linux (systemd):**", 1)[1]
    script = section.split(".venv/bin/python - <<'PY'\n", 1)[1].split("   PY", 1)[0]
    script = "\n".join(line[3:] for line in script.splitlines())
    env = {**os.environ, "HOME": str(home)}
    render = subprocess.run([sys.executable, "-c", script], cwd=checkout,
                            env=env, text=True, capture_output=True)
    assert render.returncode == 0, render.stderr
    rendered = (destination / template.name.removesuffix(".example")).read_text()
    if is_plist:
        command = plistlib.loads(rendered.encode())["ProgramArguments"][-1]
    else:
        # Simulate systemd's literal escapes and %h, before the shell sees it.
        rendered = rendered.replace("%%", "%").replace("$$", "$").replace("%h", str(home))
        command = shlex.split(next(line.split("=", 1)[1]
                                  for line in rendered.splitlines()
                                  if line.startswith("ExecStart=")))[-1]
    # Avoid interactive/login profiles: execute precisely the shipped shell body.
    result = subprocess.run(["/bin/bash", "-c", command], cwd=tmp_path,
                            env=env,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(checkout), "loaded", "-m",
                                           "forgeguard.cli", template.name.split(".")[-3]
                                           if is_plist
                                           else template.name.split("-")[-1].split(".")[0]]


@pytest.mark.parametrize("args", [["inject-gate"], ["inject-gate", "--apply"]])
def test_unsupported_inject_gate_needs_no_credentials(args, monkeypatch, capsys):
    from forgeguard.cli import main
    for key in list(os.environ):
        if key.startswith("FORGEGUARD_"):
            monkeypatch.delenv(key)
    assert main(args) == 2
    out = capsys.readouterr()
    assert "not implemented" in out.err
    assert "docs/quality-gate-setup.md" in out.err


def _gate_fetch(tmp_path, *, ca=None, status="200", transport="0", digest=None):
    template = (ROOT / "ci-template/quality-gate.gitlab-ci.yml").read_text()
    # Execute the real multi-line shell block that fetches and verifies the tool.
    lines = template.split("    - |\n", 1)[1].splitlines()
    body = []
    for line in lines:
        if line and not line.startswith("      "):
            break
        body.append(line[6:])
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    curl = fakebin / "curl"
    curl.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                    "from pathlib import Path\n"
                    "Path('curl-args.json').write_text(json.dumps(sys.argv[1:]))\n"
                    "Path('check_mr.py').write_text('verified tool\\n')\n"
                    "print(os.environ['HTTP_STATUS'], end='')\n"
                    "sys.exit(int(os.environ['TRANSPORT_EXIT']))\n")
    curl.chmod(0o755)
    env = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
           "CI_API_V4_URL": "https://gitlab.example.com/api/v4",
           "CI_JOB_TOKEN": "fixture-token", "QUALITY_GATE_TOOL_PROJECT": "123",
           "QUALITY_GATE_TOOL_REF": "a" * 40,
           "QUALITY_GATE_TOOL_SHA256": digest or hashlib.sha256(b"verified tool\n").hexdigest(),
           "CI_SERVER_TLS_CA_FILE": ca or "", "HTTP_STATUS": status,
           "TRANSPORT_EXIT": transport}
    result = subprocess.run(["/bin/sh", "-ec", "\n".join(body)], cwd=tmp_path,
                            env=env, text=True, capture_output=True)
    argsfile = tmp_path / "curl-args.json"
    return result, json.loads(argsfile.read_text()) if argsfile.exists() else []


def test_gate_fetch_uses_system_trust_and_verifies_digest(tmp_path):
    result, args = _gate_fetch(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "-k" not in args and "--insecure" not in args
    assert "--cacert" not in args


def test_gate_fetch_uses_explicit_ca_with_spaces(tmp_path):
    ca = tmp_path / "private ca.pem"
    ca.write_text("fixture")
    result, args = _gate_fetch(tmp_path, ca=str(ca))
    assert result.returncode == 0, result.stderr
    assert args[args.index("--cacert") + 1] == str(ca)


@pytest.mark.parametrize("kwargs", [
    {"status": "404"}, {"transport": "60"}, {"digest": "0" * 64},
    {"ca": "/nonexistent/ca.pem"},
])
def test_gate_fetch_fails_closed(tmp_path, kwargs):
    result, _ = _gate_fetch(tmp_path, **kwargs)
    assert result.returncode != 0
