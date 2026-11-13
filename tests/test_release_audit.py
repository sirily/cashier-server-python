"""Audit policy checks use only synthetic reports; never invoke real scanners."""

import json
import subprocess
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from scripts import audit

IMAGE = "ghcr.io/example/cashier:0.14.0"
PYTHON = {
    "dependencies": [{"name": "hatchling", "version": "1.27.0", "vulns": []}],
    "fixes": [],
}
NODE = {
    "auditReportVersion": 2,
    "vulnerabilities": {},
    "metadata": {
        "vulnerabilities": {"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 0, "total": 0},
        "dependencies": {"prod": 1, "dev": 1, "optional": 0, "peer": 0, "peerOptional": 0, "total": 2},
    },
}
CONTAINER = {
    "SchemaVersion": 2,
    "ArtifactName": IMAGE,
    "ArtifactType": "container_image",
    "Metadata": {"ImageID": "sha256:test"},
    "Results": [{"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg"}],
}


class FakeRunner:
    def __init__(self):
        self.reports: list[Any] = deepcopy([PYTHON, NODE, CONTAINER])
        self.codes = [0, 0, 0]
        self.stderr = ["", "", ""]
        self.errors = {}
        self.calls = []
        self.output: Path
        self.omit_python_report = False

    def __call__(self, command, **kwargs):
        index = {"uv": 0, "npm": 1, "trivy": 2}[command[0]]
        self.calls.append((command, kwargs))
        if index == 0:
            self.output = Path(command[command.index("--output") + 1])
        if index in self.errors:
            raise self.errors[index]
        report = self.reports[index]
        raw = report if isinstance(report, str) else json.dumps(report, indent=2)
        if index == 0:
            assert self.output.parent.exists()
            if not self.omit_python_report:
                self.output.write_text(raw, encoding="utf-8")
            stdout = "uv/pip startup output (not JSON)\n"
        else:
            stdout = raw
        return subprocess.CompletedProcess(command, self.codes[index], stdout, self.stderr[index])


def invoke(tmp_path, runner, *, image=True, summary=True):
    args = ["--image", IMAGE] if image else []
    if summary:
        args += ["--summary", str(tmp_path / "summary.md")]
    return audit.main(args, runner=runner)


def add_finding(runner, scanner, severity):
    if scanner == 0:
        runner.reports[0]["dependencies"][0]["vulns"] = [{
            "id": "PYSEC-2026-1", "fix_versions": ["1.28.0"], "aliases": ["CVE-2026-1234"],
            "description": "Original Python advisory details", "severity": severity,
        }]
        runner.codes[0] = 1
    elif scanner == 1:
        runner.reports[1]["vulnerabilities"] = {"acceptance-package": {
            "name": "acceptance-package", "severity": severity, "isDirect": True,
            "via": [{"source": 1234, "name": "acceptance-package", "severity": severity,
                     "title": "Original Node advisory details", "url": "https://example.com/advisory"}],
            "effects": [], "range": "<2", "nodes": ["node_modules/acceptance-package"],
            "fixAvailable": {"name": "acceptance-package", "version": "2.0.0", "isSemVerMajor": True},
        }}
        runner.reports[1]["metadata"]["vulnerabilities"][severity] = 1
        runner.reports[1]["metadata"]["vulnerabilities"]["total"] = 1
        runner.codes[1] = 1
    else:
        runner.reports[2]["Results"][0]["Vulnerabilities"] = [{
            "VulnerabilityID": "CVE-2026-5678", "PkgName": "openssl", "Severity": severity,
            "InstalledVersion": "1.0.0", "FixedVersion": "2.0.0", "Status": "affected",
            "Title": "Original container advisory details", "Description": "Full description",
            "CVSS": {"nvd": {"V3Score": 9.9}}, "References": ["https://example.com/container"],
        }]


def test_commands_and_scope_and_temporary_report_cleanup(tmp_path, capsys):
    runner = FakeRunner()
    assert invoke(tmp_path, runner) == 0
    python, node, container = runner.calls
    assert python[0] == [
        "uv", "run", "--locked", "--group", "dev", "--with", "pip-audit==2.9.0",
        "--with", "hatchling==1.27.0", "pip-audit", "--format", "json",
        "--progress-spinner", "off", "--desc", "on", "--aliases", "on",
        "--output", str(runner.output),
    ]
    assert node[0] == ["npm", "audit", "--json", "--include=dev"]
    assert node[1]["cwd"] == audit.ROOT / "tests" / "acceptance"
    assert container[0] == [
        "trivy", "image", "--format", "json", "--scanners", "vuln", "--exit-code", "0", IMAGE,
    ]
    assert python[1]["cwd"] == container[1]["cwd"] == audit.ROOT
    for _, kwargs in runner.calls:
        assert kwargs["capture_output"] and kwargs["text"]
        assert kwargs["check"] is False and kwargs["timeout"] == 150
        assert "shell" not in kwargs and "env" not in kwargs
    assert not runner.output.parent.exists()
    assert list(tmp_path.iterdir()) == [tmp_path / "summary.md"]
    assert "uv/pip startup output (not JSON)" in capsys.readouterr().out


@pytest.mark.parametrize("scanner,severities", [
    (0, ("low", "medium", "high", "critical")),
    (1, audit.SEVERITIES),
    (2, ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")),
])
def test_every_severity_is_advisory_and_original_report_is_preserved(
    tmp_path, capsys, scanner, severities,
):
    for severity in severities:
        runner = FakeRunner()
        add_finding(runner, scanner, severity)
        assert invoke(tmp_path, runner) == 0
        stdout = capsys.readouterr().out
        summary = (tmp_path / "summary.md").read_text()
        raw = json.dumps(runner.reports[scanner], indent=2)
        assert raw in stdout and raw in summary
        assert "::notice::" in stdout
        assert "::error::" not in stdout
        assert len(runner.calls) == 3


def test_build_backend_requirement_matches_audited_hatchling_pin(tmp_path):
    build = tomllib.loads((audit.ROOT / "pyproject.toml").read_text())["build-system"]
    runner = FakeRunner()
    assert invoke(tmp_path, runner) == 0
    command = runner.calls[0][0]
    overlay = [command[index + 1] for index, value in enumerate(command) if value == "--with"]
    assert build["build-backend"] == "hatchling.build"
    assert build["requires"] == ["hatchling==1.27.0"]
    assert [requirement for requirement in overlay if requirement.startswith("hatchling")] == build["requires"]


@pytest.mark.parametrize("legacy", [False, True])
def test_python_missing_severity_is_disclosed_without_fabricating_it(tmp_path, capsys, legacy):
    runner = FakeRunner()
    add_finding(runner, 0, "critical")
    del runner.reports[0]["dependencies"][0]["vulns"][0]["severity"]
    if legacy:
        runner.reports[0] = runner.reports[0]["dependencies"]
    original = json.dumps(runner.reports[0], indent=2)
    assert invoke(tmp_path, runner) == 0
    stdout = capsys.readouterr().out
    summary = (tmp_path / "summary.md").read_text()
    for target in (stdout, summary):
        assert original in target
        assert "did not supply severity" in target
        assert "no severity has been assigned" in target
        assert '"severity"' not in target
    assert "::notice::Python" in stdout


def test_simultaneous_findings_and_warnings_do_not_block(tmp_path, capsys):
    runner = FakeRunner()
    for scanner, severity in enumerate(("critical", "critical", "CRITICAL")):
        add_finding(runner, scanner, severity)
        runner.stderr[scanner] = "WARNING: advisory feed cache will expire soon"
    assert invoke(tmp_path, runner) == 0
    assert capsys.readouterr().out.count("::notice::") == 3
    assert "WARNING: advisory feed cache will expire soon" in (tmp_path / "summary.md").read_text()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("finding", [False, True])
@pytest.mark.parametrize("reason", audit.KNOWN_SKIPS)
def test_known_skips_disclose_coverage(tmp_path, capsys, legacy, finding, reason):
    runner = FakeRunner()
    if finding:
        add_finding(runner, 0, "critical")
    skip_reason = reason if reason == "distribution marked as editable" else reason + "git-plugin"
    runner.reports[0]["dependencies"].append({"name": "git-plugin", "skip_reason": skip_reason})
    if legacy:
        runner.reports[0] = runner.reports[0]["dependencies"]
    assert invoke(tmp_path, runner) == 0
    output = capsys.readouterr().out
    summary = (tmp_path / "summary.md").read_text()
    assert "::notice::Python" in output
    for target in (output, summary):
        assert "Coverage limitation" in target and reason in target and "git-plugin" in target
        assert "clean scan" not in target


def test_legacy_python_list_with_findings(tmp_path):
    runner = FakeRunner()
    add_finding(runner, 0, "critical")
    runner.reports[0] = runner.reports[0]["dependencies"]
    assert invoke(tmp_path, runner) == 0


@pytest.mark.parametrize("scanner,report", [
    (0, "not-json"), (0, ""), (0, "null"), (0, "[]"), (0, "{}"),
    (0, '{"dependencies": [], "dependencies": []}'),
    (0, '{"dependencies": [], "unexpected": NaN}'),
    (0, '{"dependencies": [], "unexpected": Infinity}'),
    (0, {"dependencies": [], "error": "feed unavailable"}),
    (0, {"dependencies": [{"name": "p", "version": "1", "vulns": {}}]}),
    (0, {"dependencies": [{"name": "p", "version": "1", "vulns": ["CVE-1"]}]}),
    (0, {"dependencies": [{"name": "p", "version": "1", "vulns": [{"id": "CVE-1"}]}]}),
    (0, {"dependencies": [{"name": "p", "skip_reason": "network timeout"}]}),
    (0, {"dependencies": [{"name": "p", "skip_reason": None}]}),
    (0, {"dependencies": [{"name": "p", "skip_reason": "distribution marked as editable ERROR"}]}),
    (0, {"dependencies": [{"name": "p", "skip_reason": audit.KNOWN_SKIPS[0]}]}),
    (0, {"dependencies": [{"name": "p", "skip_reason": audit.KNOWN_SKIPS[0] + "p", "vulns": [{}]}]}),
    (0, {"dependencies": PYTHON["dependencies"], "fixes": [{"name": "p"}]}),
    (1, "not-json"), (1, ""), (1, "null"), (1, "[]"), (1, "{}"),
    (1, {**NODE, "error": {"code": "EAUDITNOLOCK", "summary": "no lockfile"}}),
    (1, {**NODE, "vulnerabilities": []}), (1, {**NODE, "metadata": {}}),
    (1, {**NODE, "auditReportVersion": 2.0}),
    (1, {**NODE, "metadata": {**NODE["metadata"], "errors": []}}),
    (1, {**NODE, "metadata": {**NODE["metadata"], "dependencies": {"total": True}}}),
    (1, {**NODE, "metadata": {**NODE["metadata"], "dependencies": {"total": 0}}}),
    (1, {**NODE, "vulnerabilities": {"p": {"name": "p", "severity": "critical", "via": []}}}),
    (2, "not-json"), (2, ""), (2, "null"), (2, "[]"), (2, "{}"),
    (2, {**CONTAINER, "SchemaVersion": "2"}),
    (2, {**CONTAINER, "Results": {}}),
    (2, {**CONTAINER, "Error": "database unavailable"}),
    (2, {**CONTAINER, "Results": [{"Target": "linux", "Vulnerabilities": None}]}),
    (2, {**CONTAINER, "Results": [{"Target": "linux", "Vulnerabilities": [{}]}]}),
])
def test_invalid_reports_fail_and_other_scanners_continue(tmp_path, capsys, scanner, report):
    runner = FakeRunner()
    runner.reports[scanner] = report
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 3
    assert not runner.output.parent.exists()
    stdout = capsys.readouterr().out
    assert "::error::" in stdout and "NOT a clean scan" in stdout
    assert "NOT a clean scan" in (tmp_path / "summary.md").read_text()


@pytest.mark.parametrize("field", [*audit.SEVERITIES, "total"])
@pytest.mark.parametrize("bad", [-1, 1, True, "0", None])
def test_npm_metadata_counts_must_match_report(tmp_path, field, bad):
    runner = FakeRunner()
    runner.reports[1]["metadata"]["vulnerabilities"][field] = bad
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 3


@pytest.mark.parametrize("scanner", [0, 1, 2])
@pytest.mark.parametrize("code", [1, 2, -9])
def test_nonzero_status_without_findings_is_not_a_clean_scan(tmp_path, scanner, code):
    runner = FakeRunner()
    runner.codes[scanner] = code
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 3


@pytest.mark.parametrize("scanner", [0, 1, 2])
def test_status_inconsistent_with_findings_fails(tmp_path, scanner):
    runner = FakeRunner()
    add_finding(runner, scanner, "CRITICAL" if scanner == 2 else "critical")
    runner.codes[scanner] = 1 if scanner == 2 else 0
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 3


@pytest.mark.parametrize("scanner", [0, 1, 2])
@pytest.mark.parametrize("exception", [
    FileNotFoundError("private secret must not appear"),
    PermissionError("private secret must not appear"),
    subprocess.TimeoutExpired("private secret must not appear", 150),
])
def test_execution_failures_do_not_stop_other_scanners_or_leak_exceptions(
    tmp_path, capsys, scanner, exception,
):
    runner = FakeRunner()
    runner.errors[scanner] = exception
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 3
    assert "private secret" not in capsys.readouterr().out
    assert "private secret" not in (tmp_path / "summary.md").read_text()
    assert not runner.output.parent.exists()


@pytest.mark.parametrize("timed_out", [(0,), (1,), (2,), (0, 1, 2)])
def test_timeouts_run_remaining_scans_and_publish_summary(tmp_path, monkeypatch, capsys, timed_out):
    summary_path = tmp_path / "actions-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    runner = FakeRunner()
    for scanner in timed_out:
        runner.errors[scanner] = subprocess.TimeoutExpired("secret command", 150)
    assert invoke(tmp_path, runner, summary=False) == 1
    assert [command[0] for command, _ in runner.calls] == ["uv", "npm", "trivy"]
    assert [kwargs["timeout"] for _, kwargs in runner.calls] == [150, 150, 150]
    stdout = capsys.readouterr().out
    summary = summary_path.read_text()
    assert stdout.count("TimeoutExpired after 150 seconds") == len(timed_out) * 2
    assert summary.count("TimeoutExpired after 150 seconds") == len(timed_out)
    for target in (stdout, summary):
        assert "secret command" not in target
        for scanner in set(range(3)) - set(timed_out):
            assert json.dumps(runner.reports[scanner], indent=2) in target
    assert summary.count("NOT a clean scan") == len(timed_out)
    assert not runner.output.parent.exists()
    assert list(tmp_path.iterdir()) == [summary_path]


def test_tls_verification_failure_remains_blocking_without_disabling_tls(tmp_path, capsys):
    runner = FakeRunner()
    runner.codes[0] = 1
    runner.omit_python_report = True
    runner.stderr[0] = "SSLError: CERTIFICATE_VERIFY_FAILED: certificate verify failed"
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 3
    for target in (capsys.readouterr().out, (tmp_path / "summary.md").read_text()):
        assert runner.stderr[0] in target
        assert "NOT a clean scan" in target
    command, kwargs = runner.calls[0]
    assert "--trusted-host" not in command and "--allow-insecure-host" not in command
    assert "env" not in kwargs


def test_missing_python_report_does_not_fall_back_to_stdout(tmp_path):
    runner = FakeRunner()
    runner.omit_python_report = True
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 3


@pytest.mark.parametrize("diagnostic", ["ERROR: feed unavailable", "FATAL database download failed", "npm error EAUDIT"])
def test_error_diagnostics_override_otherwise_valid_report(tmp_path, diagnostic):
    runner = FakeRunner()
    runner.stderr[0] = diagnostic
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 3
    assert diagnostic in (tmp_path / "summary.md").read_text()


def test_collects_all_scanner_failures(tmp_path, capsys):
    runner = FakeRunner()
    runner.reports = ["broken JSON"] * 3
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 3
    assert capsys.readouterr().out.count("::error::") == 3
    assert (tmp_path / "summary.md").read_text().count("NOT a clean scan") == 3


def test_python_temporary_storage_failure_does_not_skip_other_scans(tmp_path, monkeypatch):
    def unavailable(**kwargs):
        raise OSError("private temporary storage details")

    monkeypatch.setattr(audit.tempfile, "TemporaryDirectory", unavailable)
    runner = FakeRunner()
    assert invoke(tmp_path, runner) == 1
    assert [command[0] for command, _ in runner.calls] == ["npm", "trivy"]
    assert "private temporary storage details" not in (tmp_path / "summary.md").read_text()


def test_default_summary_appends_and_explicit_summary_overrides(tmp_path, monkeypatch):
    default = tmp_path / "actions-summary.md"
    default.write_text("Prior step\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(default))
    assert invoke(tmp_path, FakeRunner(), summary=False) == 0
    assert default.read_text().startswith("Prior step\n# Dependency audit")
    previous = default.read_text()
    assert invoke(tmp_path, FakeRunner()) == 0
    assert default.read_text() == previous
    assert (tmp_path / "summary.md").exists()


def test_summary_write_failure_blocks_after_all_scans(tmp_path, capsys):
    runner = FakeRunner()
    assert audit.main(["--summary", str(tmp_path), "--image", IMAGE], runner=runner) == 1
    assert len(runner.calls) == 3
    assert "Could not write audit summary" in capsys.readouterr().out


def test_no_image_or_summary_still_audits_dependencies_and_discloses_omission(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    runner = FakeRunner()
    assert invoke(tmp_path, runner, image=False, summary=False) == 0
    assert len(runner.calls) == 2
    output = capsys.readouterr().out
    assert "::notice::Container audit not requested" in output
    assert not list(tmp_path.iterdir())


def test_empty_trivy_results_are_disclosed_not_called_clean(tmp_path, capsys):
    runner = FakeRunner()
    del runner.reports[2]["Results"]
    assert invoke(tmp_path, runner) == 0
    assert "Coverage limitation — Trivy" in capsys.readouterr().out
    assert "no vulnerability targets" in (tmp_path / "summary.md").read_text()


def test_environment_secrets_are_not_logged_and_secret_scanner_is_not_enabled(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-env-sentinel")
    monkeypatch.setenv("TRIVY_PASSWORD", "secret-registry-sentinel")
    runner = FakeRunner()
    assert invoke(tmp_path, runner) == 0
    output = capsys.readouterr().out + (tmp_path / "summary.md").read_text()
    assert "secret-env-sentinel" not in output and "secret-registry-sentinel" not in output
    assert runner.calls[2][0][runner.calls[2][0].index("--scanners") + 1] == "vuln"


def test_untrusted_output_cannot_inject_workflow_commands_or_break_summary_fences(tmp_path, capsys):
    runner = FakeRunner()
    runner.stderr[1] = "::notice::forged\n```\n# fake heading\n````\n"
    assert invoke(tmp_path, runner) == 0
    output = capsys.readouterr().out
    before, after = output.split(runner.stderr[1], 1)
    token = before.rsplit("::stop-commands::", 1)[1].splitlines()[0]
    assert f"::{token}::" in after
    summary = (tmp_path / "summary.md").read_text()
    assert "`````\n" + runner.stderr[1] in summary


@pytest.mark.parametrize("image", ["", " ", "--help"])
def test_invalid_image_argument_never_runs_scanners(image):
    runner = FakeRunner()
    with pytest.raises(SystemExit) as exc:
        audit.main(["--image=" + image], runner=runner)
    assert exc.value.code == 2
    assert not runner.calls
