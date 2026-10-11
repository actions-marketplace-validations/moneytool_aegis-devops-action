"""Runs `aegis check terraform|tofu` for the GitHub Action and reports the result.

Standard library only. Reads its inputs from INPUT_* environment variables (set by
action.yml), writes step outputs, a job summary, and optionally a pull request comment.
Any failure to reach a verdict fails the step: this is a policy gate, so it fails closed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

VERDICTS = {0: "ALLOW", 2: "ESCALATE", 3: "BLOCK"}
MARKER = "<!-- aegis-devops-plan-check -->"
ICON = {"ALLOW": "✅", "ESCALATE": "⚠️", "BLOCK": "⛔", "ERROR": "❌"}
EXIT_HINTS = {
    64: "bad command line",
    65: "the plan or the policy files could not be read or verified",
    66: "no policy found: run `aegis init .aegis` in the repository, or set `config-dir`",
}
REPO_URL = "https://github.com/moneytool/aegis-devops"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def set_output(name: str, value: str) -> None:
    path = env("GITHUB_OUTPUT")
    if not path:
        print(f"{name}={value}")
        return
    with open(path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            fh.write(f"{name}<<__AEGIS_EOF__\n{value}\n__AEGIS_EOF__\n")
        else:
            fh.write(f"{name}={value}\n")


def annotate(level: str, message: str) -> None:
    """A workflow annotation; newlines must be escaped or the rest is dropped."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title=Aegis-DevOps::{escaped}")


def fail(message: str) -> None:
    annotate("error", message)
    set_output("verdict", "ERROR")
    write_summary(f"## {ICON['ERROR']} Aegis-DevOps plan check: ERROR\n\n{message}\n")
    sys.exit(1)


def write_summary(markdown: str) -> None:
    path = env("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def json_plan(plan: str, tool: str) -> Path:
    """The plan as a JSON file; binary plans are converted with `<tool> show -json`."""
    path = Path(plan)
    if not path.is_file():
        fail(f"plan file not found: `{plan}` (working directory: `{os.getcwd()}`)")
    head = path.read_bytes()[:1].strip()
    if head in (b"{", b"["):
        return path
    out = Path(env("RUNNER_TEMP", "/tmp")) / "aegis-plan.json"
    try:
        with open(out, "wb") as fh:
            subprocess.run([tool, "show", "-json", str(path)], stdout=fh, check=True)
    except FileNotFoundError:
        fail(f"`{plan}` is a binary plan and `{tool}` is not on PATH to convert it. "
             f"Run `{tool} show -json` first, or set up {tool} in an earlier step.")
    except subprocess.CalledProcessError as exc:
        fail(f"`{tool} show -json {plan}` failed with exit code {exc.returncode}")
    return out


def run_check(plan_json: Path, tool: str, config_dir: str) -> tuple[int, list[dict], str]:
    cmd = [env("AEGIS_BIN", "aegis"), "check", tool, "--json"]
    if config_dir:
        cmd += ["--config-dir", config_dir]
    cmd.append(str(plan_json))
    child_env = dict(os.environ)
    key = child_env.pop("INPUT_SIGNING_KEY", "").strip()
    if key:
        child_env["AEGIS_SIGNING_KEY"] = key
    child_env.pop("GITHUB_TOKEN", None)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=child_env)
    records = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return proc.returncode, records, proc.stderr.strip()


def build_report(verdict: str, records: list[dict], plan_label: str) -> tuple[str, list[str]]:
    plan = next((r["plan"] for r in records if "plan" in r), {})
    citations = list(dict.fromkeys(plan.get("citations", [])))
    rows = []
    for r in records:
        if "intent" not in r:
            continue
        intent, decision = r["intent"], r["decision"]
        cites = ", ".join(f"`{c}`" for c in decision.get("citations", [])) or ""
        rows.append(
            f"| {ICON.get(decision['verdict'], '')} {decision['verdict']} "
            f"| `{intent['action']}` | `{intent['resource']}` | {cites} |"
        )
    lines = [
        MARKER,
        f"## {ICON.get(verdict, '')} Aegis-DevOps plan check: {verdict}",
        "",
        f"Plan `{plan_label}`: {plan.get('n_intents', len(rows))} resource change(s).",
    ]
    if citations:
        lines.append("Decided by: " + ", ".join(f"`{c}`" for c in citations))
    for note in plan.get("notes", []):
        lines.append(f"- {note}")
    discarded = plan.get("discarded", [])
    if discarded:
        lines.append(f"- {len(discarded)} matching rule(s) discarded as untrustworthy "
                     "(tampered, forged or unauthorized): they got no vote.")
    health = plan.get("store_health", {})
    if health:
        lines.append(
            f"- Policy store: {health.get('loaded', '?')} rules loaded, "
            f"{len(health.get('quarantined', []))} quarantined."
        )
        for warning in health.get("warnings", []):
            lines.append(f"- Warning: {warning}")
    if rows:
        lines += [
            "",
            "<details><summary>Per-resource decisions (plan-level rules, listed above, can block a plan whose individual changes are each allowed)</summary>",
            "",
            "| Verdict | Action | Resource | Rules |",
            "|---|---|---|---|",
            *rows[:200],
        ]
        if len(rows) > 200:
            lines.append(f"| … | | {len(rows) - 200} more | |")
        lines += ["", "</details>"]
    lines += ["", f"<sub>Checked by [Aegis-DevOps]({REPO_URL}).</sub>"]
    return "\n".join(lines), citations


def pr_number() -> int | None:
    event_path = env("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).is_file():
        return None
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    return pr.get("number")


def github(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aegis-devops-action",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode()
        return json.loads(text) if text else None


def post_comment(markdown: str, marker: str = MARKER) -> None:
    """Posts or updates this run's pull request comment, found again by its ``marker``."""
    number, token = pr_number(), env("GITHUB_TOKEN")
    repo, api = env("GITHUB_REPOSITORY"), env("GITHUB_API_URL", "https://api.github.com")
    if not number or not token or not repo:
        return
    base = f"{api}/repos/{repo}/issues"
    try:
        existing = None
        for page in range(1, 6):
            comments = github("GET", f"{base}/{number}/comments?per_page=100&page={page}", token)
            existing = next((c for c in comments if marker in (c.get("body") or "")), None)
            if existing or len(comments) < 100:
                break
        if existing:
            github("PATCH", f"{base}/comments/{existing['id']}", token, {"body": markdown})
        else:
            github("POST", f"{base}/{number}/comments", token, {"body": markdown})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        annotate("warning", f"could not post the pull request comment ({exc}). "
                 "Grant `pull-requests: write`, or set `comment: false`.")


def main() -> None:
    if env("INPUT_SIGNING_KEY"):
        print(f"::add-mask::{env('INPUT_SIGNING_KEY')}")
    tool = env("INPUT_TOOL", "terraform").lower()
    if tool not in ("terraform", "tofu"):
        fail(f"`tool` must be `terraform` or `tofu`, not `{tool}`")
    fail_on = env("INPUT_FAIL_ON", "block").lower()
    if fail_on not in ("block", "escalate", "never"):
        fail(f"`fail-on` must be `block`, `escalate` or `never`, not `{fail_on}`")
    plan = env("INPUT_PLAN")
    if not plan:
        fail("the `plan` input is required")

    plan_json = json_plan(plan, tool)
    code, records, stderr = run_check(plan_json, tool, env("INPUT_CONFIG_DIR"))
    if stderr:
        print(stderr, file=sys.stderr)

    report_path = Path(env("RUNNER_TEMP", "/tmp")) / "aegis-report.jsonl"
    report_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    set_output("report", str(report_path))

    verdict = VERDICTS.get(code)
    if verdict is None or not any("plan" in r for r in records):
        hint = EXIT_HINTS.get(code, "unexpected error")
        last = stderr.splitlines()[-1] if stderr else ""
        fail(f"aegis exited with code {code}: {hint}." + (f"\n\n```\n{last}\n```" if last else ""))

    markdown, citations = build_report(verdict, records, plan)
    set_output("verdict", verdict)
    set_output("citations", ",".join(citations))
    write_summary(markdown)
    if env("INPUT_COMMENT", "true").lower() == "true":
        post_comment(markdown)

    print(f"Aegis-DevOps: {verdict}" + (f" ({', '.join(citations)})" if citations else ""))
    failing = {"block": {"BLOCK"}, "escalate": {"BLOCK", "ESCALATE"}, "never": set()}[fail_on]
    if verdict in failing:
        annotate("error", f"plan verdict {verdict}"
                 + (f" by {', '.join(citations)}" if citations else ""))
        sys.exit(1)


if __name__ == "__main__":
    main()
