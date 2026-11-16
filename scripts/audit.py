"""Release dependency audits: findings are advisory, failed/incomplete scans are not."""

import argparse
import html
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import uuid
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
# Leave headroom below Actions' 1 MiB per-step limit, including any prior content.
SUMMARY_BYTE_BUDGET = 900_000
PIP_AUDIT = "pip-audit==2.9.0"
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
                    and text(vuln.get("InstalledVersion"))
                    and ("FixedVersion" not in vuln or isinstance(vuln["FixedVersion"], str))
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


def log(message):
    # Reports/diagnostics are untrusted; never execute embedded Actions commands.
    token = uuid.uuid4().hex
    print(f"::stop-commands::{token}\n{message}\n::{token}::", flush=True)


def package_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_inventory():
    paths = sorted({sysconfig.get_path(key) for key in ("purelib", "platlib")})
    packages = {}
    for distribution in importlib.metadata.distributions(path=paths):
        name = package_name(distribution.metadata["Name"])
        require(name not in packages, "duplicate installed Python distribution")
        packages[name] = distribution.version
    require(packages, "empty installed Python inventory")
    return {"paths": paths, "packages": packages}


def check_inventory(report, inventory):
    dependencies = report if isinstance(report, list) else report["dependencies"]
    actual = {}
    for dep in dependencies:
        name = package_name(dep["name"])
        require(name not in actual, "duplicate Python dependency in report")
        actual[name] = dep
    require(actual.keys() == inventory["packages"].keys(), "Python report does not cover installed inventory")
    for name, version in inventory["packages"].items():
        dep = actual[name]
        require(("skip_reason" in dep and "version" not in dep) or dep.get("version") == version,
                "Python report version differs from installed inventory")


def cell(value):
    value = str(value)
    value = "".join(f"\\u{ord(char):04x}" if ord(char) < 32 or ord(char) == 127 else char
                    for char in value)
    value = html.escape(value, quote=True)
    return re.sub(r"[\\`|\[\]()*_!#~]", lambda match: f"&#{ord(match[0])};", value)


def link(url, label="advisory"):
    if not text(url):
        return "not supplied (see logs)"
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        return "unsafe link (see logs)"
    return f"[{label}]({quote(url, safe='/:?=&%#')})"


def compact_report(report, validate):
    rows = set()
    severities = Counter()
    if validate is python_report:
        dependencies = report if isinstance(report, list) else report["dependencies"]
        skipped = sum("skip_reason" in dep for dep in dependencies)
        coverage = f"{len(dependencies)} installed packages; {skipped} skipped (listed below)."
        for dep in dependencies:
            for vuln in dep.get("vulns", []):
                severity = vuln.get("severity", "not supplied")
                severities[severity] += 1
                ids = ", ".join([vuln["id"], *vuln.get("aliases", [])])
                rows.add((ids, dep["name"], dep["version"], ", ".join(vuln["fix_versions"]) or "not supplied",
                          severity, link("https://osv.dev/vulnerability/" + quote(vuln["id"], safe=""))))
    elif validate is node_report:
        counts = report["metadata"]["dependencies"]
        coverage = f"{counts['total']} dependencies (npm metadata); installed versions from package-lock.json."
        lock = json.loads((ROOT / "tests" / "acceptance" / "package-lock.json").read_text(encoding="utf-8"))
        packages = record(lock.get("packages"))
        def advisories(name, seen=frozenset()):
            require(name in report["vulnerabilities"], "unresolved npm transitive advisory")
            if name in seen:
                return []
            found = []
            for item in report["vulnerabilities"][name]["via"]:
                found.extend(advisories(item, seen | {name}) if isinstance(item, str) else [item])
            return found

        for name, vuln in report["vulnerabilities"].items():
            severities[vuln["severity"]] += 1
            nodes = vuln.get("nodes")
            require(isinstance(nodes, list) and nodes and all(text(node) for node in nodes),
                    "missing npm installed package locations")
            versions = set()
            for node in nodes:
                package = record(packages.get(node))
                require(text(package.get("version")), "missing npm locked installed version")
                versions.add(package["version"])
            fix = vuln.get("fixAvailable")
            if isinstance(fix, dict):
                require(text(fix.get("name")) and text(fix.get("version")), "invalid npm fixed version")
                fixed = f"{fix['name']}@{fix['version']}"
            else:
                require(type(fix) is bool, "invalid npm fix availability")
                fixed = "available; version not supplied" if fix else "not supplied"
            resolved = advisories(name)
            require(resolved, "npm package has no resolvable advisory details")
            for advisory in resolved:
                require(type(advisory.get("source")) is int or text(advisory.get("url")),
                        "missing npm advisory identity")
                identity = str(advisory.get("source", advisory.get("url")))
                severity = f"package: {vuln['severity']}; advisory: {advisory['severity']}"
                rows.add((identity, name, ", ".join(sorted(versions)), fixed,
                          severity, link(advisory.get("url"))))
        coverage += " Severity totals count vulnerable packages, not individual via advisories."
    else:
        results = report.get("Results", [])
        targets = sorted({result["Target"] for result in results})
        coverage = f"{len(results)} vulnerability targets: " + ", ".join(targets)
        for result in results:
            for vuln in result.get("Vulnerabilities", []):
                severities[vuln["Severity"]] += 1
                rows.add((vuln["VulnerabilityID"], vuln["PkgName"], vuln["InstalledVersion"],
                          vuln.get("FixedVersion") or "not supplied", vuln["Severity"],
                          link(vuln.get("PrimaryURL") or "https://osv.dev/vulnerability/"
                               + quote(vuln["VulnerabilityID"], safe=""))))
    lines = [f"Coverage: {cell(coverage)}\n",
             "Original severity counts: " + (", ".join(f"{cell(key)}={value}" for key, value in sorted(severities.items())) or "none") + ".\n",
             f"{len(rows)} unique finding rows; exact duplicate rows collapsed, original counts unchanged.\n"]
    if rows:
        lines += ["| Advisory / aliases | Package | Installed version | Fixed version | Original severity | Details |",
                  "| --- | --- | --- | --- | --- | --- |"]
        lines += ["| " + " | ".join([*(cell(value) for value in row[:5]), row[5]]) + " |"
                  for row in sorted(rows)]
    return "\n".join(lines) + "\n"


def write_summary(path, content):
    payload = content.encode("utf-8", errors="strict")
    with path.open("a+b") as summary:
        summary.seek(0, os.SEEK_END)
        existing = summary.tell()
        require(existing + len(payload) <= SUMMARY_BYTE_BUDGET,
                f"summary exceeds {SUMMARY_BYTE_BUDGET} UTF-8 byte budget "
                f"({existing} existing + {len(payload)} new); no findings truncated; see full CI logs")
        summary.seek(0)
        summary.read().decode("utf-8", errors="strict")
        require(summary.write(payload) == len(payload), "short audit summary write")
        summary.flush()
        require(os.fstat(summary.fileno()).st_size == existing + len(payload),
                "audit summary size changed during reporting")


def main(argv=None, *, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="production container image to scan with Trivy")
    parser.add_argument("--summary", type=Path, default=os.environ.get("GITHUB_STEP_SUMMARY") or None,
                        help="append bounded Markdown here (default: GITHUB_STEP_SUMMARY, if set)")
    args = parser.parse_args(argv)
    if args.image is not None and (not args.image.strip() or args.image.startswith("-")):
        parser.error("--image must be a nonempty image reference, not an option")
    sections = ["# Dependency audit\n\nAll severities are advisory; audit failures are blocking.\n",
                ("Compact findings below retain original counts/severities and coverage. Full original JSON "
                "reports and diagnostics are in this job's **Audit Python, Node, and production image dependencies** "
                "CI log; descriptions and duplicate rows are not copied here. No downloadable report artifacts.\n")]
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if repository and run_id:
        sections.append(link(server.rstrip("/") + "/" + quote(repository, safe="/")
                             + "/actions/runs/" + quote(run_id, safe=""), "Full CI logs") + "\n")
    failed = False

    def emit(message):
        sections.append(message)
        log(message)

    def execute(command, cwd):
        try:
            result = runner(command, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="strict", check=False, timeout=150)
        except subprocess.TimeoutExpired as exc:
            for label, value in (("Partial scanner stdout", exc.stdout), ("Partial scanner stderr", exc.stderr)):
                if value:
                    log(fenced(label, value.decode("utf-8", errors="backslashreplace")
                               if isinstance(value, bytes) else value))
            raise
        for label, value in (("Scanner stdout", result.stdout), ("Scanner stderr", result.stderr)):
            if value:
                log(fenced(label, value))
        return result

    def prepare_build(directory):
        build = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]
        requirements = build["requires"]
        require(isinstance(requirements, list) and requirements and all(text(item) for item in requirements),
                "missing build backend requirements")
        environment = Path(directory) / "build"
        python = environment / "bin" / "python"
        commands = [["uv", "venv", "--no-project", "--python", sys.executable, str(environment)],
                    ["uv", "pip", "install", "--python", str(python), *requirements]]
        for command in commands:
            require(execute(command, ROOT).returncode == 0, "build audit environment setup failed")
        # Import only this standalone stdlib script, never the application or its plugins.
        code = ("import runpy, json; module = runpy.run_path(" + repr(str(Path(__file__).resolve()))
                + "); print(json.dumps(module['installed_inventory']()))")
        result = execute([str(python), "-c", code], ROOT)
        require(result.returncode == 0, "build audit inventory failed")
        return json.loads(result.stdout)

    with ExitStack() as cleanup:
        scans = [
            ("Python runtime/dev (pip-audit)", None, ROOT, python_report),
            ("Python build (pip-audit)", None, ROOT, python_report),
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
                inventory = None
                if validate is python_report:
                    directory = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="cashier-audit-"))
                    if name.startswith("Python build"):
                        inventory = prepare_build(directory)
                    else:
                        inventory = installed_inventory()
                        sources = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]["sources"]
                        emit("Coverage limitation — PyPI advisories match names/versions, not fork or Git source code. "
                             "Custom source dependencies: " + cell(", ".join(sorted(sources)))
                             + ". Any scanner skips are listed individually below; they are not clean results.\n")
                    output = Path(directory) / "python.json"
                    command = ["uv", "run", "--no-project", "--isolated", "--with", PIP_AUDIT,
                               "pip-audit", "--vulnerability-service", "pypi", "--skip-editable",
                               "--format", "json", "--progress-spinner", "off", "--desc", "on", "--aliases", "on"]
                    for path in inventory["paths"]:
                        command += ["--path", path]
                    command += ["--output", str(output)]
                result = execute(command, cwd)
                raw = output.read_text(encoding="utf-8") if output is not None else result.stdout
                if validate is python_report:
                    log(fenced("Original JSON report", raw))
                report = json.loads(raw, object_pairs_hook=unique_fields,
                                    parse_constant=lambda _: require(False, "nonstandard JSON constant"))
                count, notices = validate(report)
                if inventory is not None:
                    check_inventory(report, inventory)
                expected = 0 if validate is container_report else int(count > 0)
                require(result.returncode == expected,
                        f"scanner exit {result.returncode} inconsistent with report (expected {expected})")
                require(not re.search(r"(?m)\b(?:ERROR|FATAL)\b|^npm (?:ERR!|error)\b", result.stderr),
                        "scanner diagnostics report an audit error")
                compact = compact_report(report, validate)
                message = f"{name}: {count} vulnerability finding(s); all severities are advisory."
                emit(message + "\n")
                emit(compact)
                if result.stderr:
                    emit("Scanner diagnostics were emitted; see full CI logs (not copied into this summary).\n")
                if count:
                    annotation("notice", message)
                for notice in notices:
                    emit(f"> Notice: {cell(notice)}\n")
                    annotation("notice", f"{name}: {notice}")
            except (OSError, ValueError, KeyError, TypeError, RecursionError, subprocess.SubprocessError) as exc:
                if isinstance(exc, subprocess.TimeoutExpired) and output is not None and output.exists():
                    try:
                        log(fenced("Partial original JSON report", output.read_text(encoding="utf-8")))
                    except (OSError, UnicodeError):
                        log("Partial report could not be read; scan remains failed.")
                failed = True
                # Exception strings can contain credentials/commands; diagnostics above are sufficient.
                reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, (UnicodeError, json.JSONDecodeError)) else type(exc).__name__
                if isinstance(exc, subprocess.TimeoutExpired):
                    reason += f" after {exc.timeout} seconds"
                message = f"{name}: audit failed ({reason}); NOT a clean scan."
                emit(cell(message) + "\n")
                annotation("error", message)
        if not args.image:
            message = "Container audit not requested: supply --image for production-image coverage."
            emit(f"> Notice: {message}\n")
            annotation("notice", message)
    if not args.summary and os.environ.get("GITHUB_ACTIONS") == "true":
        annotation("error", "Missing GITHUB_STEP_SUMMARY; audit reporting failed.")
        failed = True
    if args.summary:
        try:
            write_summary(args.summary, "\n".join(sections) + "\n")
        except (OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else type(exc).__name__
            annotation("error", f"Could not write audit summary; audit reporting failed ({reason}).")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
