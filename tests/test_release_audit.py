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
        self.reports: list[Any] = [deepcopy(report) for report in (PYTHON, NODE, CONTAINER, PYTHON)]
        self.codes = [0, 0, 0, 0]
        self.stderr = ["", "", "", ""]
        self.setup_calls = []

        self.build_requirements = tomllib.loads((audit.ROOT / "pyproject.toml").read_text())["build-system"]["requires"]
        self.errors = {}
        self.calls = []
        self.output: Path
        self.omit_python_report = False

    def __call__(self, command, **kwargs):
        if command[0] == "uv" and command[1] in ("venv", "pip"):
            self.setup_calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "build environment setup", "")
        if command[0].endswith("/bin/python"):
            self.setup_calls.append((command, kwargs))
            version = self.build_requirements[0].split("==")[1]
            inventory = {"paths": [str(Path(command[0]).parent.parent / "site-packages")],
                         "packages": {"hatchling": version}}
            self.reports[3]["dependencies"][0]["version"] = version
            return subprocess.CompletedProcess(command, 0, json.dumps(inventory), "")
        index = {"uv": 0, "npm": 1, "trivy": 2}[command[0]]
        if index == 0 and "/frozen/application/site-packages" not in command:
            index = 3
        self.calls.append((command, kwargs))
        if index in (0, 3):
            output = Path(command[command.index("--output") + 1])
            if index == 0:
                self.output = output
        if index in self.errors:
            raise self.errors[index]
        report = self.reports[index]
        raw = report if isinstance(report, str) else json.dumps(report, indent=2)
        if index in (0, 3):
            output = Path(command[command.index("--output") + 1])
            assert output.parent.exists()
            if not self.omit_python_report or index == 3:
                output.write_text(raw, encoding="utf-8")
            stdout = "uv/pip startup output (not JSON)\n"
        else:
            stdout = raw
        return subprocess.CompletedProcess(command, self.codes[index], stdout, self.stderr[index])


@pytest.fixture(autouse=True)
def frozen_inventory(monkeypatch):
    monkeypatch.setattr(audit, "installed_inventory", lambda: {
        "paths": ["/frozen/application/site-packages"], "packages": {"hatchling": "1.27.0"},
    })
    original_read = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == audit.ROOT / "tests" / "acceptance" / "package-lock.json":
            return json.dumps({"packages": {"node_modules/acceptance-package": {"version": "1.0.0"}}})
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


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
    elif scanner == 3:
        runner.reports[3]["dependencies"][0]["vulns"] = [{
            "id": "PYSEC-BUILD-1", "fix_versions": ["9.0.0"], "severity": severity,
        }]
        runner.codes[3] = 1
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
    python, build, node, container = runner.calls
    assert python[0] == [
        "uv", "run", "--no-project", "--isolated", "--with", "pip-audit==2.9.0",
        "pip-audit", "--vulnerability-service", "pypi", "--skip-editable", "--format", "json",
        "--progress-spinner", "off", "--desc", "on", "--aliases", "on",
        "--path", "/frozen/application/site-packages", "--output", str(runner.output),
    ]
    assert "--path" in build[0]
    assert "/frozen/application/site-packages" not in build[0]
    assert all("hatchling==1.27.0" not in command for command, _ in runner.calls)
    assert not Path(build[0][-1]).parent.exists()
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
        assert raw in stdout and raw not in summary
        assert "Original severity counts:" in summary
        assert severity in summary
        assert "Original Python advisory details" not in summary
        assert "Original Node advisory details" not in summary
        assert "Full description" not in summary
        assert "::notice::" in stdout
        assert "::error::" not in stdout
        assert len(runner.calls) == 4


def test_build_backend_requirement_matches_audited_hatchling_pin(tmp_path):
    build = tomllib.loads((audit.ROOT / "pyproject.toml").read_text())["build-system"]
    runner = FakeRunner()
    assert invoke(tmp_path, runner) == 0
    command = runner.setup_calls[1][0]
    assert command[:3] == ["uv", "pip", "install"]
    assert command[5:] == build["requires"]
    assert "--with" not in command
    assert all("hatchling==" not in argument for argument in runner.calls[0][0])


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
    assert original in stdout and original not in summary
    for target in (stdout, summary):
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
    assert "Scanner diagnostics were emitted" in (tmp_path / "summary.md").read_text()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("finding", [False, True])
@pytest.mark.parametrize("reason", audit.KNOWN_SKIPS)
def test_known_skips_disclose_coverage(tmp_path, capsys, monkeypatch, legacy, finding, reason):
    runner = FakeRunner()
    if finding:
        add_finding(runner, 0, "critical")
    skip_reason = reason if reason == "distribution marked as editable" else reason + "git-plugin"
    runner.reports[0]["dependencies"].append({"name": "git-plugin", "skip_reason": skip_reason})
    monkeypatch.setattr(audit, "installed_inventory", lambda: {
        "paths": ["/frozen/application/site-packages"],
        "packages": {"hatchling": "1.27.0", "git-plugin": "0.1"},
    })
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
    assert len(runner.calls) == 4
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
    assert len(runner.calls) == 4


@pytest.mark.parametrize("scanner", [0, 1, 2])
@pytest.mark.parametrize("code", [1, 2, -9])
def test_nonzero_status_without_findings_is_not_a_clean_scan(tmp_path, scanner, code):
    runner = FakeRunner()
    runner.codes[scanner] = code
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 4


@pytest.mark.parametrize("scanner", [0, 1, 2])
def test_status_inconsistent_with_findings_fails(tmp_path, scanner):
    runner = FakeRunner()
    add_finding(runner, scanner, "CRITICAL" if scanner == 2 else "critical")
    runner.codes[scanner] = 1 if scanner == 2 else 0
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 4


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
    assert len(runner.calls) == 4
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
    assert [command[0] for command, _ in runner.calls] == ["uv", "uv", "npm", "trivy"]
    assert [kwargs["timeout"] for _, kwargs in runner.calls] == [150] * 4
    stdout = capsys.readouterr().out
    summary = summary_path.read_text()
    assert stdout.count("TimeoutExpired after 150 seconds") == len(timed_out) * 2
    assert summary.count("TimeoutExpired after 150 seconds") == len(timed_out)
    for target in (stdout, summary):
        assert "secret command" not in target
    for scanner in set(range(3)) - set(timed_out):
        assert json.dumps(runner.reports[scanner], indent=2) in stdout
    assert summary.count("NOT a clean scan") == len(timed_out)
    assert not runner.output.parent.exists()
    assert list(tmp_path.iterdir()) == [summary_path]


def test_tls_verification_failure_remains_blocking_without_disabling_tls(tmp_path, capsys):
    runner = FakeRunner()
    runner.codes[0] = 1
    runner.omit_python_report = True
    runner.stderr[0] = "SSLError: CERTIFICATE_VERIFY_FAILED: certificate verify failed"
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 4
    assert runner.stderr[0] in capsys.readouterr().out
    assert "NOT a clean scan" in (tmp_path / "summary.md").read_text()
    command, kwargs = runner.calls[0]
    assert "--trusted-host" not in command and "--allow-insecure-host" not in command
    assert "env" not in kwargs


def test_missing_python_report_does_not_fall_back_to_stdout(tmp_path):
    runner = FakeRunner()
    runner.omit_python_report = True
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 4


@pytest.mark.parametrize("diagnostic", ["ERROR: feed unavailable", "FATAL database download failed", "npm error EAUDIT"])
def test_error_diagnostics_override_otherwise_valid_report(tmp_path, capsys, diagnostic):
    runner = FakeRunner()
    runner.stderr[0] = diagnostic
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 4
    assert diagnostic in capsys.readouterr().out
    assert "scanner diagnostics report an audit error" in (tmp_path / "summary.md").read_text()


def test_collects_all_scanner_failures(tmp_path, capsys):
    runner = FakeRunner()
    runner.reports[:3] = ["broken JSON"] * 3
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 4
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
    assert len(runner.calls) == 4
    assert "Could not write audit summary" in capsys.readouterr().out


def test_no_image_or_summary_still_audits_dependencies_and_discloses_omission(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    runner = FakeRunner()
    assert invoke(tmp_path, runner, image=False, summary=False) == 0
    assert len(runner.calls) == 3
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
    assert runner.calls[3][0][runner.calls[3][0].index("--scanners") + 1] == "vuln"


def test_untrusted_output_cannot_inject_workflow_commands_or_break_summary_fences(tmp_path, capsys):
    runner = FakeRunner()
    runner.stderr[1] = "::notice::forged\n```\n# fake heading\n````\n"
    assert invoke(tmp_path, runner) == 0
    output = capsys.readouterr().out
    before, after = output.split(runner.stderr[1], 1)
    token = before.rsplit("::stop-commands::", 1)[1].splitlines()[0]
    assert f"::{token}::" in after
    summary = (tmp_path / "summary.md").read_text()
    assert runner.stderr[1] not in summary
    assert "# fake heading" not in summary
    assert "Scanner diagnostics were emitted" in summary


def test_multi_megabyte_raw_report_stays_in_logs_not_summary(tmp_path, capsys, monkeypatch):
    runner = FakeRunner()
    add_finding(runner, 2, "CRITICAL")
    finding = runner.reports[2]["Results"][0]["Vulnerabilities"][0]
    finding["Description"] = "完整 original details " * 160_000
    runner.reports[2]["Results"][0]["Vulnerabilities"] *= 3
    raw = json.dumps(runner.reports[2], indent=2)
    assert len(raw.encode("utf-8")) > 2_800_000
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    assert invoke(tmp_path, runner) == 0
    assert raw in capsys.readouterr().out
    summary = (tmp_path / "summary.md").read_text()
    assert len(summary.encode("utf-8")) < 10_000
    assert "Container (Trivy): 3 vulnerability finding(s)" in summary
    assert "CRITICAL=3" in summary
    assert "1 unique finding rows" in summary
    assert summary.count("| CVE-2026-5678 | openssl | 1.0.0 | 2.0.0 | CRITICAL |") == 1
    assert "https://github.com/owner/repository/actions/runs/123" in summary
    assert "完整 original details" not in summary
    assert list(tmp_path.iterdir()) == [tmp_path / "summary.md"]


@pytest.mark.parametrize("existing", [b"", "先前\n".encode()])
def test_summary_budget_is_exact_utf8_bytes_including_existing_content(tmp_path, monkeypatch, existing):
    path = tmp_path / "summary.md"
    payload = "é帳\n"
    path.write_bytes(existing)
    budget = len(existing) + len(payload.encode("utf-8"))
    monkeypatch.setattr(audit, "SUMMARY_BYTE_BUDGET", budget)
    audit.write_summary(path, payload)
    assert path.read_bytes() == existing + payload.encode("utf-8")
    path.write_bytes(existing)
    monkeypatch.setattr(audit, "SUMMARY_BYTE_BUDGET", budget - 1)
    with pytest.raises(ValueError, match="UTF-8 byte budget"):
        audit.write_summary(path, payload)
    assert path.read_bytes() == existing


@pytest.mark.parametrize("existing", [b"x" * 1_048_577, "账".encode() * 350_000])
def test_existing_oversized_actions_summary_blocks_without_modifying_it(tmp_path, monkeypatch, capsys, existing):
    path = tmp_path / "summary.md"
    path.write_bytes(existing)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    runner = FakeRunner()
    assert invoke(tmp_path, runner, summary=False) == 1
    assert len(runner.calls) == 4
    assert path.read_bytes() == existing
    output = capsys.readouterr().out
    assert "::error::Could not write audit summary" in output
    assert "no findings truncated" in output
    for report in runner.reports:
        assert json.dumps(report, indent=2) in output


def test_compact_finding_list_over_budget_fails_without_truncating(tmp_path, monkeypatch, capsys):
    runner = FakeRunner()
    add_finding(runner, 2, "HIGH")
    original = runner.reports[2]["Results"][0]["Vulnerabilities"][0]
    findings = [{**original, "VulnerabilityID": f"CVE-2026-{index:05}"} for index in range(200)]
    runner.reports[2]["Results"][0]["Vulnerabilities"] = findings
    monkeypatch.setattr(audit, "SUMMARY_BYTE_BUDGET", 10_000)
    assert invoke(tmp_path, runner) == 1
    output = capsys.readouterr().out
    assert json.dumps(runner.reports[2], indent=2) in output
    assert all(finding["VulnerabilityID"] in output for finding in findings)
    assert "audit reporting failed" in output
    assert (tmp_path / "summary.md").read_bytes() == b""


@pytest.mark.parametrize("invalid", [b"\xff", b"previous\xc3"])
def test_invalid_existing_utf8_summary_is_reporting_failure(tmp_path, capsys, invalid):
    path = tmp_path / "summary.md"
    path.write_bytes(invalid)
    assert invoke(tmp_path, FakeRunner()) == 1
    assert path.read_bytes() == invalid
    assert "audit reporting failed" in capsys.readouterr().out


def test_summary_short_write_fails_closed(tmp_path, monkeypatch, capsys):
    original_open = Path.open

    class ShortWriter:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, payload):
            return self.file.write(payload[:-1])

    def open_file(path, *args, **kwargs):
        file = original_open(path, *args, **kwargs)
        return ShortWriter(file) if path == tmp_path / "summary.md" else file

    monkeypatch.setattr(Path, "open", open_file)
    runner = FakeRunner()
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 4
    assert "short audit summary write" in capsys.readouterr().out


def test_actions_without_summary_path_is_reporting_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert invoke(tmp_path, FakeRunner(), summary=False) == 1
    assert "Missing GITHUB_STEP_SUMMARY" in capsys.readouterr().out


def test_compact_fields_and_urls_cannot_inject_markdown_or_commands(tmp_path, capsys):
    runner = FakeRunner()
    add_finding(runner, 2, "HIGH")
    finding = runner.reports[2]["Results"][0]["Vulnerabilities"][0]
    finding["PkgName"] = "pkg|injected\n::error::forged\n# fake\n<script>bad</script>````"
    finding["PrimaryURL"] = "https://example.com/)\n![injected](https://evil.test/)"
    assert invoke(tmp_path, runner) == 0
    summary = (tmp_path / "summary.md").read_text()
    assert "<script>" not in summary and "\n::error::forged" not in summary
    assert "pkg|injected" not in summary and "![injected]" not in summary
    assert "%29%0A%21%5Binjected%5D%28" in summary
    output = capsys.readouterr().out
    before, after = output.split(json.dumps(runner.reports[2], indent=2), 1)
    token = before.rsplit("::stop-commands::", 1)[1].splitlines()[0]
    assert f"::{token}::" in after
    assert not any(line.startswith("::error::") for line in output.splitlines())


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,test", "https://user:secret@example.com/a"])
def test_unsafe_advisory_links_are_not_rendered(tmp_path, url):
    runner = FakeRunner()
    add_finding(runner, 2, "LOW")
    runner.reports[2]["Results"][0]["Vulnerabilities"][0]["PrimaryURL"] = url
    assert invoke(tmp_path, runner) == 0
    summary = (tmp_path / "summary.md").read_text()
    assert url not in summary
    assert "unsafe link (see logs)" in summary


@pytest.mark.parametrize("change", ["version", "missing", "extra", "duplicate"])
def test_python_report_must_match_frozen_installed_inventory(tmp_path, capsys, change):
    runner = FakeRunner()
    dependencies = runner.reports[0]["dependencies"]
    if change == "version":
        dependencies[0]["version"] = "99.0.0"
    elif change == "missing":
        dependencies[0]["name"] = "scanner-only"
    elif change == "extra":
        dependencies.append({"name": "scanner-only", "version": "1", "vulns": []})
    else:
        dependencies.append(deepcopy(dependencies[0]))
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 4
    assert "::error::Python runtime/dev" in capsys.readouterr().out


def test_separate_runtime_and_build_versions_are_both_audited(tmp_path, monkeypatch):
    runner = FakeRunner()
    runner.reports[0]["dependencies"][0]["version"] = "1.0.0"
    monkeypatch.setattr(audit, "installed_inventory", lambda: {
        "paths": ["/frozen/application/site-packages"], "packages": {"hatchling": "1.0.0"},
    })
    add_finding(runner, 0, "critical")
    add_finding(runner, 3, "CRITICAL")
    assert invoke(tmp_path, runner) == 0
    summary = (tmp_path / "summary.md").read_text()
    assert "| hatchling | 1.0.0 |" in summary
    assert "| hatchling | " + runner.build_requirements[0].split("==")[1] + " |" in summary
    runtime, build = runner.calls[:2]
    for command, _ in (runtime, build):
        assert command[:4] == ["uv", "run", "--no-project", "--isolated"]
        assert command.count("--with") == 1
        assert "--ignore-vuln" not in command and "--fix" not in command


def test_installed_inventory_reads_target_paths_not_scanner_environment(monkeypatch):
    class Distribution:
        def __init__(self, name, version):
            self.metadata = {"Name": name}
            self.version = version

    monkeypatch.setattr(audit.sysconfig, "get_path", lambda _: "/application/site-packages")

    def distributions(*, path):
        assert path == ["/application/site-packages"]
        return [Distribution("Runtime.Package", "1.2.3"), Distribution("dev-package", "4.5.6")]

    monkeypatch.setattr(audit.importlib.metadata, "distributions", distributions)
    # Recover the real function replaced by the autouse fixture.
    import runpy
    inventory = runpy.run_path(str(Path(audit.__file__)))["installed_inventory"]()
    assert inventory == {"paths": ["/application/site-packages"],
                         "packages": {"runtime-package": "1.2.3", "dev-package": "4.5.6"}}


@pytest.mark.parametrize("stage", ["venv", "install", "inventory"])
def test_build_setup_failures_block_but_other_scans_continue(tmp_path, capsys, stage):
    runner = FakeRunner()

    def failing(command, **kwargs):
        if ((stage == "venv" and command[:2] == ["uv", "venv"])
                or (stage == "install" and command[:2] == ["uv", "pip"])
                or (stage == "inventory" and command[0].endswith("/bin/python"))):
            return subprocess.CompletedProcess(command, 2, "", "setup unavailable")
        return runner(command, **kwargs)

    assert invoke(tmp_path, failing) == 1
    assert [command[0] for command, _ in runner.calls] == ["uv", "npm", "trivy"]
    assert "::error::Python build" in capsys.readouterr().out


def test_fava_and_git_plugin_coverage_is_explicit(tmp_path, capsys):
    assert invoke(tmp_path, FakeRunner()) == 0
    for output in (capsys.readouterr().out, (tmp_path / "summary.md").read_text()):
        assert "not fork or Git source code" in output
        for package in ("fava", "beancount-lazy-plugins", "beancount-share",
                        "beancount-interpolate", "beancount-reds-plugins"):
            assert package in output


def test_npm_transitive_advisories_preserve_package_and_advisory_severities(tmp_path, monkeypatch):
    runner = FakeRunner()
    add_finding(runner, 1, "high")
    runner.reports[1]["vulnerabilities"]["wrapper"] = {
        "name": "wrapper", "severity": "critical", "via": ["acceptance-package"],
        "nodes": ["node_modules/wrapper"], "fixAvailable": True,
    }
    counts = runner.reports[1]["metadata"]["vulnerabilities"]
    counts["critical"] = 1
    counts["total"] = 2
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == audit.ROOT / "tests" / "acceptance" / "package-lock.json":
            return json.dumps({"packages": {
                "node_modules/acceptance-package": {"version": "1.0.0"},
                "node_modules/wrapper": {"version": "3.0.0"},
            }})
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert invoke(tmp_path, runner) == 0
    summary = (tmp_path / "summary.md").read_text()
    assert "| 1234 | wrapper | 3.0.0 | available; version not supplied | package: critical; advisory: high |" in summary
    assert "critical=1, high=1" in summary
    assert "2 unique finding rows" in summary


@pytest.mark.parametrize("mutation", ["unresolved", "cycle", "missing-version", "missing-fix", "missing-identity"])
def test_incomplete_compact_npm_details_fail_reporting(tmp_path, mutation):
    runner = FakeRunner()
    add_finding(runner, 1, "high")
    vuln = runner.reports[1]["vulnerabilities"]["acceptance-package"]
    if mutation == "unresolved":
        vuln["via"] = ["not-reported"]
    elif mutation == "cycle":
        vuln["via"] = ["acceptance-package"]
    elif mutation == "missing-version":
        vuln["nodes"] = ["node_modules/not-locked"]
    elif mutation == "missing-fix":
        del vuln["fixAvailable"]
    else:
        del vuln["via"][0]["source"]
        del vuln["via"][0]["url"]
    assert invoke(tmp_path, runner) == 1
    assert len(runner.calls) == 4
    summary = (tmp_path / "summary.md").read_text()
    assert "Node acceptance" in summary and "NOT a clean scan" in summary


def test_python_duplicate_rows_keep_original_counts(tmp_path):
    runner = FakeRunner()
    add_finding(runner, 0, "critical")
    runner.reports[0]["dependencies"][0]["vulns"] *= 2
    assert invoke(tmp_path, runner) == 0
    summary = (tmp_path / "summary.md").read_text()
    assert "Python runtime/dev (pip-audit): 2 vulnerability finding(s)" in summary
    assert "critical=2" in summary
    assert summary.count("| PYSEC-2026-1, CVE-2026-1234 | hatchling |") == 1


def test_timeout_preserves_partial_scanner_logs(tmp_path, capsys):
    runner = FakeRunner()
    runner.errors[0] = subprocess.TimeoutExpired("secret command", 150,
                                                output=b"partial report\n::error::forged",
                                                stderr=b"partial diagnostic")
    assert invoke(tmp_path, runner) == 1
    output = capsys.readouterr().out
    assert "partial report\n::error::forged" in output
    assert "partial diagnostic" in output
    assert "partial report" not in (tmp_path / "summary.md").read_text()
    assert "secret command" not in output


def test_build_scan_findings_are_advisory_and_have_original_versions(tmp_path):
    runner = FakeRunner()
    add_finding(runner, 3, "critical")
    assert invoke(tmp_path, runner) == 0
    summary = (tmp_path / "summary.md").read_text()
    version = runner.build_requirements[0].split("==")[1]
    assert f"| PYSEC-BUILD-1 | hatchling | {version} | 9.0.0 | critical |" in summary


@pytest.mark.parametrize("image", ["", " ", "--help"])
def test_invalid_image_argument_never_runs_scanners(image):
    runner = FakeRunner()
    with pytest.raises(SystemExit) as exc:
        audit.main(["--image=" + image], runner=runner)
    assert exc.value.code == 2
    assert not runner.calls
