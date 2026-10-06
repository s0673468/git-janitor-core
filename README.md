# git-janitor

A conservative Git/GitHub hygiene scanner and explicit operator tools. Scan
results identify dirty worktrees, unpublished changes, stale branches, and pull
requests needing attention. Scanning does not apply those recommendations.

This source distribution excludes the original private Git history, personal
repository inventory, deployment services, reports, credentials, and private
agent instructions. It adds no license grant; dependency licenses still apply.

## Install and scan

Python 3.11+ and Git are required. Live GitHub operations also need `gh`
authenticated by the operator. Runtime code uses the standard library.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp config.example.toml /absolute/private/path/janitor.toml
.venv/bin/git-janitor --config /absolute/private/path/janitor.toml --no-fetch
```

Add explicit repository paths to the private configuration. The example has
empty scope, remote fetching disabled, every mutation flag false, and an empty
apply allowlist. A normal scan never merges PRs or deletes branches. Enabling
fetching refreshes/prunes remote-tracking refs; `--no-fetch` leaves them alone.
Generated reports can contain repository paths and PR metadata, so store them
privately rather than in a public checkout.

## Explicit operations and proofs

The opt-in execution engine requires an action's configuration flag, its
configured category allowlist, explicit `--apply`, and fresh precondition checks.
`--dry-run` records proposed actions without mutating. A CLI category filter can
only narrow the configuration allowlist. Every attempt has an append-only audit
record. [Execution details](docs/execution-engine.md) describe the supported
operations and excluded high-risk categories.

`git-safe-delete`, `preserve-branch`, and `pr-shepherd` are separate explicit
operator tools. They preserve their branch/PR identity checks, merge evidence,
exact-head tests, dirty-worktree guards, and append-only receipts. Read `--help`
before an operator command. Branch preservation defaults to
`~/.local/share/git-janitor/preservation`.

Code-review generation is disabled. Tests and required checks govern supported
PR delivery; historical review parsers remain for compatibility. The package
does not grant authorization to change a repository merely because a proof gate
passes. Deployment, credentials, publication, and other high-risk changes remain
outside automatic scanner execution.

## Account and runtime configuration

Account-wide `repo-facts` and `pr-shepherd` sweeps require `--owner OWNER`, or an
explicit `GIT_JANITOR_OWNER` environment value. There is no personal account
default. The facts cache defaults to `~/.local/state/git-janitor/repo-facts.json`;
project registry input defaults to `~/.config/git-janitor/projects.json` and is
supplied separately by the operator.

Runner observations have optional explicit policy configuration:

- `GIT_JANITOR_ORG_RUNNER_OWNER`: owner whose organization runner pool is queried.
- `GIT_JANITOR_HOSTED_OWNER`: owner independently verified to use hosted runners;
  hosted capacity is reported as unknown instead of inferred from runner registration.
- `GIT_JANITOR_RUNNER_NAME_PREFIX`: prefix for an expected private repository runner;
  defaults to `runner-`.

These settings observe existing runner policy. This package never registers a
runner or attaches local execution to public repositories. Public CI
uses standard GitHub-hosted Linux only.

Optional alerts require an operator-provided executable selected with
`--alert-command`; its neutral default location is
`~/.config/git-janitor/alert-command`. No alert service, recipient, or scheduled
job is installed. Touched-repository planning uses local state under
`~/.local/state/git-janitor/maintainer`; it does not install an automation.

## Validate

```sh
make check PYTHON=.venv/bin/python
make lint-workflows ACTIONLINT=/absolute/path/to/actionlint
```

The suite uses synthetic GitHub responses, generated temporary Git repositories,
and invented fixture owners/paths. It checks read-only scanning, dual mutation
gates, fresh identity/proof checks, receipt integrity, and refusal on drift.
Private deployment and external watchdog consumer tests are excluded with their
absent components; producer receipt tests remain. No GitHub credentials are
needed for the offline suite.

`make mutation` is a separate, deliberately manual expensive validation target.
The public workflow runs the ordinary gate with a read-only token, no secrets,
no self-hosted runner, and no artifact uploads or persistent caches.

The export passed 371 offline tests with 16 existing disabled-provider skips,
Ruff, package installation, and CLI smoke checks. All workflow scripts passed
actionlint with ShellCheck and Bash syntax validation. Hosted CI runs these
checks again for each published revision.

## Qualification and evidence

[The frozen qualification matrix](docs/qualification.md) exercises adversarial
Git and mocked API states with explicit preservation boundaries. Reports expose
a ranked inspection queue, observed source identities and missing evidence.
Offline reproduction retains the original snapshot; it never fetches or acts.
A passing fixture or green PR observation does not grant cleanup or delivery
authority. Store real reports outside this public checkout.
