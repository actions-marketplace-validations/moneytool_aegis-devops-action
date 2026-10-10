"""The session budget cap for coding agents run in a workflow (`budget: setup|report`).

`setup` runs before the step that runs an agent (Claude Code, Codex, Gemini CLI, OpenCode,
Copilot CLI): it checks that the policy directory holds a usable `budget.yaml` and installs
aegis's hooks for the listed agents in the runner's home directory, so the agent's tool calls
and prompts are checked against the budget while it runs. `report` runs after it (usually with
`if: always()`): it rebuilds the session totals from the agents' logs, writes the job summary
(and optionally a pull request comment), and fails the step when a limit was reached.

Standard library only; shares its helpers with aegis_action.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from aegis_action import annotate, env, github, pr_number, set_output, write_summary

AGENTS = ("claude", "codex", "gemini", "opencode", "copilot")
MARKER = "<!-- aegis-devops-budget -->"
ICON = {"allow": "✅", "warn": "⚠️", "deny": "⛔", "error": "❌"}
EXIT_HINTS = {
    65: "budget.yaml cannot be used: check its signature (`signing-key`), its principal's "
        "`budget` authority, and its shape",
    66: "no budget: add a signed budget.yaml to the policy directory "
        "(`aegis init` writes budget.example.yaml)",
}
AGENT_NOTES = {
    "codex": "Codex runs new hooks only after they are trusted; in a workflow, run "
             "`codex exec --dangerously-bypass-hook-trust`.",
    "gemini": "Gemini CLI asks to trust the folder; in a workflow set "
              "`GEMINI_CLI_TRUST_WORKSPACE: true` on the agent step.",
}


def aegis() -> str:
    return env("AEGIS_BIN")


def fail(message: str) -> None:
    annotate("error", message)
    set_output("budget-decision", "error")
    write_summary(f"## {ICON['error']} Aegis-DevOps budget: error\n\n{message}\n")
    sys.exit(1)


def key_args() -> list[str]:
    key = env("INPUT_SIGNING_KEY")
    return ["--key", key] if key else []


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([aegis(), *args], capture_output=True, text=True, check=False)


def status(project: str) -> dict:
    """`aegis budget status --json`, or a failed step saying why not."""
    proc = run("budget", "status", "--project", project, "--json", *key_args())
    if proc.returncode != 0:
        hint = EXIT_HINTS.get(proc.returncode, "unexpected error")
        last = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
        fail(f"aegis budget exited with code {proc.returncode}: {hint}."
             + (f"\n\n```\n{last}\n```" if last else ""))
    if not proc.stdout.lstrip().startswith("{"):
        fail(proc.stdout.strip() or "aegis budget status printed nothing; is the working "
             "directory inside a project with a .aegis/ policy directory?")
    return json.loads(proc.stdout)


def fmt(unit: str, amount: float | None) -> str:
    if amount is None:
        return "—"
    if unit == "usd":
        return f"${amount:,.2f}"
    if unit == "tokens":
        return f"{amount:,.0f} tokens"
    return f"{amount:,.2f} premium requests"


def report_markdown(st: dict) -> str:
    decision = st["decision"]
    over = [s for s in st["sessions"] if s["level"] == "deny"]
    if over and decision != "deny":
        decision = "deny"
    lines = [MARKER, f"## {ICON[decision]} Aegis-DevOps budget: {decision.upper()}", ""]
    unit = st["unit"]
    pd = st.get("project_day")
    if pd:
        lines.append(f"Project today ({st['day']}, {st['tz']}): **{fmt(unit, pd['used'])}** of "
                     f"{fmt(unit, pd['limit'])} ({pd['fraction']:.0%})"
                     + (f", overshoot {fmt(unit, pd['overshoot'])}" if pd["overshoot"] else ""))
    if st["completeness"] != "complete":
        lines.append(f"History: {st['completeness']}"
                     + (" (acknowledged by a signed reset)" if st["acknowledged"] else ""))
    for m in st.get("messages", []):
        lines.append(f"- {m.removeprefix('budget: ')}")
    if st["sessions"]:
        lines += ["", "| Agent | Session | Today | Session total | Limit | |",
                  "|---|---|---|---|---|---|"]
        for s in st["sessions"]:
            flag = {"deny": "⛔ over", "warn": "⚠️ near"}.get(s["level"], "")
            if s["unverified"]:
                flag += f" lower bound ({', '.join(s['unverified'])})"
            lines.append(f"| {s['agent']} | `{s['session_id']}` | {fmt(s['unit'], s['today'])} "
                         f"| {fmt(s['unit'], s['total'])} | {fmt(s['unit'], s['limit'])} "
                         f"| {flag.strip()} |")
    else:
        lines += ["", "No agent sessions in this project today."]
    lines += ["", "<sub>The budget denies tool calls and prompts once the usage visible at the "
              "hook reaches a limit; it is not a hard ceiling. See [the budget docs]"
              "(https://moneytool.github.io/aegis-devops/budget.html).</sub>"]
    return "\n".join(lines), decision


def post_comment(markdown: str) -> None:
    number, token = pr_number(), env("GITHUB_TOKEN")
    repo, api = env("GITHUB_REPOSITORY"), env("GITHUB_API_URL", "https://api.github.com")
    if not number or not token or not repo:
        return
    base = f"{api}/repos/{repo}/issues"
    try:
        existing = None
        for page in range(1, 6):
            comments = github("GET", f"{base}/{number}/comments?per_page=100&page={page}", token)
            existing = next((c for c in comments if MARKER in (c.get("body") or "")), None)
            if existing or len(comments) < 100:
                break
        if existing:
            github("PATCH", f"{base}/comments/{existing['id']}", token, {"body": markdown})
        else:
            github("POST", f"{base}/{number}/comments", token, {"body": markdown})
    except Exception as exc:  # noqa: BLE001 - a comment is best effort
        annotate("warning", f"could not post the pull request comment ({exc}). "
                 "Grant `pull-requests: write`, or set `comment: false`.")


def setup(project: str) -> None:
    agents = [a.strip() for a in env("INPUT_AGENTS", "claude").split(",") if a.strip()]
    unknown = [a for a in agents if a not in AGENTS]
    if not agents or unknown:
        fail(f"`agents` must list some of {', '.join(AGENTS)}"
             + (f"; unknown: {', '.join(unknown)}" if unknown else ""))
    check = run("budget", "check", "--project", project, *key_args())
    if check.returncode not in (0, 3):
        hint = EXIT_HINTS.get(check.returncode, "unexpected error")
        last = check.stderr.strip().splitlines()[-1] if check.stderr.strip() else ""
        fail(f"aegis budget exited with code {check.returncode}: {hint}."
             + (f"\n\n```\n{last}\n```" if last else ""))
    if check.returncode == 3:
        annotate("warning", "the budget is already reached for today; the agent's tool calls "
                 "will be denied")
    installed = []
    for agent in agents:
        proc = run("install", agent, "--user", "--budget")
        if proc.returncode != 0:
            fail(f"could not install the {agent} hook: {proc.stderr.strip() or proc.stdout}")
        installed.append(agent)
        print(proc.stdout.strip())
    notes = [AGENT_NOTES[a] for a in installed if a in AGENT_NOTES]
    if env("INPUT_SIGNING_KEY"):
        notes.append("The hooks verify budget.yaml while the agent runs: give the agent step "
                     "`AEGIS_SIGNING_KEY` in its `env` (anything in that environment is "
                     "readable by the agent).")
    set_output("budget-decision", "deny" if check.returncode == 3 else "allow")
    write_summary("\n".join([
        f"## Aegis-DevOps budget: hooks installed for {', '.join(installed)}", "",
        *(f"- {n}" for n in notes)]))


def report(project: str) -> None:
    fail_on = env("INPUT_FAIL_ON", "block").lower()
    if fail_on not in ("block", "escalate", "never"):
        fail(f"`fail-on` must be `block`, `escalate` or `never`, not `{fail_on}`")
    st = status(project)
    path = Path(env("RUNNER_TEMP", "/tmp")) / "aegis-budget.json"
    path.write_text(json.dumps(st, indent=2), encoding="utf-8")
    markdown, decision = report_markdown(st)
    set_output("budget-decision", decision)
    set_output("budget-status", str(path))
    write_summary(markdown)
    if env("INPUT_COMMENT", "true").lower() == "true":
        post_comment(markdown)
    print(f"Aegis-DevOps budget: {decision}")
    failing = {"block": {"deny"}, "escalate": {"deny", "warn"}, "never": set()}[fail_on]
    if decision in failing:
        reasons = [m.removeprefix("budget: ") for m in st.get("messages", [])]
        reasons += [f"{s['agent']} session {s['session_id']} reached its limit: "
                    f"{fmt(s['unit'], s['total'])} of {fmt(s['unit'], s['limit'])}"
                    for s in st["sessions"] if s["level"] == "deny"]
        annotate("error", f"budget {decision}: " + "; ".join(reasons))
        sys.exit(1)


def main() -> None:
    if env("INPUT_SIGNING_KEY"):
        print(f"::add-mask::{env('INPUT_SIGNING_KEY')}")
    mode = env("INPUT_BUDGET").lower()
    project = str(Path(env("INPUT_WORKING_DIRECTORY", ".")).resolve())
    config_dir = env("INPUT_CONFIG_DIR")
    if config_dir:
        # aegis budget, and the hooks in later steps, find the policy through this
        os.environ["AEGIS_CONFIG_DIR"] = str((Path(project) / config_dir).resolve())
        if mode == "setup" and env("GITHUB_ENV"):
            with open(env("GITHUB_ENV"), "a", encoding="utf-8") as fh:
                fh.write(f"AEGIS_CONFIG_DIR={os.environ['AEGIS_CONFIG_DIR']}\n")
    if mode == "setup":
        setup(project)
    elif mode == "report":
        report(project)
    else:
        fail(f"`budget` must be `off`, `setup` or `report`, not `{mode}`")


if __name__ == "__main__":
    main()
