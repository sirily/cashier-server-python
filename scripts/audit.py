"""Release dependency audits: findings are advisory, failed/incomplete scans are not."""

import argparse
import json
import os
import re
import subprocess
import tempfile
import uuid
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEVERITIES = ("info", "low", "moderate", "high", "critical")
KNOWN_SKIPS = (
    "Dependency not found on PyPI and could not be audited: ",
    "Package has invalid version and could not be audited: ",
    "distribution marked as editable",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def record(value):
    require(isinstance(value, dict), "expected a JSON object")
    require(not {"error", "errors", "Error", "Errors"}.intersection(value),
            "scanner reported an audit error")
    return value


def text(value):
    return isinstance(value, str) and bool(value.strip())


def unique_fields(pairs):
    require(len(dict(pairs)) == len(pairs), "duplicate JSON fields")
    return dict(pairs)


def python_report(report):
    dependencies = report if isinstance(report, list) else record(report).get("dependencies")
    require(isinstance(dependencies, list) and dependencies, "missing/empty Python dependencies")
    if isinstance(report, dict):
        require(report.get("fixes", []) == [], "unexpected Python fixes in a read-only audit")
    count, notices, missing_severity = 0, [], False
    for dependency in dependencies:
        dep = record(dependency)
        require(text(dep.get("name")), "missing Python package name")
        if "skip_reason" in dep:
            reason = dep["skip_reason"]
            require(text(reason) and (reason == KNOWN_SKIPS[2] or any(
                reason.startswith(prefix) and text(reason[len(prefix):]) for prefix in KNOWN_SKIPS[:2]
            )), "unrecognized Python audit skip")
            require(dep.get("vulns", []) == [], "skipped package also reports vulnerabilities")
            notices.append(f"Coverage limitation — {dep['name']}: {reason}")
            continue
        require(text(dep.get("version")), "missing Python package version")
        vulns = dep.get("vulns")
        require(isinstance(vulns, list), "missing Python vulnerability list")
        for vuln in vulns:
            vuln = record(vuln)
            require(text(vuln.get("id")), "missing Python vulnerability ID")
            require(isinstance(vuln.get("fix_versions"), list)
                    and all(text(v) for v in vuln["fix_versions"]), "invalid Python fix versions")
            for field in ("description", "severity"):
                require(field not in vuln or text(vuln[field]), f"invalid Python {field}")
            if "severity" not in vuln:
                missing_severity = True
            require("aliases" not in vuln or isinstance(vuln["aliases"], list)
                    and all(text(v) for v in vuln["aliases"]), "invalid Python aliases")
        count += len(vulns)
    if missing_severity:
        notices.append("pip-audit did not supply severity for some findings; no severity has been assigned.")
    return count, notices


def node_report(report):
    report = record(report)
    require(type(report.get("auditReportVersion")) is int and report["auditReportVersion"] == 2,
            "unsupported npm audit report version")
    vulns = report.get("vulnerabilities")
    require(isinstance(vulns, dict), "missing npm vulnerabilities")
    counts = Counter()
    for name, vuln in vulns.items():
        vuln = record(vuln)
        require(text(name) and vuln.get("name") == name, "invalid npm package name")
        severity = vuln.get("severity")
        require(severity in SEVERITIES, "invalid npm severity")
        via = vuln.get("via")
        require(isinstance(via, list) and via, "missing npm advisory details")
        for advisory in via:
            require(text(advisory) or isinstance(advisory, dict), "invalid npm advisory")
            if isinstance(advisory, dict):
                require(text(record(advisory).get("title"))
                        and advisory.get("severity") in SEVERITIES, "invalid npm advisory details")
        counts[severity] += 1
    metadata = record(report.get("metadata"))
    totals = record(metadata.get("vulnerabilities"))
    for severity in (*SEVERITIES, "total"):
        expected = len(vulns) if severity == "total" else counts[severity]
        require(type(totals.get(severity)) is int and totals[severity] == expected,
                f"inconsistent npm {severity} vulnerability count")
    dependencies = record(metadata.get("dependencies"))
    require(all(type(n) is int and n >= 0 for n in dependencies.values())
            and dependencies.get("total", 0) >= max(1, len(vulns)), "missing/invalid npm dependency counts")
    return len(vulns), []


def container_report(report):
    report = record(report)
    require(type(report.get("SchemaVersion")) is int and report["SchemaVersion"] == 2,
            "unsupported Trivy report schema")
    require(text(report.get("ArtifactName")) and report.get("ArtifactType") == "container_image",
            "missing Trivy container identity")
    record(report.get("Metadata"))
    # Trivy omits Results/Vulnerabilities when empty (including scratch images).
    results = report.get("Results", [])
    require(isinstance(results, list), "invalid Trivy results")
    count = 0
    for result in results:
        result = record(result)
        require(text(result.get("Target")), "missing Trivy scan target")
        vulns = result.get("Vulnerabilities", [])
        require(isinstance(vulns, list), "invalid Trivy vulnerability list")
        for vuln in vulns:
            vuln = record(vuln)
            require(text(vuln.get("VulnerabilityID")) and text(vuln.get("PkgName"))
                    and vuln.get("Severity") in ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"),
                    "invalid Trivy vulnerability details")
        count += len(vulns)
    notices = [] if results else ["Coverage limitation — Trivy reported no vulnerability targets."]
    return count, notices


def fenced(label, value):
    fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", value)), default=0))
    return f"### {label}\n\n{fence}\n{value}\n{fence}\n"


def annotation(level, message):
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{escaped}")


def main(argv=None, *, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="production container image to scan with Trivy")
    parser.add_argument("--summary", type=Path, default=os.environ.get("GITHUB_STEP_SUMMARY") or None,
                        help="append Markdown here (default: GITHUB_STEP_SUMMARY, if set)")
    args = parser.parse_args(argv)
    if args.image is not None and (not args.image.strip() or args.image.startswith("-")):
        parser.error("--image must be a nonempty image reference, not an option")
    sections = ["# Dependency audit\n\nAll severities are advisory; audit failures are blocking.\n"]
    failed = False

    def emit(message):
        sections.append(message)
        # Reports/diagnostics are untrusted; do not execute embedded Actions commands.
        token = uuid.uuid4().hex
        print(f"::stop-commands::{token}\n{message}\n::{token}::")

    with ExitStack() as cleanup:
        scans = [
            ("Python runtime/dev/build (pip-audit)", [
                "uv", "run", "--locked", "--group", "dev", "--with", "pip-audit==2.9.0",
                "--with", "hatchling==1.27.0", "pip-audit", "--format", "json",
                "--progress-spinner", "off", "--desc", "on", "--aliases", "on",
            ], ROOT, python_report),
            ("Node acceptance (npm audit)", ["npm", "audit", "--json", "--include=dev"],
             ROOT / "tests" / "acceptance", node_report),
        ]
        if args.image:
            scans.append(("Container (Trivy)", [
                "trivy", "image", "--format", "json", "--scanners", "vuln", "--exit-code", "0",
                args.image,
            ], ROOT, container_report))
        for name, command, cwd, validate in scans:
            emit(f"## {name}\n")
            output = None
            try:
                if validate is python_report:
                    directory = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="cashier-audit-"))
                    output = Path(directory) / "python.json"
                    command += ["--output", str(output)]
                result = runner(command, cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False, timeout=150)
                for label, value in (("Scanner stdout", result.stdout), ("Scanner stderr", result.stderr)):
                    if value:
                        emit(fenced(label, value))
                raw = output.read_text(encoding="utf-8") if output is not None else result.stdout
                if validate is python_report:
                    emit(fenced("Original JSON report", raw))
                report = json.loads(raw, object_pairs_hook=unique_fields,
                                    parse_constant=lambda _: require(False, "nonstandard JSON constant"))
                count, notices = validate(report)
                expected = 0 if validate is container_report else int(count > 0)
                require(result.returncode == expected,
                        f"scanner exit {result.returncode} inconsistent with report (expected {expected})")
                require(not re.search(r"(?m)\b(?:ERROR|FATAL)\b|^npm (?:ERR!|error)\b", result.stderr),
                        "scanner diagnostics report an audit error")
                message = f"{name}: {count} vulnerability finding(s); all severities are advisory."
                emit(message + "\n")
                if count:
                    annotation("notice", message)
                for notice in notices:
                    emit(f"> Notice: {notice}\n")
                    annotation("notice", f"{name}: {notice}")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                failed = True
                # Exception strings can contain credentials/commands; diagnostics above are sufficient.
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                if isinstance(exc, subprocess.TimeoutExpired):
                    reason += f" after {exc.timeout} seconds"
                message = f"{name}: audit failed ({reason}); NOT a clean scan."
                emit(message + "\n")
                annotation("error", message)
        if not args.image:
            message = "Container audit not requested: supply --image for production-image coverage."
            emit(f"> Notice: {message}\n")
            annotation("notice", message)
    if args.summary:
        try:
            with args.summary.open("a", encoding="utf-8") as summary:
                summary.write("\n".join(sections) + "\n")
        except OSError:
            annotation("error", "Could not write audit summary; audit reporting failed.")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
