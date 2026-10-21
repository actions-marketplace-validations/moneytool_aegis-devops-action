# Aegis-DevOps Plan Check

A GitHub Action that checks a Terraform or OpenTofu plan against your policy before it's applied, and fails the job if the policy blocks it.

It runs [Aegis-DevOps](https://github.com/moneytool/aegis-devops). A policy rule only counts if nobody has edited it since it was signed, and if its author was allowed to write that kind of rule. So a rule someone slips into the repo, or into a ticket an AI agent reads, gets no vote.

```yaml
- uses: moneytool/aegis-devops-action@v1
  with:
    plan: plan.json
```

The result goes to the job summary and, on pull requests, to a comment that's updated on each push:

> ## ⛔ Aegis-DevOps plan check: BLOCK
> Plan `plan.json`: 2 resource change(s).
> Decided by: `plan-no-db-deletes`
> - max_matching: 1 delete/replace > 0

## Setup

1. Add a policy to the repository. `aegis init` writes a signed example policy you can edit:

   ```bash
   pip install aegis-devops
   aegis init .aegis
   ```

   The example rules include "block any plan that deletes or replaces an `aws_db_instance`" and "escalate any plan that touches more than 25 resources". See [writing constraints](https://github.com/moneytool/aegis-devops/blob/main/docs/constraints.md).

   The example files are signed with a published example key, so anyone could re-sign an edited file. Before you rely on the policy, sign it with your own key, keep the key out of the repository, and give it to the action from a secret:

   ```bash
   aegis keygen --out aegis.key          # store its contents as the secret AEGIS_SIGNING_KEY
   rm .aegis/example-signing.key
   aegis sign --key file:aegis.key .aegis/*.yaml .aegis/sources
   ```

2. Add the step after `terraform plan`:

```yaml
name: terraform
on: pull_request

permissions:
  contents: read
  pull-requests: write   # for the PR comment; drop it and set comment: false if you don't want one

jobs:
  plan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_wrapper: false
      - run: terraform init -input=false && terraform plan -input=false -out=tfplan
      - uses: moneytool/aegis-devops-action@v1
        with:
          plan: tfplan          # binary plans are converted with `terraform show -json`
          signing-key: ${{ secrets.AEGIS_SIGNING_KEY }}
```

### OpenTofu

```yaml
      - uses: opentofu/setup-opentofu@v1
        with:
          tofu_wrapper: false
      - run: tofu init -input=false && tofu plan -input=false -out=tfplan
      - uses: moneytool/aegis-devops-action@v1
        with:
          plan: tfplan
          tool: tofu
```

### A plan in a subdirectory

```yaml
      - uses: moneytool/aegis-devops-action@v1
        with:
          working-directory: infra/prod
          plan: tfplan
          config-dir: ../../.aegis
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `plan` | (required unless `budget` is set) | Path to the plan. JSON plans are read as is; binary plans are converted with `<tool> show -json`, which needs the tool on PATH and an initialised directory. |
| `tool` | `terraform` | `terraform` or `tofu`. |
| `working-directory` | `.` | Where to run. Relative `plan` and `config-dir` paths resolve from here. |
| `config-dir` | Aegis's search order | Policy directory. Empty means `$AEGIS_CONFIG_DIR`, then `./.aegis`, then `~/.config/aegis`. |
| `fail-on` | `block` | `block` fails on BLOCK; `escalate` fails on ESCALATE or BLOCK; `never` only reports. |
| `comment` | `true` | Post or update a pull request comment. Needs `pull-requests: write`. Without it you get a warning, not a failure. |
| `signing-key` | (empty) | Key the policy files are signed with, from a secret. Empty falls back to `$AEGIS_SIGNING_KEY`, then the example key from `aegis init`. |
| `github-token` | `github.token` | Token for the comment. |
| `escalate-scripts` | `false` | With `budget: setup`: install the hooks with `--escalate-scripts`: commands that run code aegis cannot read (scripts, `make`/`npm run`, inline or piped code) are escalated (blocked in Codex, Gemini CLI and OpenCode; an "ask" in Claude Code and Copilot CLI). Needs aegis-devops 1.2.0+. |
| `aegis-version` | `1.1.0` | aegis-devops version installed from PyPI. |
| `budget` | `off` | `setup` / `report`: the session budget cap for coding agents in this job ([below](#budget-cap-for-coding-agents-in-a-workflow)). `off` checks a plan. |
| `agents` | `claude` | With `budget: setup`: comma-separated agents to install the budget hooks for (`claude`, `codex`, `gemini`, `opencode`, `copilot`). |
| `python-version` | `3.12` | Python used to run Aegis (3.11+). |

## Outputs

| Output | Description |
|---|---|
| `verdict` | `ALLOW`, `ESCALATE` or `BLOCK`, or `ERROR` if Aegis couldn't decide. |
| `citations` | Comma-separated IDs of the rules behind the verdict. |
| `report` | Path to the full JSON-lines report. |
| `budget-decision` | With `budget`: `allow`, `warn` or `deny`, or `error` if the budget couldn't be read. |
| `budget-status` | With `budget: report`: path to the JSON from `aegis budget status`. |

## Failure behaviour

This is a policy gate, so it fails closed. If there's no policy, the plan can't be read, a binary plan can't be converted, or Aegis exits with an error, the step fails with the reason. It never reports ALLOW when it couldn't decide.

## Budget cap for coding agents in a workflow

If a job runs a coding agent (Claude Code, Codex, Gemini CLI, OpenCode, Copilot CLI), the same
action can cap what that agent spends, using the signed `budget.yaml` in your policy directory
([budget docs](https://moneytool.github.io/aegis-devops/budget.html)). Put one step before the
agent and one after it:

```yaml
      - uses: moneytool/aegis-devops-action@v1
        with:
          budget: setup              # checks budget.yaml, installs the hooks for these agents
          agents: claude
          signing-key: ${{ secrets.AEGIS_SIGNING_KEY }}

      - name: Run the agent
        env:
          AEGIS_SIGNING_KEY: ${{ secrets.AEGIS_SIGNING_KEY }}   # the hooks verify budget.yaml
        run: claude -p "Fix the failing test"

      - uses: moneytool/aegis-devops-action@v1
        if: always()
        with:
          budget: report             # totals the sessions, summary + PR comment
          signing-key: ${{ secrets.AEGIS_SIGNING_KEY }}
```

- **`setup`** fails the step if the policy directory has no `budget.yaml`, or it can't be used
  (signature, authority, shape). It installs aegis's hooks for `agents` in the runner's home
  directory, so while the agent runs, its tool calls (and new prompts, where the agent can block
  them) are denied once the session or the day reaches a limit.
- **`report`** rebuilds today's totals from the agents' logs, writes the job summary and a
  pull request comment, and fails per `fail-on`: `block` (default) when a limit was reached,
  `escalate` also when one is near, `never` to only report.
- **Keys.** The hooks check `budget.yaml`'s signature while the agent runs, so the agent step
  needs `AEGIS_SIGNING_KEY` in its `env`. Anything in that environment is readable by the agent:
  this is a guard against runaway loops and mistakes, not against an agent that sets out to
  raise its own limit.
- **Agents in CI.** Codex runs new hooks only after they are trusted: use
  `codex exec --dangerously-bypass-hook-trust`. Gemini CLI asks to trust the folder: set
  `GEMINI_CLI_TRUST_WORKSPACE: true` on the agent step. Copilot CLI and Codex report their
  usage only after a turn's tool calls, so in a one-prompt run the cap applies from the next turn;
  `report` still shows the total.

## Also for AI coding agents

The same policy can stop Claude Code, Codex, Copilot, Cursor, Gemini CLI and OpenCode before they run `terraform destroy`, `kubectl delete` or `DROP TABLE`. See the [Aegis-DevOps README](https://github.com/moneytool/aegis-devops#readme).

## License

Apache-2.0
