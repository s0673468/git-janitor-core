from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping

from .git import parse_github_remote, run_command
from .github import classify_check_rollup

# Re-exported: tests and callers construct results as pr_shepherd.CommandResult.
from .models import CommandResult  # noqa: F401

from .repo_facts import (
    DEFAULT_OUTPUT,
    DEFAULT_OWNER,
    SCHEMA_VERSION,
    collect_all,
    collect_one,
    fresh_enough,
    load_cache,
    write_cache,
)
from .review_stall import ReviewRound
from .review_chain import (
    DEFAULT_LUNA_MODEL,
    REVIEW_CONTEXT_LINES,
    REVIEW_RECEIPT_SCHEMA,
    ReviewResult,
    run_luna_review,
)


DEFAULT_ALERT_COMMAND = Path("~/.config/git-janitor/alert-command").expanduser()
DEFAULT_MERGE_LEDGER = Path("reports/pr-shepherd-audit.jsonl")
PROTECTION_MAX_AGE = timedelta(days=7)
RUNNER_MAX_AGE = timedelta(hours=1)
# Sweeps pin `now` before their network calls, so a check created while the
# sweep runs — or ordinary skew between this host's clock and GitHub's — lands
# after it. Tolerate that much without treating the evidence as malformed.
SWEEP_CLOCK_SKEW = timedelta(minutes=5)
BOT_LOGIN = "chatgpt-codex-connector[bot]"
FINDING_PRIORITY_RE = re.compile(r"\bP([0-3])\b")
BLOCKING_PRIORITIES = frozenset({"P1"})
# Historical readback only. New Codex-bot requests are disabled below and have
# no parser or dispatch path.
REVIEW_REQUEST_RE = re.compile(r"^\s*@codex\s+review\b", re.IGNORECASE)
DEFAULT_REVIEW_LOCK_DIR = Path("~/.local/state/git-janitor/review-locks").expanduser()
DEFAULT_PROVIDER_REVIEW_LOCK_DIR = Path(
    "~/.local/state/git-janitor/provider-review-locks"
).expanduser()
DEFAULT_PROVIDER_REVIEW_TIMEOUT_SECONDS = 40 * 60
DEFAULT_LUNA_REVIEW_TIMEOUT_SECONDS = DEFAULT_PROVIDER_REVIEW_TIMEOUT_SECONDS
REVIEW_ALREADY_RECEIPTED_EXIT = 6
PROVIDER_STALL_TRAILING_ROUNDS = 2
GROK_REVIEW_RECEIPT_PREFIX = "<!-- git-janitor-review-receipt "
GROK_REVIEW_RECEIPT_LOGIN = "example-owner"
GROK_REVIEW_RECEIPT_KEYS = frozenset(
    {"provider", "model", "head", "report_sha256", "outcome", "blocking"}
)
# Optional marker keys added by finding verification. Kept optional so receipts
# posted before verification existed still parse.
GROK_REVIEW_RECEIPT_OPTIONAL_KEYS = frozenset(
    {
        "findings",
        "unverifiable",
        "p1",
        "p2",
        "p3",
        "reasoning",
        "base",
        "base_ref",
        "schema",
        "diff_sha256",
        "files_covered",
        "coverage_status",
        "context_lines",
        "input_omitted",
        "input_truncated",
        "context_omitted",
        "context_truncated",
        "elapsed_ms",
        "attempts",
    }
)
PROVIDER_REVIEW_COMMENTS_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      comments(first:100,after:$after){
        pageInfo{hasNextPage endCursor}
        nodes{body author{login}}
      }
    }
  }
}
""".strip()
ADD_PROVIDER_REVIEW_COMMENT_MUTATION = """
mutation($subjectId:ID!,$body:String!){
  addComment(input:{subjectId:$subjectId,body:$body}){
    commentEdge{node{body author{login}}}
  }
}
""".strip()
FIX_FORWARD_PREFIX = "<!-- git-janitor-fix-forward "
FIX_FORWARD_KEYS = frozenset({"receipt_head", "merge_head", "summary_sha256"})
REVIEW_DISPOSITION_PREFIX = "<!-- git-janitor-review-disposition "
REVIEW_DISPOSITION_SCHEMA = "luna-review-disposition/v1"
REVIEW_DISPOSITION_KEYS = frozenset(
    {
        "schema",
        "report_sha256",
        "head",
        "findings",
        "confirmed",
        "dismissed",
        "unverifiable",
        "complete",
        "post_merge_escape_refs",
    }
)
POST_MERGE_ESCAPE_RE = re.compile(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*"
)
UNANSWERED_REVIEW_TIMEOUT_SECONDS = DEFAULT_PROVIDER_REVIEW_TIMEOUT_SECONDS
ORPHAN_MERGED_PR_SCAN_MAX = 500
OPEN_PR_SCAN_LIMIT = 200
WATCH_RECEIPT_SCHEMA = "pr-shepherd.watch-receipt/v2"
DESTINATION_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_ALERT_SUBJECT_BYTES = 512
MAX_ALERT_BODY_BYTES = 65_536
ALERT_CONFIG_TIMEOUT_SECONDS = 10
ALERT_DELIVERY_TIMEOUT_SECONDS = 75
ALERT_TERM_GRACE_SECONDS = 2
ALERT_GROUP_VERIFY_SECONDS = 2
ALERT_GROUP_POLL_SECONDS = 0.05
MAX_RECEIPT_BYTES = 4_096
RECEIPT_LOCK_TIMEOUT_SECONDS = 5
MAX_REVIEW_THREAD_PAGES = 100
MAX_CHECK_CONTEXTS = 100
MAX_CHECK_SUITES = 100
WATCH_COMPLETED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|\+00:00)"
)
WATCH_RUN_ID_RE = re.compile(r"[0-9a-f]{64}")
WATCH_RUNTIME_SHA_RE = re.compile(r"[0-9a-f]{40}")
WATCH_MANIFEST_SHA_RE = re.compile(r"[0-9a-f]{64}")
WATCH_SHA256_RE = re.compile(r"[0-9a-f]{64}")
RECEIPT_RECOVERY_SEPARATOR = b"\x00\n"

STALL_EVIDENCE_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      number headRefOid autoMergeRequest{enabledAt}
      commits(last:1){totalCount nodes{commit{
        oid pushedDate committedDate
        checkSuites(first:100){pageInfo{hasNextPage endCursor} nodes{
          createdAt app{databaseId}
          checkRuns(first:100,filterBy:{checkType:LATEST}){
            pageInfo{hasNextPage endCursor}
            nodes{__typename name status conclusion startedAt completedAt checkSuite{createdAt}}
          }
        }}
        status{contexts{__typename context state createdAt}}
      }}}
    }
  }
}
""".strip()

# Unlike `gh pr checks --required`, this also works when a private Free plan
# cannot expose branch protection. Bind the complete inventory to the PR head.
GUARDED_CHECKS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      headRefOid baseRefOid baseRefName state isDraft mergeable
      commits(last:1){nodes{commit{oid statusCheckRollup{
        contexts(first:100){totalCount pageInfo{hasNextPage} nodes{
          __typename
          ... on CheckRun{name status conclusion}
          ... on StatusContext{context state}
        }}
      }}}}
    }
  }
}
""".strip()

CODE_REVIEW_ENABLED = False
MERGE_POLICY = "tests-only"

Runner = Any


def main(argv: list[str] | None = None, *, runner: Runner = run_command) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in {"review", "review-outcome"}:
        print("Code review is disabled; PR delivery uses tests and required CI.", file=sys.stderr)
        return 2

    if args.command == "watch" and args.receipt_path is not None:
        if (
            WATCH_RUN_ID_RE.fullmatch(args.run_id or "") is None
            or WATCH_RUNTIME_SHA_RE.fullmatch(args.runtime_sha or "") is None
            or WATCH_MANIFEST_SHA_RE.fullmatch(args.runtime_manifest_sha256 or "") is None
            or WATCH_SHA256_RE.fullmatch(args.destination_target_sha256 or "") is None
            or WATCH_SHA256_RE.fullmatch(args.gh_executable_sha256 or "") is None
            or WATCH_RUNTIME_SHA_RE.fullmatch(args.hermes_runtime_sha or "") is None
            or WATCH_SHA256_RE.fullmatch(args.hermes_executable_sha256 or "") is None
            or WATCH_SHA256_RE.fullmatch(args.hermes_runtime_manifest_sha256 or "") is None
        ):
            parser.error(
                "--receipt-path requires exact rollout, route, gh, and Hermes identities"
            )

    if args.command == "stalls":
        stalls = stalled_auto_merges(
            owner=args.owner,
            facts_path=args.repo_facts.expanduser(),
            min_age=timedelta(hours=args.min_age_hours),
            runner=runner,
        )
        print(json.dumps(stalls, indent=2, sort_keys=True))
        return 3 if _sweep_failure_codes(stalls) else (2 if stalls else 0)

    if args.command == "orphans":
        orphans = orphaned_findings(
            owner=args.owner,
            facts_path=args.repo_facts.expanduser(),
            days=args.days,
            runner=runner,
        )
        print(json.dumps(orphans, indent=2, sort_keys=True))
        return 3 if _sweep_failure_codes(orphans) else (2 if orphans else 0)

    if args.command == "watch":
        return daily_watch(
            owner=args.owner,
            facts_path=args.repo_facts.expanduser(),
            min_age=timedelta(hours=args.min_age_hours),
            days=args.days,
            alert_command=args.alert_command.expanduser(),
            alert_destination_alias=args.alert_destination_alias,
            require_alert_config=args.require_alert_config,
            receipt_path=args.receipt_path.expanduser() if args.receipt_path else None,
            run_id=args.run_id,
            runtime_sha=args.runtime_sha,
            runtime_manifest_sha256=args.runtime_manifest_sha256,
            destination_target_sha256=args.destination_target_sha256,
            gh_executable_sha256=args.gh_executable_sha256,
            hermes_runtime_sha=args.hermes_runtime_sha,
            hermes_executable_sha256=args.hermes_executable_sha256,
            hermes_runtime_manifest_sha256=args.hermes_runtime_manifest_sha256,
            refresh_facts=args.refresh_facts,
            runner=runner,
        )

    inference_cwd = None
    if args.command == "finish":
        target = args.worktree
        if not target.is_dir() and args.pr is None:
            raise SystemExit("finish target worktree is absent; pass --pr to resume cleanup")
        # The documented caller is a surviving checkout, which may be on main or
        # unrelated work. Never infer the task PR from that caller's branch.
        inference_cwd = target if target.is_dir() else target.parent
    repo = infer_repo(args.repo, cwd=inference_cwd, runner=runner)
    pr = infer_pr(args.pr, repo=repo, cwd=inference_cwd, runner=runner)
    if args.command == "review":
        return run_provider_review(
            repo=repo,
            pr=pr,
            high_risk=args.high_risk,
            allow_pending_ci=args.allow_pending_ci,
            timeout_seconds=DEFAULT_LUNA_REVIEW_TIMEOUT_SECONDS,
            force_review=args.force_review,
            new_scope=args.new_scope,
            hazard=args.hazard,
            runner=runner,
        )
    if args.command == "review-outcome":
        return record_review_outcome(
            repo=repo,
            pr=pr,
            confirmed=args.confirmed,
            dismissed=args.dismissed,
            unverifiable=args.unverifiable,
            post_merge_escape_refs=tuple(args.post_merge_escape_ref),
            runner=runner,
        )
    if args.command == "review-metrics":
        metrics = review_metrics(repo=repo, pr=pr, runner=runner)
        if metrics is None:
            print(f"{repo}#{pr}: no Luna v2 review receipt found.", file=sys.stderr)
            return 2
        print(json.dumps(metrics, indent=2, sort_keys=True))
        return 0
    if args.command == "threads":
        threads = list_threads(repo=repo, pr=pr, unresolved=args.unresolved, runner=runner)
        print(json.dumps(threads, indent=2, sort_keys=True))
        return 0
    if args.command == "resolve":
        return resolve_thread(
            repo=repo,
            pr=pr,
            thread_id=args.thread_id,
            reply=args.reply,
            runner=runner,
        )
    if args.command == "finish":
        from .delivery import finish_delivery

        return finish_delivery(
            repo=repo, pr=pr, worktree=args.worktree, expected_head=args.expected_head,
            ledger_path=args.ledger.expanduser().resolve(), runner=runner,
            merge_options={
                "facts_path": args.repo_facts.expanduser(),
                "alert_command": args.alert_command.expanduser(),
                "alert_destination_alias": args.alert_destination_alias,
                "allow_unresolved": args.allow_unresolved,
                "fix_forward": args.fix_forward, "summary": args.summary,
                "required_checks": tuple(args.required_check),
            },
        )
    if args.command == "merge":
        return guarded_merge(
            repo=repo,
            pr=pr,
            facts_path=args.repo_facts.expanduser(),
            alert_command=args.alert_command.expanduser(),
            alert_destination_alias=args.alert_destination_alias,
            ledger_path=args.ledger,
            allow_unresolved=args.allow_unresolved,
            fix_forward=args.fix_forward,
            summary=args.summary,
            expected_head=args.expected_head,
            required_checks=tuple(args.required_check),
            runner=runner,
        )
    raise AssertionError(f"unhandled command {args.command!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Explicit GitHub PR lifecycle helper.")
    parser.add_argument("--repo", help="owner/repo. Defaults to cwd origin.")
    parser.add_argument("--pr", type=int, help="Pull request number. Defaults to current branch PR.")
    parser.add_argument("--repo-facts", type=Path, default=DEFAULT_OUTPUT)
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser(
        "review",
        help="Disabled compatibility command; delivery uses tests and CI.",
    )
    review.add_argument(
        "--high-risk",
        action="store_true",
        help="Acknowledge that policy classifies this PR as high-risk.",
    )
    review.add_argument(
        "--allow-pending-ci", action="store_true",
        help="Ignored compatibility flag; review is disabled.",
    )
    review.add_argument(
        "--new-scope",
        help=(
            "Ignored compatibility option; review is disabled."
        ),
    )
    review.add_argument(
        "--hazard",
        help="Ignored compatibility option; review is disabled.",
    )
    review.add_argument(
        "--force",
        "--force-review",
        dest="force_review",
        action="store_true",
        help=(
            "Ignored compatibility flag; review is disabled."
        ),
    )

    outcome = sub.add_parser(
        "review-outcome",
        help="Disabled compatibility command; delivery uses tests and CI.",
    )
    outcome.add_argument("--confirmed", type=_finding_indices)
    outcome.add_argument("--dismissed", type=_finding_indices)
    outcome.add_argument("--unverifiable", type=_finding_indices)
    outcome.add_argument(
        "--post-merge-escape-ref",
        action="append",
        default=[],
        help="Durable issue reference such as owner/repo#123; may be repeated.",
    )

    sub.add_parser(
        "review-metrics",
        help="Read the current PR's compact Luna review measurement as JSON.",
    )
    threads = sub.add_parser("threads", help="List review threads as JSON.")
    threads.add_argument("--unresolved", action="store_true")

    resolve = sub.add_parser("resolve", help="Reply to and resolve one review thread.")
    resolve.add_argument("thread_id")
    resolve.add_argument("--reply", required=True)

    for command in ("merge", "finish"):
        merge = sub.add_parser(
            command, help="Guard delivery; finish also verifies and cleans an explicitly owned worktree.",
        )
        merge.add_argument("--repo-facts", type=Path, default=DEFAULT_OUTPUT)
        merge.add_argument("--alert-command", type=Path, default=DEFAULT_ALERT_COMMAND)
        merge.add_argument(
            "--alert-destination-alias",
            help="Non-secret alias for best-effort runner-offline alerts.",
        )
        merge.add_argument("--ledger", type=Path, default=DEFAULT_MERGE_LEDGER)
        merge.add_argument(
            "--fix-forward",
            action="store_true",
            help=(
                "Ignored historical flag; required tests always govern merge."
            ),
        )
        merge.add_argument(
            "--summary",
            help="Ignored historical option; no review summary is required.",
        )
        merge.add_argument(
            "--allow-unresolved",
            action="store_true",
            help="Ignored historical flag; it cannot bypass required tests.",
        )

        merge.add_argument(
            "--required-check", action="append", default=[], metavar="NAME",
            help="Expected check name; repeat for the full required set. Required with "
                 "--expected-head for private repositories without check protection.",
        )
        merge.add_argument(
            "--expected-head", required=command == "finish",
            help="Exact 40-character delivery SHA; required for unprotected private merges.",
        )
        if command == "finish":
            merge.set_defaults(ledger=Path("~/.local/state/git-janitor/delivery-audit.jsonl"))
            merge.add_argument("--worktree", required=True, type=Path,
                               help="Absolute task-owned worktree to clean; never the main checkout.")

    stalls = sub.add_parser("stalls", help="List auto-merge PRs parked for too long.")
    stalls.add_argument("--owner", default=DEFAULT_OWNER, required=not bool(DEFAULT_OWNER))
    stalls.add_argument("--repo-facts", type=Path, default=DEFAULT_OUTPUT)
    stalls.add_argument("--min-age-hours", type=float, default=2.0)

    orphans = sub.add_parser(
        "orphans",
        help="Retired compatibility command; returns an empty list without review queries.",
    )
    orphans.add_argument("--owner", default=DEFAULT_OWNER, required=not bool(DEFAULT_OWNER))
    orphans.add_argument("--repo-facts", type=Path, default=DEFAULT_OUTPUT)
    orphans.add_argument("--days", type=float, default=7.0)

    watch = sub.add_parser(
        "watch",
        help="Run the stalls and orphans sweeps; alert via the explicitly configured command on findings.",
    )
    watch.add_argument("--owner", default=DEFAULT_OWNER, required=not bool(DEFAULT_OWNER))
    watch.add_argument("--repo-facts", type=Path, default=DEFAULT_OUTPUT)
    watch.add_argument("--min-age-hours", type=float, default=2.0)
    watch.add_argument("--days", type=float, default=7.0)
    watch.add_argument("--alert-command", type=Path, default=DEFAULT_ALERT_COMMAND)
    watch.add_argument(
        "--alert-destination-alias",
        help="Non-secret destination alias forwarded to the alert command.",
    )
    watch.add_argument(
        "--require-alert-config",
        action="store_true",
        help="Fail closed unless the alert command validates its destination configuration.",
    )
    watch.add_argument(
        "--receipt-path",
        type=Path,
        help="Append one bounded JSONL receipt for this run (scheduled jobs should set this).",
    )
    watch.add_argument("--run-id", help=argparse.SUPPRESS)
    watch.add_argument("--runtime-sha", help=argparse.SUPPRESS)
    watch.add_argument("--runtime-manifest-sha256", help=argparse.SUPPRESS)
    watch.add_argument("--destination-target-sha256", help=argparse.SUPPRESS)
    watch.add_argument("--gh-executable-sha256", help=argparse.SUPPRESS)
    watch.add_argument("--hermes-runtime-sha", help=argparse.SUPPRESS)
    watch.add_argument("--hermes-executable-sha256", help=argparse.SUPPRESS)
    watch.add_argument("--hermes-runtime-manifest-sha256", help=argparse.SUPPRESS)
    watch.add_argument(
        "--refresh-facts",
        action="store_true",
        help="Rebuild the repo-facts cache first, for hosts where nothing else maintains it.",
    )
    return parser


def _finding_indices(value: str) -> tuple[int, ...]:
    if not value:
        return ()
    try:
        indices = tuple(int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "finding indices must be comma-separated positive integers"
        ) from exc
    if any(index < 1 for index in indices) or len(indices) != len(set(indices)):
        raise argparse.ArgumentTypeError(
            "finding indices must be unique positive integers"
        )
    return tuple(sorted(indices))


def infer_repo(repo: str | None, *, cwd: Path | None = None, runner: Runner = run_command) -> str:
    if repo:
        return repo
    result = runner(["git", "remote", "get-url", "origin"], cwd, 20)
    if result.returncode != 0:
        raise SystemExit(result.stderr or "cannot infer repository from cwd")
    parsed = parse_github_remote(result.stdout)
    if not parsed:
        raise SystemExit(f"origin is not a GitHub repo: {result.stdout}")
    return parsed


def infer_pr(pr: int | None, *, repo: str, cwd: Path | None = None, runner: Runner = run_command) -> int:
    if pr is not None:
        return pr
    # gh refuses branch inference when --repo is passed ("argument required
    # when using the --repo flag"), so resolve the branch and pass it as the
    # selector.
    branch_result = runner(["git", "branch", "--show-current"], cwd, 20)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    if not branch:
        raise SystemExit("cannot infer pull request: not on a branch; pass --pr")
    result = runner(
        ["gh", "pr", "view", branch, "--repo", repo, "--json", "number"], cwd, 30
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr or "cannot infer pull request")
    payload = _json(result.stdout, {})
    number = payload.get("number")
    if not number:
        raise SystemExit("cannot infer pull request number")
    return int(number)


def run_provider_review(
    *,
    repo: str,
    pr: int,
    high_risk: bool,
    allow_pending_ci: bool = False,
    timeout_seconds: int = DEFAULT_LUNA_REVIEW_TIMEOUT_SECONDS,
    repo_path: Path | None = None,
    lock_dir: Path = DEFAULT_PROVIDER_REVIEW_LOCK_DIR,
    runner: Runner = run_command,
    luna_adapter: Callable[..., ReviewResult] = run_luna_review,
    readback_attempts: int = 3,
    readback_poll_interval: float = 1.0,
    force_review: bool = False,
    new_scope: str | None = None,
    hazard: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Reject stale callers before any runner, credential, or provider access."""
    print("Code review is disabled; PR delivery uses tests and required CI.", file=sys.stderr)
    return 2


def _pr_review_context(
    *, repo: str, pr: int, runner: Runner = run_command
) -> tuple[str, str, str] | None:
    result = runner(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "headRefOid,baseRefName,baseRefOid",
        ],
        None,
        30,
    )
    if result.returncode != 0:
        print(result.stderr or "failed to read PR review context", file=sys.stderr)
        return None
    payload = _json(result.stdout, None)
    if not isinstance(payload, dict):
        print("PR review context is malformed.", file=sys.stderr)
        return None
    head = payload.get("headRefOid")
    base_ref = payload.get("baseRefName")
    base = payload.get("baseRefOid")
    if (
        not isinstance(head, str)
        or re.fullmatch(r"[0-9a-f]{40}", head) is None
        or not isinstance(base, str)
        or re.fullmatch(r"[0-9a-f]{40}", base) is None
        or not isinstance(base_ref, str)
        or not base_ref
    ):
        print("PR review context is incomplete.", file=sys.stderr)
        return None
    return head, base_ref, base


def _review_checkout_context(
    *,
    expected_head: str,
    repo_path: Path,
    runner: Runner = run_command,
) -> Path | None:
    root = runner(["git", "rev-parse", "--show-toplevel"], repo_path, 20)
    if root.returncode != 0:
        print(root.stderr or "cannot resolve repository checkout", file=sys.stderr)
        return None
    resolved = Path(root.stdout).expanduser()
    if not resolved.is_absolute():
        print("Repository checkout path is not absolute.", file=sys.stderr)
        return None
    head = runner(["git", "rev-parse", "HEAD"], resolved, 20)
    if head.returncode != 0:
        print(head.stderr or "cannot resolve checkout HEAD", file=sys.stderr)
        return None
    if head.stdout != expected_head:
        print(
            "Local checkout is not at the exact PR head; check out or update the PR branch.",
            file=sys.stderr,
        )
        return None
    status = runner(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        resolved,
        20,
    )
    if status.returncode != 0:
        print(status.stderr or "cannot verify worktree cleanliness", file=sys.stderr)
        return None
    if status.stdout:
        print(
            "Local worktree is dirty; refusing an exact-head provider review.",
            file=sys.stderr,
        )
        return None
    return resolved


def _is_rest_issue_comment_not_found(result: CommandResult) -> bool:
    detail = (result.stderr or "").casefold()
    return result.returncode != 0 and "not found" in detail and "http 404" in detail


def _graphql_provider_review_comments(
    *,
    repo: str,
    pr: int,
    runner: Runner,
) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    comments: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    while True:
        pages += 1
        if pages > MAX_REVIEW_THREAD_PAGES:
            raise RuntimeError("provider review receipt GraphQL pagination exceeded the page limit")
        args = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={PROVIDER_REVIEW_COMMENTS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr}",
        ]
        if cursor is not None:
            args.extend(["-f", f"after={cursor}"])
        result = runner(args, None, 30)
        if result.returncode != 0:
            raise RuntimeError(
                "failed to read provider review receipts through GraphQL: "
                f"{result.stderr or 'unknown error'}"
            )
        payload = _json(result.stdout, None)
        if not isinstance(payload, dict) or payload.get("errors"):
            raise RuntimeError(
                "provider review receipt GraphQL query returned errors or malformed data"
            )
        data = payload.get("data")
        repository = data.get("repository") if isinstance(data, dict) else None
        pull_request = (
            repository.get("pullRequest") if isinstance(repository, dict) else None
        )
        connection = (
            pull_request.get("comments") if isinstance(pull_request, dict) else None
        )
        nodes = connection.get("nodes") if isinstance(connection, dict) else None
        page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
        if (
            not isinstance(nodes, list)
            or not isinstance(page_info, dict)
            or not isinstance(page_info.get("hasNextPage"), bool)
        ):
            raise RuntimeError("provider review receipt GraphQL query returned incomplete data")
        for node in nodes:
            body = node.get("body") if isinstance(node, dict) else None
            author = node.get("author") if isinstance(node, dict) else None
            login = author.get("login") if isinstance(author, dict) else None
            if not isinstance(body, str) or (login is not None and not isinstance(login, str)):
                raise RuntimeError(
                    "provider review receipt GraphQL query returned malformed comment data"
                )
            comments.append({"body": body, "user": {"login": login}})
        if not page_info["hasNextPage"]:
            return comments
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise RuntimeError(
                "provider review receipt GraphQL pagination reported a next page "
                "without a cursor"
            )
        if next_cursor in seen_cursors:
            raise RuntimeError(
                "provider review receipt GraphQL pagination cursor did not advance"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _read_provider_review_comments(
    *,
    repo: str,
    pr: int,
    runner: Runner = run_command,
) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    result = runner(
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/issues/{pr}/comments?per_page=100",
            "--paginate",
            "--slurp",
        ],
        None,
        30,
    )
    if result.returncode != 0:
        if _is_rest_issue_comment_not_found(result):
            return _graphql_provider_review_comments(
                repo=repo,
                pr=pr,
                runner=runner,
            )
        raise RuntimeError(
            f"failed to read provider review receipts: {result.stderr or 'unknown error'}"
        )
    payload = _json(result.stdout, None)
    pages = payload if isinstance(payload, list) else None
    if pages is None or any(not isinstance(page, list) for page in pages):
        raise RuntimeError(
            "provider review receipt query returned malformed pagination data"
        )
    comments: list[dict[str, Any]] = []
    for page in pages:
        for item in page:
            if not isinstance(item, dict):
                raise RuntimeError(
                    "provider review receipt query returned malformed comment data"
                )
            comments.append(item)
    return comments


def _post_provider_review_receipt(
    *,
    repo: str,
    pr: int,
    comment: str,
    runner: Runner = run_command,
) -> CommandResult:
    rest_args = [
        "gh",
        "pr",
        "comment",
        str(pr),
        "--repo",
        repo,
        "--body",
        comment,
    ]
    result = runner(rest_args, None, 30)
    if result.returncode == 0 or not _is_rest_issue_comment_not_found(result):
        return result
    view_args = ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "id"]
    view = runner(view_args, None, 30)
    pull_request_id = _json(view.stdout, {}).get("id") if view.returncode == 0 else None
    if not isinstance(pull_request_id, str) or not pull_request_id:
        return CommandResult(
            view_args,
            1,
            "",
            view.stderr or "failed to resolve PR GraphQL identity for review receipt",
        )
    mutation_args = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={ADD_PROVIDER_REVIEW_COMMENT_MUTATION}",
        "-f",
        f"subjectId={pull_request_id}",
        "-f",
        f"body={comment}",
    ]
    mutation = runner(mutation_args, None, 30)
    if mutation.returncode != 0:
        return mutation
    payload = _json(mutation.stdout, None)
    node = (
        payload.get("data", {})
        .get("addComment", {})
        .get("commentEdge", {})
        .get("node")
        if isinstance(payload, dict) and not payload.get("errors")
        else None
    )
    author = node.get("author") if isinstance(node, dict) else None
    if (
        not isinstance(node, dict)
        or node.get("body") != comment
        or not isinstance(author, dict)
        or author.get("login") != GROK_REVIEW_RECEIPT_LOGIN
    ):
        return CommandResult(
            mutation_args,
            1,
            "",
            "GraphQL review receipt post returned incomplete or mismatched data",
        )
    return mutation


def grok_review_receipt_state(
    *,
    repo: str,
    pr: int,
    head_sha: str,
    required_base_sha: str | None = None,
    required_base_ref: str | None = None,
    sticky_prior_blocking: bool = False,
    require_current_if_reviewed: bool = False,
    latest_only: bool = False,
    validate_diff: bool = False,
    repo_path: Path | None = None,
    runner: Runner = run_command,
) -> dict[str, Any] | None:
    if required_base_sha is not None and re.fullmatch(
        r"[0-9a-f]{40}", required_base_sha
    ) is None:
        raise ValueError("required_base_sha must be a full lowercase Git SHA")
    if required_base_ref is not None and not _valid_receipt_base_ref(required_base_ref):
        raise ValueError("required_base_ref must be a safe branch name")
    if validate_diff and (required_base_sha is None or required_base_ref is None):
        raise ValueError("diff validation requires the exact base SHA and ref")
    comments = _read_provider_review_comments(
        repo=repo,
        pr=pr,
        runner=runner,
    )
    pages = [comments]

    current_latest: dict[str, Any] | None = None
    current_blocking: dict[str, Any] | None = None
    prior_latest: dict[str, Any] | None = None
    prior_blocking: dict[str, Any] | None = None
    latest_receipt: dict[str, Any] | None = None
    latest_authorizes = False
    expected_identities: dict[str, tuple[str, tuple[str, ...]]] = {}
    for page in pages:
        for item in page:
            body = item.get("body") if isinstance(item, dict) else None
            user = item.get("user") if isinstance(item, dict) else None
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(body, str) or login != GROK_REVIEW_RECEIPT_LOGIN:
                continue
            first_line = body.splitlines()[0] if body else ""
            if not first_line.startswith(GROK_REVIEW_RECEIPT_PREFIX):
                continue
            receipt = _receipt_marker_payload(first_line)
            latest_receipt = receipt
            latest_authorizes = True
            if required_base_sha is not None or required_base_ref is not None:
                authorizes = _receipt_authorizes_luna(
                    receipt,
                    base_sha=(
                        required_base_sha
                        if required_base_sha is not None
                        else receipt.get("base")
                    ),
                    base_ref=(
                        required_base_ref
                        if required_base_ref is not None
                        else receipt.get("base_ref")
                    ),
                )
                if authorizes and validate_diff:
                    receipt_head = receipt["head"]
                    if receipt_head not in expected_identities:
                        expected_identities[receipt_head] = _exact_review_input_identity(
                            base_sha=required_base_sha,
                            head_sha=receipt_head,
                            repo_path=repo_path or Path.cwd(),
                            runner=runner,
                        )
                    expected_identity = expected_identities[receipt_head]
                    authorizes = _receipt_authorizes_luna(
                        receipt,
                        base_sha=required_base_sha,
                        base_ref=required_base_ref,
                        expected_diff_sha256=expected_identity[0],
                        expected_files_covered=expected_identity[1],
                    )
                if not authorizes:
                    latest_authorizes = False
                    continue
            if receipt["head"] == head_sha:
                current_latest = receipt
                if receipt["blocking"]:
                    current_blocking = receipt
            else:
                prior_latest = receipt
                if receipt["blocking"]:
                    prior_blocking = receipt
    if latest_only:
        if latest_receipt is None:
            return None
        if not latest_authorizes:
            if require_current_if_reviewed:
                stale = dict(latest_receipt)
                stale["_stale"] = True
                stale["_requires_fresh_luna"] = True
                return stale
            return latest_receipt
        if latest_receipt["head"] == head_sha:
            return latest_receipt
        if sticky_prior_blocking and latest_receipt["blocking"]:
            return latest_receipt
        if require_current_if_reviewed:
            stale = dict(latest_receipt)
            stale["_stale"] = True
            return stale
        return None
    if require_current_if_reviewed and latest_receipt is not None and not latest_authorizes:
        stale = dict(latest_receipt)
        stale["_stale"] = True
        stale["_requires_fresh_luna"] = True
        return stale
    if current_latest is not None:
        return current_blocking or current_latest
    if sticky_prior_blocking:
        if prior_blocking is not None:
            return prior_blocking
    if require_current_if_reviewed and prior_latest is not None:
        stale = dict(prior_latest)
        stale["_stale"] = True
        return stale
    return None


def review_round_history(
    *,
    repo: str,
    pr: int,
    luna_only: bool = False,
    required_base_sha: str | None = None,
    required_base_ref: str | None = None,
    runner: Runner = run_command,
) -> tuple[ReviewRound, ...]:
    """Return every posted provider review receipt, oldest first.

    Receipts written before finding counts existed in the marker fall back to
    counting the rendered finding list in the comment body, so an in-flight PR
    does not lose its history when the helper is upgraded.
    """
    comments = _read_provider_review_comments(
        repo=repo,
        pr=pr,
        runner=runner,
    )
    pages = [comments]

    rounds: list[ReviewRound] = []
    for page in pages:
        for item in page:
            body = item.get("body") if isinstance(item, dict) else None
            user = item.get("user") if isinstance(item, dict) else None
            login = user.get("login") if isinstance(user, dict) else None
            if not isinstance(body, str) or login != GROK_REVIEW_RECEIPT_LOGIN:
                continue
            first_line = body.splitlines()[0] if body else ""
            if not first_line.startswith(GROK_REVIEW_RECEIPT_PREFIX):
                continue
            receipt = _receipt_marker_payload(first_line)
            if luna_only and not _receipt_authorizes_luna(
                receipt,
                base_sha=(
                    required_base_sha
                    if required_base_sha is not None
                    else receipt.get("base")
                ),
                base_ref=(
                    required_base_ref
                    if required_base_ref is not None
                    else receipt.get("base_ref")
                ),
            ):
                # A newer trusted marker that no longer authorizes this exact
                # base invalidates older Luna history. The gate must reopen so
                # a fresh exact-head/base Luna pass can replace it instead of
                # tripping the one-pass refusal on stale authorization.
                rounds.clear()
                continue
            count = receipt.get("findings")
            if count is None:
                count = _rendered_finding_count(body)
            # Convergence is measured on ACTIONABLE findings. Counting ghosts
            # would let a series that improved from four real issues to one
            # real plus three unverifiable still read as flat, firing the stall
            # on a PR that is genuinely converging.
            actionable = max(0, int(count) - int(receipt.get("unverifiable", 0)))
            rounds.append(
                ReviewRound(
                    head=receipt["head"],
                    blocking=bool(receipt["blocking"]),
                    finding_count=actionable,
                )
            )
    return tuple(rounds)


def _provider_review_stalled(history: tuple[ReviewRound, ...]) -> bool:
    """Return whether the last two explicitly allowed blocking passes made no progress."""
    deduped: list[ReviewRound] = []
    for entry in history:
        if deduped and deduped[-1].head == entry.head:
            deduped[-1] = entry
        else:
            deduped.append(entry)

    trailing: list[ReviewRound] = []
    for entry in reversed(deduped):
        if not entry.blocking:
            break
        trailing.append(entry)
    trailing.reverse()
    if len(trailing) <= PROVIDER_STALL_TRAILING_ROUNDS:
        return False
    counts = [entry.finding_count for entry in trailing]
    window = counts[-PROVIDER_STALL_TRAILING_ROUNDS:]
    baseline = min(counts[:-PROVIDER_STALL_TRAILING_ROUNDS])
    return min(window) >= baseline


def _describe_provider_review_stall(history: tuple[ReviewRound, ...]) -> str:
    blocking = [entry.finding_count for entry in history if entry.blocking]
    series = ", ".join(str(count) for count in blocking)
    return (
        "Provider review is not converging: the trailing blocking rounds show "
        f"no new low in the last {PROVIDER_STALL_TRAILING_ROUNDS} ({series}).\n"
        "Refusing to spend another provider pass. Verify the fix diff locally, "
        "then use tests-only delivery; historical review flags are obsolete. "
        "`--allow-unresolved` for the operator's explicit override."
    )


def _rendered_finding_count(body: str) -> int:
    return len(re.findall(r"^\d+\. \*\*P[123] ", body, re.MULTILINE))


def _receipt_marker_payload(comment_or_marker: str) -> dict[str, Any]:
    first_line = comment_or_marker.splitlines()[0] if comment_or_marker else ""
    if not first_line.startswith(GROK_REVIEW_RECEIPT_PREFIX) or not first_line.endswith(" -->"):
        raise RuntimeError("malformed provider review receipt marker")
    raw = first_line.removeprefix(GROK_REVIEW_RECEIPT_PREFIX).removesuffix(" -->")
    payload = _json(raw, None)
    if not isinstance(payload, dict) or not (
        GROK_REVIEW_RECEIPT_KEYS
        <= set(payload)
        <= (GROK_REVIEW_RECEIPT_KEYS | GROK_REVIEW_RECEIPT_OPTIONAL_KEYS)
    ):
        raise RuntimeError("malformed provider review receipt payload")
    count_keys = {
        "findings",
        "unverifiable",
        "p1",
        "p2",
        "p3",
        "context_lines",
        "elapsed_ms",
        "attempts",
    }
    for optional in count_keys:
        count = payload.get(optional)
        if optional in payload and (
            isinstance(count, bool) or not isinstance(count, int) or count < 0
        ):
            raise RuntimeError("malformed provider review receipt payload")
    files_covered = payload.get("files_covered")
    if "files_covered" in payload and (
        not isinstance(files_covered, list)
        or not all(_valid_receipt_file_path(path) for path in files_covered)
        or len(files_covered) != len(set(files_covered))
    ):
        raise RuntimeError("malformed provider review receipt payload")
    for flag in (
        "input_omitted",
        "input_truncated",
        "context_omitted",
        "context_truncated",
    ):
        if flag in payload and not isinstance(payload.get(flag), bool):
            raise RuntimeError("malformed provider review receipt payload")
    if (
        payload.get("provider") not in {"luna", "grok", "gemini"}
        or not isinstance(payload.get("model"), str)
        or not payload["model"]
        or not isinstance(payload.get("head"), str)
        or re.fullmatch(r"[0-9a-f]{40}", payload["head"]) is None
        or not isinstance(payload.get("report_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["report_sha256"]) is None
        or payload.get("outcome") not in {"clean", "findings"}
        or payload.get("reasoning", "legacy") not in {"max", "legacy"}
        or (payload.get("provider") == "luna" and payload.get("reasoning") != "max")
        or (
            "base" in payload
            and (
                not isinstance(payload.get("base"), str)
                or re.fullmatch(r"[0-9a-f]{40}", payload["base"]) is None
            )
        )
        or (
            "base_ref" in payload
            and not _valid_receipt_base_ref(payload.get("base_ref"))
        )
        or (
            "schema" in payload
            and payload.get("schema") != REVIEW_RECEIPT_SCHEMA
        )
        or (
            "diff_sha256" in payload
            and (
                not isinstance(payload.get("diff_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", payload["diff_sha256"]) is None
            )
        )
        or (
            "coverage_status" in payload
            and payload.get("coverage_status")
            not in {"complete", "incomplete", "too_broad"}
        )
        or not isinstance(payload.get("blocking"), bool)
        or (payload["outcome"] == "clean" and payload["blocking"])
    ):
        raise RuntimeError("malformed provider review receipt payload")
    return payload


def _receipt_authorizes_luna(
    receipt: Mapping[str, Any],
    *,
    base_sha: object,
    base_ref: object,
    expected_diff_sha256: str | None = None,
    expected_files_covered: tuple[str, ...] | None = None,
) -> bool:
    return (
        receipt.get("provider") == "luna"
        and receipt.get("model") == DEFAULT_LUNA_MODEL
        and receipt.get("reasoning") == "max"
        and isinstance(base_sha, str)
        and re.fullmatch(r"[0-9a-f]{40}", base_sha) is not None
        and receipt.get("base") == base_sha
        and isinstance(base_ref, str)
        and _valid_receipt_base_ref(base_ref)
        and receipt.get("base_ref") == base_ref
        and receipt.get("schema") == REVIEW_RECEIPT_SCHEMA
        and isinstance(receipt.get("diff_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", receipt["diff_sha256"]) is not None
        and isinstance(receipt.get("files_covered"), list)
        and all(_valid_receipt_file_path(path) for path in receipt["files_covered"])
        and len(receipt["files_covered"]) == len(set(receipt["files_covered"]))
        and receipt.get("coverage_status") == "complete"
        and receipt.get("context_lines") == REVIEW_CONTEXT_LINES
        and receipt.get("input_omitted") is False
        and receipt.get("input_truncated") is False
        and receipt.get("context_omitted") is False
        and receipt.get("context_truncated") is False
        and (
            expected_diff_sha256 is None
            or receipt.get("diff_sha256") == expected_diff_sha256
        )
        and (
            expected_files_covered is None
            or receipt.get("files_covered") == list(expected_files_covered)
        )
    )


def _exact_review_input_identity(
    *,
    base_sha: str,
    head_sha: str,
    repo_path: Path,
    runner: Runner = run_command,
) -> tuple[str, tuple[str, ...]]:
    """Recompute the exact diff identity that an authorizing Luna receipt claims."""
    comparison = f"{base_sha}...{head_sha}"
    names = runner(
        ["git", "diff", "--no-ext-diff", "--name-only", comparison],
        repo_path,
        45,
    )
    if names.returncode != 0:
        raise RuntimeError(
            names.stderr or "failed to recompute the reviewed changed-file list"
        )
    raw_names = [line for line in names.stdout.splitlines() if line]
    files_covered = tuple(path for path in raw_names if _valid_receipt_file_path(path))
    if len(files_covered) != len(raw_names):
        raise RuntimeError("actual review diff contains an unsafe changed-file path")

    diff = runner(
        [
            "git",
            "diff",
            "--no-ext-diff",
            f"--unified={REVIEW_CONTEXT_LINES}",
            comparison,
        ],
        repo_path,
        45,
    )
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr or "failed to recompute the reviewed diff")
    return hashlib.sha256(diff.stdout.encode("utf-8")).hexdigest(), files_covered


def _valid_receipt_file_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    if any(ord(character) < 32 for character in value):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and str(path) == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _valid_receipt_base_ref(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    if value.endswith(("/", ".", ".lock")) or value.startswith("."):
        return False
    if ".." in value or "//" in value or "@{" in value:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    if any(character in " ~^:?*[\\" for character in value):
        return False
    return all(
        part not in {"", ".", ".."} and not part.startswith(".")
        for part in value.split("/")
    )


def _format_fix_forward_comment(
    *,
    summary: str,
    receipt_head: str,
    merge_head: str,
) -> str:
    normalized_summary = " ".join(summary.split())
    if not normalized_summary:
        raise ValueError("fix-forward summary must be non-empty")
    if (
        re.fullmatch(r"[0-9a-f]{40}", receipt_head) is None
        or re.fullmatch(r"[0-9a-f]{40}", merge_head) is None
    ):
        raise ValueError("fix-forward heads must be full lowercase Git SHAs")
    marker = {
        "receipt_head": receipt_head,
        "merge_head": merge_head,
        "summary_sha256": hashlib.sha256(
            normalized_summary.encode("utf-8")
        ).hexdigest(),
    }
    return (
        normalized_summary
        + "\n\n"
        + FIX_FORWARD_PREFIX
        + json.dumps(marker)
        + " -->"
    )


def _fix_forward_marker_payload(body: str) -> dict[str, str] | None:
    marker_lines = [
        (index, line)
        for index, line in enumerate(body.splitlines())
        if line.startswith(FIX_FORWARD_PREFIX)
    ]
    if not marker_lines:
        return None
    if len(marker_lines) != 1:
        raise RuntimeError("malformed fix-forward marker")
    marker_index, marker_line = marker_lines[0]
    lines = body.splitlines()
    if marker_index != 2 or lines[1] != "" or marker_index != len(lines) - 1:
        raise RuntimeError("malformed fix-forward comment body")
    summary = lines[0]
    if not summary or summary != " ".join(summary.split()):
        raise RuntimeError("malformed fix-forward summary")
    if not marker_line.endswith(" -->"):
        raise RuntimeError("malformed fix-forward marker")
    raw = marker_line.removeprefix(FIX_FORWARD_PREFIX).removesuffix(" -->")
    payload = _json(raw, None)
    if not isinstance(payload, dict) or set(payload) != FIX_FORWARD_KEYS:
        raise RuntimeError("malformed fix-forward marker payload")
    if (
        not isinstance(payload.get("receipt_head"), str)
        or re.fullmatch(r"[0-9a-f]{40}", payload["receipt_head"]) is None
        or not isinstance(payload.get("merge_head"), str)
        or re.fullmatch(r"[0-9a-f]{40}", payload["merge_head"]) is None
        or not isinstance(payload.get("summary_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["summary_sha256"]) is None
        or payload["summary_sha256"]
        != hashlib.sha256(summary.encode("utf-8")).hexdigest()
    ):
        raise RuntimeError("malformed fix-forward marker payload")
    return payload


def _issue_comments(
    *,
    repo: str,
    pr: int,
    runner: Runner = run_command,
) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    result = runner(
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/issues/{pr}/comments?per_page=100",
            "--paginate",
            "--slurp",
        ],
        None,
        30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "failed to read PR issue comments")
    payload = _json(result.stdout, None)
    if not isinstance(payload, list) or any(
        not isinstance(page, list) for page in payload
    ):
        raise RuntimeError("PR issue comment query returned malformed pagination data")
    comments: list[dict[str, Any]] = []
    for page in payload:
        for item in page:
            if not isinstance(item, dict):
                raise RuntimeError("PR issue comment query returned malformed comment data")
            comments.append(item)
    return comments


def _review_disposition_marker_payload(comment_or_marker: str) -> dict[str, Any]:
    first_line = comment_or_marker.splitlines()[0] if comment_or_marker else ""
    if not first_line.startswith(REVIEW_DISPOSITION_PREFIX) or not first_line.endswith(
        " -->"
    ):
        raise RuntimeError("malformed Luna review disposition marker")
    raw = first_line.removeprefix(REVIEW_DISPOSITION_PREFIX).removesuffix(" -->")
    payload = _json(raw, None)
    if not isinstance(payload, dict) or set(payload) != REVIEW_DISPOSITION_KEYS:
        raise RuntimeError("malformed Luna review disposition payload")
    findings = payload.get("findings")
    if (
        payload.get("schema") != REVIEW_DISPOSITION_SCHEMA
        or not isinstance(payload.get("report_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["report_sha256"]) is None
        or not isinstance(payload.get("head"), str)
        or re.fullmatch(r"[0-9a-f]{40}", payload["head"]) is None
        or isinstance(findings, bool)
        or not isinstance(findings, int)
        or findings < 0
        or payload.get("complete") is not True
    ):
        raise RuntimeError("malformed Luna review disposition payload")
    groups: list[tuple[int, ...]] = []
    for key in ("confirmed", "dismissed", "unverifiable"):
        raw_group = payload.get(key)
        if (
            not isinstance(raw_group, list)
            or not all(
                isinstance(index, int) and not isinstance(index, bool) and index >= 1
                for index in raw_group
            )
            or raw_group != sorted(set(raw_group))
        ):
            raise RuntimeError("malformed Luna review disposition payload")
        groups.append(tuple(raw_group))
    classified = [index for group in groups for index in group]
    if len(classified) != len(set(classified)) or set(classified) != set(
        range(1, findings + 1)
    ):
        raise RuntimeError("Luna review disposition must classify every finding once")
    escape_refs = payload.get("post_merge_escape_refs")
    if (
        not isinstance(escape_refs, list)
        or not all(
            isinstance(reference, str)
            and POST_MERGE_ESCAPE_RE.fullmatch(reference) is not None
            for reference in escape_refs
        )
        or escape_refs != list(dict.fromkeys(escape_refs))
    ):
        raise RuntimeError("malformed Luna review disposition payload")
    return payload


def _latest_review_measurement_state(
    *,
    repo: str,
    pr: int,
    runner: Runner = run_command,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    review: dict[str, Any] | None = None
    dispositions: dict[str, dict[str, Any]] = {}
    for item in _issue_comments(repo=repo, pr=pr, runner=runner):
        body = item.get("body")
        user = item.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(body, str) or login != GROK_REVIEW_RECEIPT_LOGIN:
            continue
        first_line = body.splitlines()[0] if body else ""
        if first_line.startswith(GROK_REVIEW_RECEIPT_PREFIX):
            candidate = _receipt_marker_payload(first_line)
            if _receipt_authorizes_luna(
                candidate,
                base_sha=candidate.get("base"),
                base_ref=candidate.get("base_ref"),
            ):
                review = candidate
            continue
        if first_line.startswith(REVIEW_DISPOSITION_PREFIX):
            candidate = _review_disposition_marker_payload(first_line)
            dispositions[candidate["report_sha256"]] = candidate
    if review is None:
        return None, None
    return review, dispositions.get(review["report_sha256"])


def _format_review_disposition_comment(payload: Mapping[str, Any]) -> str:
    marker = (
        REVIEW_DISPOSITION_PREFIX
        + json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
        + " -->"
    )
    return "\n".join(
        (
            marker,
            "### Luna Max review outcome measurement",
            "",
            (
                f"Classified {payload['findings']} finding(s); "
                f"recorded {len(payload['post_merge_escape_refs'])} "
                "post-merge escape reference(s)."
            ),
        )
    )


def record_review_outcome(
    *,
    repo: str,
    pr: int,
    confirmed: tuple[int, ...] | None,
    dismissed: tuple[int, ...] | None,
    unverifiable: tuple[int, ...] | None,
    post_merge_escape_refs: tuple[str, ...] = (),
    runner: Runner = run_command,
) -> int:
    """Append one trusted, report-bound disposition and escape receipt."""
    if any(POST_MERGE_ESCAPE_RE.fullmatch(reference) is None for reference in post_merge_escape_refs):
        print(
            "Post-merge escape references must look like owner/repo#123.",
            file=sys.stderr,
        )
        return 2
    try:
        review, prior = _latest_review_measurement_state(repo=repo, pr=pr, runner=runner)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if review is None:
        print("No trusted Luna v2 review receipt exists for this PR.", file=sys.stderr)
        return 2
    findings = review.get("findings")
    if isinstance(findings, bool) or not isinstance(findings, int) or findings < 0:
        print("The Luna review receipt has no valid finding count.", file=sys.stderr)
        return 3

    explicit = any(group is not None for group in (confirmed, dismissed, unverifiable))
    if explicit:
        groups = (
            confirmed or (),
            dismissed or (),
            unverifiable or (),
        )
    elif prior is not None:
        groups = (
            tuple(prior["confirmed"]),
            tuple(prior["dismissed"]),
            tuple(prior["unverifiable"]),
        )
    elif findings == 0:
        groups = ((), (), ())
    else:
        print(
            "Classify every finding with --confirmed, --dismissed, or --unverifiable.",
            file=sys.stderr,
        )
        return 2
    classified = [index for group in groups for index in group]
    if len(classified) != len(set(classified)) or set(classified) != set(
        range(1, findings + 1)
    ):
        print(
            f"Disposition indices must classify each finding from 1 through {findings} once.",
            file=sys.stderr,
        )
        return 2
    prior_escapes = prior["post_merge_escape_refs"] if prior is not None else []
    escapes = list(dict.fromkeys([*prior_escapes, *post_merge_escape_refs]))
    payload: dict[str, Any] = {
        "schema": REVIEW_DISPOSITION_SCHEMA,
        "report_sha256": review["report_sha256"],
        "head": review["head"],
        "findings": findings,
        "confirmed": list(groups[0]),
        "dismissed": list(groups[1]),
        "unverifiable": list(groups[2]),
        "complete": True,
        "post_merge_escape_refs": escapes,
    }
    comment = _format_review_disposition_comment(payload)
    post = runner(
        ["gh", "pr", "comment", str(pr), "--repo", repo, "--body", comment],
        None,
        30,
    )
    if post.returncode != 0:
        print(post.stderr or "failed to post Luna review disposition", file=sys.stderr)
        return 3
    try:
        _, readback = _latest_review_measurement_state(repo=repo, pr=pr, runner=runner)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if readback != payload:
        print("Posted Luna review disposition could not be read back exactly.", file=sys.stderr)
        return 3
    print(f"{repo}#{pr}: recorded complete Luna review outcome measurement.")
    return 0


def review_metrics(
    *,
    repo: str,
    pr: int,
    runner: Runner = run_command,
) -> dict[str, Any] | None:
    review, disposition = _latest_review_measurement_state(
        repo=repo,
        pr=pr,
        runner=runner,
    )
    if review is None:
        return None
    return {
        "schema": review.get("schema"),
        "head": review["head"],
        "report_sha256": review["report_sha256"],
        "elapsed_ms": review.get("elapsed_ms"),
        "findings": review.get("findings"),
        "disposition": disposition or "pending",
    }


def fix_forward_marker_state(
    *,
    repo: str,
    pr: int,
    receipt_head: str,
    merge_head: str,
    runner: Runner = run_command,
) -> dict[str, str] | None:
    latest: dict[str, str] | None = None
    for item in _issue_comments(repo=repo, pr=pr, runner=runner):
        body = item.get("body")
        user = item.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(body, str) or login != GROK_REVIEW_RECEIPT_LOGIN:
            continue
        marker = _fix_forward_marker_payload(body)
        if (
            marker is not None
            and marker["receipt_head"] == receipt_head
            and marker["merge_head"] == merge_head
        ):
            latest = marker
    return latest


def _blocking_receipt_followed_by_fix_forward(
    *,
    repo: str,
    pr: int,
    receipt: dict[str, Any],
    merge_head: str,
    runner: Runner = run_command,
) -> bool:
    seen_receipt = False
    for item in _issue_comments(repo=repo, pr=pr, runner=runner):
        body = item.get("body")
        user = item.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(body, str) or login != GROK_REVIEW_RECEIPT_LOGIN:
            continue
        first_line = body.splitlines()[0] if body else ""
        if first_line.startswith(GROK_REVIEW_RECEIPT_PREFIX):
            posted_receipt = _receipt_marker_payload(first_line)
            seen_receipt = (
                posted_receipt["blocking"]
                and posted_receipt["head"] == receipt.get("head")
            )
            continue
        marker = _fix_forward_marker_payload(body)
        if (
            seen_receipt
            and marker is not None
            and marker["receipt_head"] == receipt.get("head")
            and marker["merge_head"] == merge_head
        ):
            return True
    return False


def _fix_forward_descendant(
    *,
    repo: str,
    receipt_head: str,
    merge_head: str,
    runner: Runner = run_command,
) -> bool:
    owner, name = repo.split("/", 1)
    result = runner(
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/compare/{receipt_head}...{merge_head}",
        ],
        None,
        30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "failed to compare fix-forward heads")
    payload = _json(result.stdout, None)
    status = payload.get("status") if isinstance(payload, dict) else None
    if status not in {"ahead", "behind", "diverged", "identical"}:
        raise RuntimeError("fix-forward comparison returned malformed status")
    return status in {"ahead", "identical"}


def _post_fix_forward_comment(
    *,
    repo: str,
    pr: int,
    summary: str,
    receipt_head: str,
    merge_head: str,
    readback_attempts: int,
    readback_poll_interval: float,
    runner: Runner = run_command,
) -> None:
    comment = _format_fix_forward_comment(
        summary=summary,
        receipt_head=receipt_head,
        merge_head=merge_head,
    )
    expected = _fix_forward_marker_payload(comment)
    post = runner(
        [
            "gh",
            "pr",
            "comment",
            str(pr),
            "--repo",
            repo,
            "--body",
            comment,
        ],
        None,
        30,
    )
    if post.returncode != 0:
        raise RuntimeError(post.stderr or "failed to post fix-forward summary")
    error = "posted fix-forward marker was not visible"
    for attempt in range(readback_attempts):
        try:
            readback = fix_forward_marker_state(
                repo=repo,
                pr=pr,
                receipt_head=receipt_head,
                merge_head=merge_head,
                runner=runner,
            )
            if readback == expected:
                return
            error = "posted fix-forward marker did not match"
        except RuntimeError as exc:
            error = str(exc)
        if attempt + 1 < readback_attempts:
            time.sleep(readback_poll_interval)
    raise RuntimeError(error)


def run_direct_codex_wait(
    *,
    repo: str,
    pr: int,
    timeout_seconds: int,
    poll_interval: float,
    nudge: bool,
    high_risk: bool,
    runner: Runner = run_command,
) -> int:
    print("Code review is disabled; use tests and CI.", file=sys.stderr)
    return 2
def current_pr_head(
    *, repo: str, pr: int, runner: Runner = run_command
) -> str | None:
    result = runner(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "headRefOid"],
        None,
        30,
    )
    if result.returncode != 0:
        return None
    head = _json(result.stdout, {}).get("headRefOid")
    return head if isinstance(head, str) and head else None


def wait_for_review(
    *,
    repo: str,
    pr: int,
    timeout_seconds: int,
    poll_interval: float,
    nudge: bool,
    high_risk: bool,
    expected_head_sha: str | None = None,
    lock_dir: Path = DEFAULT_REVIEW_LOCK_DIR,
    runner: Runner = run_command,
) -> int:
    print("Code review is disabled; use tests and CI.", file=sys.stderr)
    return 2
def request_review_once(
    *,
    repo: str,
    pr: int,
    lock_dir: Path = DEFAULT_REVIEW_LOCK_DIR,
    runner: Runner = run_command,
) -> str:
    print("Code review is disabled; use tests and CI.", file=sys.stderr)
    return "error"
@contextmanager
def review_request_lock(
    *, repo: str, pr: int, lock_dir: Path = DEFAULT_REVIEW_LOCK_DIR
) -> Iterator[int]:
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(f"{repo}#{pr}".encode()).hexdigest()
    lock_path = lock_dir / f"{digest}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _review_lock_marked(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, 64).strip() == b"requested"


def _mark_review_lock(fd: int) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, b"requested\n")
    os.fsync(fd)


def review_request_exists(
    *, repo: str, pr: int, runner: Runner = run_command
) -> bool | None:
    owner, name = repo.split("/", 1)
    result = runner(
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/issues/{pr}/comments?per_page=100",
            "--paginate",
            "--slurp",
        ],
        None,
        30,
    )
    if result.returncode != 0:
        return None
    invalid = object()
    payload = _json(result.stdout, invalid)
    if payload is invalid:
        return None
    pages = payload if payload and all(isinstance(page, list) for page in payload) else [payload]
    for page in pages:
        if not isinstance(page, list):
            return None
        for item in page:
            body = item.get("body") if isinstance(item, dict) else None
            if isinstance(body, str) and REVIEW_REQUEST_RE.match(body):
                return True
    return False


def _required_check_state(*, repo: str, pr: int, runner: Runner = run_command) -> str:
    result = runner(["gh", "pr", "checks", str(pr), "--repo", repo, "--required",
                     "--json", "bucket,state"], None, 60)
    if result.returncode not in {0, 8}:
        return "blocked"
    checks = _json(result.stdout, None)
    if not isinstance(checks, list) or not checks or any(not isinstance(c, dict) or c.get("bucket") not in {"pass", "pending"} for c in checks):
        return "blocked"
    return "pending" if any(c["bucket"] == "pending" for c in checks) else "green"



def _unprotected_check_state(
    *, repo: str, pr: int, expected: dict[str, Any],
    required_checks: tuple[str, ...], runner: Runner = run_command,
) -> str:
    """Require explicit expectations and a complete, exact-head green inventory.

    Missing protection never becomes permission to treat whatever checks happened
    to appear as the required set. Unknown/truncated evidence fails closed; pending
    checks can only be monitored, never used to arm unprotected auto-merge.
    """
    owner, name = repo.split("/", 1)
    result = runner([
        "gh", "api", "graphql", "-f", f"query={GUARDED_CHECKS_QUERY}",
        "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={pr}",
    ], None, 60)
    payload = _json(result.stdout, None)
    if result.returncode != 0 or not isinstance(payload, dict) or payload.get("errors"):
        return "blocked"
    try:
        current = payload["data"]["repository"]["pullRequest"]
        if (any(current[key] != expected[key] for key in (
                "headRefOid", "baseRefOid", "baseRefName", "state", "isDraft"))
                or current["mergeable"] != "MERGEABLE"):
            return "blocked"
        commits = current["commits"]["nodes"]
        if not isinstance(commits, list) or len(commits) != 1:
            return "blocked"
        commit = commits[0]["commit"]
        if commit["oid"] != expected["headRefOid"]:
            return "blocked"
        inventory = commit["statusCheckRollup"]["contexts"]
        checks = inventory["nodes"]
        if (not isinstance(checks, list) or not checks
                or inventory["pageInfo"]["hasNextPage"] is not False
                or type(inventory["totalCount"]) is not int
                or inventory["totalCount"] != len(checks) or len(checks) > 100):
            return "blocked"
        names, pending = set(), False
        for check in checks:
            if check["__typename"] == "CheckRun":
                name = check["name"]
                if check["status"] == "COMPLETED":
                    if check["conclusion"] != "SUCCESS":
                        return "blocked"
                elif (check["status"] in {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"}
                      and check["conclusion"] is None):
                    pending = True
                else:
                    return "blocked"
            elif check["__typename"] == "StatusContext":
                name = check["context"]
                if check["state"] == "PENDING":
                    pending = True
                elif check["state"] != "SUCCESS":
                    return "blocked"
            else:
                return "blocked"
            if not isinstance(name, str) or not name or name in names:
                return "blocked"
            names.add(name)
        if not set(required_checks).issubset(names):
            return "blocked"
        return "pending" if pending else "green"
    except (KeyError, TypeError, IndexError):
        return "blocked"


def required_checks_green(
    *, repo: str, pr: int, runner: Runner = run_command
) -> bool:
    result = runner(
        [
            "gh",
            "pr",
            "checks",
            str(pr),
            "--repo",
            repo,
            "--required",
            "--json",
            "bucket,state",
        ],
        None,
        60,
    )
    if result.returncode != 0:
        return False
    checks = _json(result.stdout, None)
    return bool(checks) and isinstance(checks, list) and all(
        isinstance(check, dict) and check.get("bucket") == "pass" for check in checks
    )


def required_checks_reviewable(*, repo: str, pr: int, runner: Runner = run_command) -> bool:
    """Opt-in overlap: a nonempty, fully known set of pass/pending required checks."""
    result = runner(
        ["gh", "pr", "checks", str(pr), "--repo", repo, "--required", "--json", "bucket,state"],
        None, 60,
    )
    if result.returncode not in (0, 8):  # gh uses 8 for pending checks
        return False
    checks = _json(result.stdout, None)
    return bool(checks) and isinstance(checks, list) and all(
        isinstance(check, dict) and check.get("bucket") in {"pass", "pending"}
        for check in checks
    )


def review_arrived(*, repo: str, pr: int, runner: Runner = run_command) -> bool:
    owner, name = repo.split("/", 1)
    head_result = runner(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "headRefOid"],
        None,
        30,
    )
    if head_result.returncode != 0:
        return False
    head = _json(head_result.stdout, {}).get("headRefOid")
    if not isinstance(head, str) or not head:
        return False
    result = runner(["gh", "api", f"repos/{owner}/{name}/pulls/{pr}/reviews"], None, 30)
    if result.returncode != 0:
        return False
    for item in _json(result.stdout, []):
        user = item.get("user") if isinstance(item, dict) else None
        login = user.get("login") if isinstance(user, dict) else None
        commit_id = item.get("commit_id") if isinstance(item, dict) else None
        if commit_id == head and (
            login == BOT_LOGIN or (login and "chatgpt-codex" in login)
        ):
            return True
    return False


def list_threads(
    *,
    repo: str,
    pr: int,
    unresolved: bool,
    runner: Runner = run_command,
) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    while True:
        pages += 1
        if pages > MAX_REVIEW_THREAD_PAGES:
            raise RuntimeError("review-thread pagination exceeded the page limit")
        after = f', after: "{cursor}"' if cursor else ""
        query = (
            "query { repository(owner: \"%s\", name: \"%s\") { pullRequest(number: %d) { "
            "reviewThreads(first: 100%s) { pageInfo { hasNextPage endCursor } "
            "nodes { id isResolved isOutdated path line "
            "comments(first: 1) { nodes { databaseId author { login } body path line } } } } } } }"
            % (owner, name, pr, after)
        )
        result = runner(["gh", "api", "graphql", "-f", f"query={query}"], None, 30)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "failed to list review threads")
        review_threads = _review_threads_payload(result.stdout)
        threads.extend(
            _thread_summary(node)
            for node in review_threads["nodes"]
            if isinstance(node, dict)
        )
        page_info = review_threads.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor:
            raise RuntimeError("review-thread pagination reported a next page without a cursor")
        if next_cursor in seen_cursors:
            raise RuntimeError("review-thread pagination cursor did not advance")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    if unresolved:
        threads = [thread for thread in threads if not thread["isResolved"]]
    return threads


def _review_threads_payload(stdout: str) -> dict[str, Any]:
    """Extract reviewThreads strictly; raise instead of treating bad payloads as empty."""
    payload = _json(stdout, None)
    if not isinstance(payload, dict):
        raise RuntimeError("review-thread query returned an unparseable response")
    if payload.get("errors"):
        raise RuntimeError(f"review-thread query returned errors: {payload['errors']}")
    data = payload.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
    review_threads = pull_request.get("reviewThreads") if isinstance(pull_request, dict) else None
    if not isinstance(review_threads, dict) or not isinstance(review_threads.get("nodes"), list):
        raise RuntimeError("review-thread query returned no thread data")
    page_info = review_threads.get("pageInfo")
    if (
        not isinstance(page_info, dict)
        or not isinstance(page_info.get("hasNextPage"), bool)
        or (page_info["hasNextPage"] and not isinstance(page_info.get("endCursor"), str))
    ):
        raise RuntimeError("review-thread query returned incomplete pagination data")
    if any(not _complete_review_thread(node) for node in review_threads["nodes"]):
        raise RuntimeError("review-thread query returned an incomplete thread record")
    return review_threads


def _complete_review_thread(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    required = {"id", "isResolved", "isOutdated", "path", "line", "comments"}
    if not required.issubset(node):
        return False
    comments = node.get("comments")
    comment_nodes = comments.get("nodes") if isinstance(comments, dict) else None
    if not isinstance(comment_nodes, list) or not comment_nodes:
        return False
    first = comment_nodes[0]
    if not isinstance(first, dict):
        return False
    author = first.get("author")
    return (
        isinstance(node["id"], str)
        and isinstance(node["isResolved"], bool)
        and isinstance(node["isOutdated"], bool)
        and isinstance(first.get("databaseId"), int)
        and isinstance(author, dict)
        and isinstance(author.get("login"), str)
        and isinstance(first.get("body"), str)
    )


def finding_priority(body: str | None) -> str | None:
    match = FINDING_PRIORITY_RE.search((body or "")[:400])
    return f"P{match.group(1)}" if match else None


def unresolved_blocking_findings(
    *,
    repo: str,
    pr: int,
    runner: Runner = run_command,
) -> list[dict[str, Any]]:
    """Unresolved Codex review threads whose finding is P1."""
    findings = []
    for thread in list_threads(repo=repo, pr=pr, unresolved=True, runner=runner):
        author = thread.get("author") or ""
        if "chatgpt-codex" not in author:
            continue
        priority = finding_priority(thread.get("body"))
        if priority not in BLOCKING_PRIORITIES:
            continue
        body = thread.get("body") or ""
        findings.append(
            {
                "thread_id": thread["thread_id"],
                "priority": priority,
                "path": thread.get("path"),
                "line": thread.get("line"),
                "title": body.splitlines()[0][:160] if body else "",
            }
        )
    return findings


def orphaned_findings(
    *,
    owner: str,
    facts_path: Path,
    days: float,
    runner: Runner = run_command,
) -> list[dict[str, Any]]:
    """Review-orphan maintenance is retired under tests-only delivery."""
    return []


def resolve_thread(
    *,
    repo: str,
    pr: int,
    thread_id: str,
    reply: str,
    runner: Runner = run_command,
) -> int:
    if not reply.strip():
        print("refusing to resolve without a non-empty reply", file=sys.stderr)
        return 2
    comment_id = first_comment_id(thread_id=thread_id, runner=runner)
    if not comment_id:
        print(f"{thread_id}: no comment id found", file=sys.stderr)
        return 3
    owner, name = repo.split("/", 1)
    reply_result = runner(
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/pulls/{pr}/comments/{comment_id}/replies",
            "-f",
            f"body={reply}",
        ],
        None,
        30,
    )
    if reply_result.returncode != 0:
        print(reply_result.stderr or "failed to reply", file=sys.stderr)
        return reply_result.returncode
    mutation = (
        "mutation { resolveReviewThread(input:{threadId:\"%s\"}) { thread { isResolved } } }"
        % thread_id
    )
    resolved = runner(["gh", "api", "graphql", "-f", f"query={mutation}"], None, 30)
    if resolved.returncode != 0:
        print(resolved.stderr or "failed to resolve thread", file=sys.stderr)
        return resolved.returncode
    print(f"resolved {thread_id}")
    return 0


def first_comment_id(*, thread_id: str, runner: Runner = run_command) -> int | None:
    query = (
        "query { node(id:\"%s\") { ... on PullRequestReviewThread { "
        "comments(first:1) { nodes { databaseId } } } } }"
        % thread_id
    )
    result = runner(["gh", "api", "graphql", "-f", f"query={query}"], None, 30)
    if result.returncode != 0:
        return None
    nodes = _json(result.stdout, {}).get("data", {}).get("node", {}).get("comments", {}).get("nodes", [])
    if not nodes:
        return None
    return int(nodes[0]["databaseId"])


def guarded_merge(
    *,
    repo: str,
    pr: int,
    facts_path: Path,
    alert_command: Path,
    alert_destination_alias: str | None = None,
    ledger_path: Path | None = None,
    allow_unresolved: bool = False,
    fix_forward: bool = False,
    summary: str | None = None,
    expected_head: str | None = None,
    required_checks: tuple[str, ...] = (),
    preserve_head_branch: bool = False,
    readback_attempts: int = 3,
    readback_poll_interval: float = 1.0,
    lock_dir: Path = DEFAULT_PROVIDER_REVIEW_LOCK_DIR,
    runner: Runner = run_command,
) -> int:
    """Serialize tests-only merge decisions for one pull request."""
    try:
        with review_request_lock(repo=repo, pr=pr, lock_dir=lock_dir):
            return _guarded_merge_locked(
                repo=repo,
                pr=pr,
                facts_path=facts_path,
                alert_command=alert_command,
                alert_destination_alias=alert_destination_alias,
                ledger_path=ledger_path,
                allow_unresolved=allow_unresolved,
                fix_forward=fix_forward,
                summary=summary,
                expected_head=expected_head,
                required_checks=required_checks,
                preserve_head_branch=preserve_head_branch,
                readback_attempts=readback_attempts,
                readback_poll_interval=readback_poll_interval,
                runner=runner,
            )
    except OSError as exc:
        print(f"Cannot lock PR merge state: {exc}", file=sys.stderr)
        return 3


def _guarded_merge_locked(
    *,
    repo: str,
    pr: int,
    facts_path: Path,
    alert_command: Path,
    alert_destination_alias: str | None = None,
    ledger_path: Path | None = None,
    allow_unresolved: bool = False,
    fix_forward: bool = False,
    summary: str | None = None,
    expected_head: str | None = None,
    required_checks: tuple[str, ...] = (),
    preserve_head_branch: bool = False,
    readback_attempts: int = 3,
    readback_poll_interval: float = 1.0,
    runner: Runner = run_command,
) -> int:
    """Merge green PRs or arm protected auto-merge, without any review gate."""
    before: dict[str, Any] = {}
    def stopped(detail: str, code: int = 4) -> int:
        print(f"{repo}#{pr}: {detail}", file=sys.stderr)
        if ledger_path:
            append_ledger(ledger_path, {"tool": "pr-shepherd merge", "repo": repo,
                "pr": pr, "status": "blocked", "detail": detail,
                "before": before, "command": [], "exit_code": None})
        return code
    try:
        before = read_pr(repo=repo, pr=pr, runner=runner)
        entry = cached_or_refreshed_facts(repo=repo, repo_name=repo.split("/", 1)[1],
                                        facts_path=facts_path, runner=runner)
    except (RuntimeError, OSError, ValueError) as exc:
        return stopped(f"cannot establish current PR/check protection: {exc}")
    protected = bool(entry.get("protected") and entry.get("required_checks"))
    if not protected and (
        entry.get("visibility") != "PRIVATE" or expected_head is None
        or not isinstance(required_checks, tuple) or not required_checks
        or any(not isinstance(name, str) or not name or name.strip() != name
               for name in required_checks)
        or len(set(required_checks)) != len(required_checks)
    ):
        return stopped("required-check protection unavailable; private guarded merge requires "
                       "--expected-head and explicit --required-check names", 3)
    head, base, base_ref = before.get("headRefOid"), before.get("baseRefOid"), before.get("baseRefName")
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None for value in (head, base)) or not _valid_receipt_base_ref(base_ref):
        return stopped("exact PR head/base identity is unavailable")
    if expected_head is not None and head != expected_head:
        return stopped("PR head differs from explicit delivery head")
    if before.get("state") != "OPEN" or before.get("isDraft") is not False:
        return stopped("only open, ready PRs can merge")
    def check_state() -> str:
        if protected:
            return _required_check_state(repo=repo, pr=pr, runner=runner)
        return _unprotected_check_state(
            repo=repo, pr=pr, expected=before, required_checks=required_checks, runner=runner)

    checks = check_state()
    if checks not in {"green", "pending"}:
        return stopped("required tests are failing, missing, skipped, or unavailable")
    try:
        final = read_pr(repo=repo, pr=pr, runner=runner)
    except RuntimeError as exc:
        return stopped(f"final PR identity read failed: {exc}")
    if any(final.get(key) != before.get(key) for key in ("headRefOid", "baseRefOid", "baseRefName", "state", "isDraft")):
        return stopped("PR head, base, or readiness changed; rerun checks for the new state")
    # A second live check read prevents using an earlier green result after reruns.
    final_checks = check_state()
    if final_checks not in {"green", "pending"}:
        return stopped("required tests changed or failed before merge")
    try:
        immediate = read_pr(repo=repo, pr=pr, runner=runner)
    except RuntimeError as exc:
        return stopped(f"immediate PR identity read failed: {exc}")
    if any(immediate.get(key) != before.get(key) for key in ("headRefOid", "baseRefOid", "baseRefName", "state", "isDraft")):
        return stopped("PR head or base changed immediately before merge")
    if final_checks == "green":
        code, detail, command, merged = _direct_squash_merge_reviewed_pr(
            repo=repo, pr=pr, expected_head=head, expected_base=base, runner=runner)
        status = "applied" if code == 0 else "verification-failed" if merged else "failed"
    else:
        if not protected:
            return stopped("private checks are pending; keep monitoring and rerun with the "
                           "same expected head/check names when all checks pass; "
                           "unprotected auto-merge is never armed", 2)
        runner_state = entry.get("runner") or {}
        if (entry.get("visibility") == "PRIVATE"
                and runner_state.get("scope") in {"org-pool", "repo"}
                and not runner_state.get("online")):
            target = describe_runner_target(runner_state, repo_name=repo.split("/", 1)[1])
            send_alert(alert_command=alert_command, destination_alias=alert_destination_alias,
                       subject=f"{target} offline; PR #{pr} parked",
                       body=f"{repo} PR #{pr} has auto-merge guarded by checks, but {target} is offline.")
        # GitHub enforces the verified required checks on future pushed heads too.
        command = ["gh", "pr", "merge", str(pr), "--auto", "--squash",
                   "--match-head-commit", head, "--repo", repo]
        result = runner(command, None, 60)
        code, detail = result.returncode, result.stderr or result.stdout
        status = "armed" if code == 0 else "failed"
        if code != 0 and any(term in detail.casefold() for term in (
            "enablepullrequestautomerge", "auto merge is not allowed", "auto-merge is not allowed",
            "auto-merge is disabled", "auto merge is disabled", "auto-merge is not enabled",
        )):
            status, code = "needs-monitor", 2
            detail += (f"\nProtected auto-merge unavailable. Keep this task running: "
                       f"gh pr checks {pr} --repo {repo} --required --watch --interval 10; "
                       "then rerun pr-shepherd merge for fresh head and check validation.")
    if detail:
        print(detail, file=sys.stdout if code == 0 else sys.stderr)
    if ledger_path:
        append_ledger(ledger_path, {"tool": "pr-shepherd merge", "repo": repo,
            "pr": pr, "status": status, "detail": detail, "before": before,
            "after": immediate, "command": command, "exit_code": code,
            "policy": MERGE_POLICY, "protected": protected,
            "expected_checks": list(required_checks)})
    return code


def _direct_squash_merge_reviewed_pr(
    *,
    repo: str,
    pr: int,
    expected_head: str,
    expected_base: str,
    runner: Runner = run_command,
) -> tuple[int, str, list[str], bool]:
    """Merge one tested PR now, then verify the exact merge result.

    GitHub can atomically precondition the PR head but not its base. The caller
    therefore performs the exact base read immediately before this function;
    the merge-commit parent readback detects any base race. Reviewed head refs
    are deliberately preserved because GitHub has no compare-and-delete ref API;
    separate proof-based cleanup avoids deleting a concurrently advanced branch.
    """
    owner, name = repo.split("/", 1)
    command = [
        "gh",
        "api",
        "--method",
        "PUT",
        f"repos/{owner}/{name}/pulls/{pr}/merge",
        "-f",
        f"sha={expected_head}",
        "-f",
        "merge_method=squash",
    ]
    result = runner(command, None, 60)
    if result.returncode != 0:
        return (
            result.returncode,
            result.stderr or result.stdout or "GitHub direct merge failed",
            command,
            False,
        )
    payload = _json(result.stdout, None)
    merge_sha = payload.get("sha") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("merged") is not True
        or not isinstance(merge_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", merge_sha) is None
    ):
        return 4, "direct merge returned malformed success data", command, True

    commit = runner(
        ["gh", "api", f"repos/{owner}/{name}/git/commits/{merge_sha}"],
        None,
        30,
    )
    commit_payload = _json(commit.stdout, None) if commit.returncode == 0 else None
    parents = commit_payload.get("parents") if isinstance(commit_payload, dict) else None
    if (
        not isinstance(parents, list)
        or len(parents) != 1
        or not isinstance(parents[0], dict)
        or parents[0].get("sha") != expected_base
    ):
        return (
            4,
            "merge commit parent does not match the exact tested base; "
            "head branch preserved",
            command,
            True,
        )

    pr_readback = runner(
        ["gh", "api", f"repos/{owner}/{name}/pulls/{pr}"],
        None,
        30,
    )
    merged_pr = _json(pr_readback.stdout, None) if pr_readback.returncode == 0 else None
    head = merged_pr.get("head") if isinstance(merged_pr, dict) else None
    if (
        not isinstance(merged_pr, dict)
        or merged_pr.get("merged") is not True
        or not isinstance(head, dict)
        or head.get("sha") != expected_head
    ):
        return 4, "merged PR readback was incomplete; head branch preserved", command, True
    return 0, f"merged {merge_sha}; head branch preserved for proof-based cleanup", command, True


def describe_runner_target(runner_state: dict[str, Any], *, repo_name: str) -> str:
    if runner_state.get("scope") == "org-pool":
        org = runner_state.get("org") or DEFAULT_OWNER
        total = runner_state.get("total_count")
        pool = f"{org} runner pool"
        return f"{pool} (0 of {total} online)" if total else pool
    return runner_state.get("expected_name") or f"m1-air-{repo_name}"


def cached_or_refreshed_facts(
    *,
    repo: str,
    repo_name: str,
    facts_path: Path,
    runner: Runner = run_command,
) -> dict[str, Any]:
    owner = repo.split("/", 1)[0]
    cache = load_cache(facts_path)
    entry = cache.get("repos", {}).get(repo_name)
    same_owner = (entry and entry.get("full_name") == repo
                  and cache.get("owner") in (None, owner))
    fresh_protection = same_owner and fresh_enough(entry.get("checked_at"), max_age=PROTECTION_MAX_AGE)
    fresh_runner = same_owner and fresh_enough(entry.get("checked_at"), max_age=RUNNER_MAX_AGE)
    if fresh_protection and fresh_runner:
        return entry
    refreshed = collect_one(owner=owner, name=repo_name, runner=runner)
    cache["schema_version"] = SCHEMA_VERSION
    cache["owner"] = owner
    cache["generated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cache.setdefault("repos", {})[repo_name] = refreshed
    write_cache(cache, facts_path)
    return refreshed


def read_pr(*, repo: str, pr: int, runner: Runner = run_command) -> dict[str, Any]:
    result = runner(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "number,baseRefName,baseRefOid,headRefOid,url,title,state,isDraft",
        ],
        None,
        30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "gh pr view failed")
    return _json(result.stdout, {})


def stalled_auto_merges(
    *,
    owner: str,
    facts_path: Path,
    min_age: timedelta,
    runner: Runner = run_command,
) -> list[dict[str, Any]]:
    cache = load_cache(facts_path)
    repos = cache.get("repos", {})
    now = datetime.now(timezone.utc)
    stalls: list[dict[str, Any]] = []
    for name, entry in sorted(repos.items()):
        repo = entry.get("full_name") or f"{owner}/{name}"
        result = runner(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(OPEN_PR_SCAN_LIMIT + 1),
                "--json",
                "number,title,url,autoMergeRequest",
            ],
            None,
            45,
        )
        if result.returncode != 0:
            stalls.append(
                _sweep_error(repo, "stalls_api_failed", result.stderr or "gh pr list failed")
            )
            continue
        try:
            open_prs = _strict_sweep_list(result.stdout)
        except RuntimeError as exc:
            stalls.append(_sweep_error(repo, "stalls_output_invalid", str(exc)))
            continue
        if len(open_prs) > OPEN_PR_SCAN_LIMIT:
            stalls.append(
                _sweep_error(
                    repo,
                    "stalls_scan_saturated",
                    f"more than {OPEN_PR_SCAN_LIMIT} open PRs require a wider scan",
                )
            )
            continue
        for pr in open_prs:
            if not _complete_open_pr(pr):
                stalls.append(
                    _sweep_error(repo, "stalls_output_incomplete", "open PR record incomplete")
                )
                break
            if pr["autoMergeRequest"] is None:
                continue
            try:
                required_checks = _required_checks(entry)
                snapshot = _current_pr_check_evidence(
                    repo=repo,
                    pr=pr["number"],
                    runner=runner,
                )
                state = _stall_state(
                    snapshot,
                    required_checks=required_checks,
                    now=now,
                )
            except _SweepEvidenceError as exc:
                stalls.append(
                    {
                        **_sweep_error(repo, exc.failure_code, str(exc)),
                        "number": pr["number"],
                    }
                )
                continue
            if state is None:
                continue
            age = now - state["blocking_since"]
            if age >= min_age:
                stalls.append(
                    {
                        "repo": repo,
                        "number": pr.get("number"),
                        "title": pr.get("title"),
                        "url": pr.get("url"),
                        "age_hours": round(age.total_seconds() / 3600, 2),
                        "check_status": state["check_status"],
                        "age_source": state["age_source"],
                        "head_sha": state["head_sha"],
                        "blocking_checks": state["blocking_checks"],
                    }
                )
    return stalls


class _SweepEvidenceError(RuntimeError):
    def __init__(self, failure_code: str, detail: str) -> None:
        super().__init__(detail)
        self.failure_code = failure_code


def _required_checks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    value = entry.get("required_checks", [])
    if not isinstance(value, list):
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            "required-check inventory is malformed",
        )
    if not value:
        return []
    if all(isinstance(item, str) for item in value):
        if any(not item or item.strip() != item for item in value) or len(set(value)) != len(
            value
        ):
            raise _SweepEvidenceError(
                "stalls_output_incomplete",
                "required-check inventory is malformed",
            )
        return [{"context": item, "app_id": None} for item in sorted(value)]
    if not all(isinstance(item, dict) for item in value):
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            "required-check inventory mixes incompatible schemas",
        )
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    for item in value:
        if set(item) != {"context", "app_id"}:
            raise _SweepEvidenceError(
                "stalls_output_incomplete",
                "required-check inventory is malformed",
            )
        context = item["context"]
        app_id = item["app_id"]
        if (
            not isinstance(context, str)
            or not context
            or context.strip() != context
            or (
                app_id is not None
                and (
                    not isinstance(app_id, int)
                    or isinstance(app_id, bool)
                    or app_id <= 0
                )
            )
            or (context, app_id) in seen
        ):
            raise _SweepEvidenceError(
                "stalls_output_incomplete",
                "required-check inventory is malformed or duplicated",
            )
        seen.add((context, app_id))
        normalized.append({"context": context, "app_id": app_id})
    return sorted(
        normalized,
        key=lambda item: (item["context"], -1 if item["app_id"] is None else item["app_id"]),
    )


def _current_pr_check_evidence(
    *,
    repo: str,
    pr: int,
    runner: Runner,
) -> dict[str, Any]:
    owner, name = repo.split("/", 1)
    result = runner(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={STALL_EVIDENCE_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr}",
        ],
        None,
        30,
    )
    if result.returncode != 0:
        raise _SweepEvidenceError(
            "stalls_check_evidence_api_failed",
            result.stderr or "current-head check evidence query failed",
        )
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            "current-head check evidence is malformed JSON",
        ) from exc
    if not isinstance(payload, dict) or payload.get("errors"):
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            "current-head check evidence returned errors",
        )
    data = payload.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(pull_request, dict):
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            "current-head pull request evidence is missing",
        )
    if pull_request.get("number") != pr or isinstance(pull_request.get("number"), bool):
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            "current-head pull request number does not match",
        )
    head_sha = pull_request.get("headRefOid")
    if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
        raise _SweepEvidenceError("stalls_output_incomplete", "headRefOid is invalid")
    auto_merge = pull_request.get("autoMergeRequest")
    if auto_merge is None:
        return {"disarmed": True, "headRefOid": head_sha}
    if (
        not isinstance(auto_merge, dict)
        or not isinstance(auto_merge.get("enabledAt"), str)
        or not auto_merge["enabledAt"]
    ):
        raise _SweepEvidenceError("stalls_output_incomplete", "enabledAt is invalid")
    commits = pull_request.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    if (
        not isinstance(commits, dict)
        or not isinstance(commits.get("totalCount"), int)
        or isinstance(commits.get("totalCount"), bool)
        or commits["totalCount"] < 1
        or not isinstance(nodes, list)
        or len(nodes) != 1
        or not isinstance(nodes[0], dict)
        or not isinstance(nodes[0].get("commit"), dict)
    ):
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            "current-head commit evidence is incomplete",
        )
    commit = nodes[0]["commit"]
    if commit.get("oid") != head_sha:
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            "current-head commit oid does not match headRefOid",
        )
    if not isinstance(commit.get("committedDate"), str) or (
        commit.get("pushedDate") is not None
        and not isinstance(commit.get("pushedDate"), str)
    ):
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            "current-head commit timestamps are incomplete",
        )
    contexts: list[dict[str, Any]] = []
    suites = commit.get("checkSuites")
    if suites is not None:
        page_info = suites.get("pageInfo") if isinstance(suites, dict) else None
        raw_suites = suites.get("nodes") if isinstance(suites, dict) else None
        if (
            not isinstance(page_info, dict)
            or not isinstance(page_info.get("hasNextPage"), bool)
            or not isinstance(raw_suites, list)
            or len(raw_suites) > MAX_CHECK_SUITES
        ):
            raise _SweepEvidenceError(
                "stalls_output_incomplete",
                "current-head check suites are incomplete",
            )
        if page_info["hasNextPage"]:
            raise _SweepEvidenceError(
                "stalls_check_contexts_saturated",
                "current-head check suites exceed the bounded page",
            )
        for suite in raw_suites:
            app = suite.get("app") if isinstance(suite, dict) else None
            app_id = app.get("databaseId") if isinstance(app, dict) else None
            if (
                not isinstance(suite, dict)
                or not isinstance(suite.get("createdAt"), str)
                or not isinstance(app_id, int)
                or isinstance(app_id, bool)
                or app_id <= 0
            ):
                raise _SweepEvidenceError(
                    "stalls_output_incomplete",
                    "current-head check suite is incomplete",
                )
            runs = suite.get("checkRuns")
            run_page = runs.get("pageInfo") if isinstance(runs, dict) else None
            raw_runs = runs.get("nodes") if isinstance(runs, dict) else None
            if (
                not isinstance(run_page, dict)
                or not isinstance(run_page.get("hasNextPage"), bool)
                or not isinstance(raw_runs, list)
                or len(raw_runs) > MAX_CHECK_CONTEXTS
                or any(not _complete_check_context(item) for item in raw_runs)
                or any(item["checkSuite"]["createdAt"] != suite["createdAt"] for item in raw_runs)
                or len({_context_name(item) for item in raw_runs}) != len(raw_runs)
            ):
                raise _SweepEvidenceError(
                    "stalls_output_incomplete",
                    "latest current-head check runs are incomplete or ambiguous",
                )
            if run_page["hasNextPage"]:
                raise _SweepEvidenceError(
                    "stalls_check_contexts_saturated",
                    "latest current-head check runs exceed the bounded page",
                )
            contexts.extend({**item, "_app_id": app_id} for item in raw_runs)
            if len(contexts) > MAX_CHECK_CONTEXTS:
                raise _SweepEvidenceError(
                    "stalls_check_contexts_saturated",
                    "current-head check contexts exceed the bounded inventory",
                )
    status = commit.get("status")
    if status is not None:
        raw_statuses = status.get("contexts") if isinstance(status, dict) else None
        if (
            not isinstance(raw_statuses, list)
            or len(raw_statuses) > MAX_CHECK_CONTEXTS
            or any(not _complete_check_context(item) for item in raw_statuses)
        ):
            raise _SweepEvidenceError(
                "stalls_output_incomplete",
                "current-head status contexts are incomplete",
            )
        contexts.extend(raw_statuses)
        if len(contexts) > MAX_CHECK_CONTEXTS:
            raise _SweepEvidenceError(
                "stalls_check_contexts_saturated",
                "current-head check contexts exceed the bounded inventory",
            )
    return {
        "disarmed": False,
        "headRefOid": head_sha,
        "enabledAt": auto_merge["enabledAt"],
        "committedDate": commit["committedDate"],
        "pushedDate": commit.get("pushedDate"),
        "contexts": contexts,
    }


def _complete_check_context(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    kind = value.get("__typename")
    if kind == "CheckRun":
        suite = value.get("checkSuite")
        return bool(
            isinstance(value.get("name"), str)
            and value["name"]
            and isinstance(value.get("status"), str)
            and value["status"]
            and (value.get("conclusion") is None or isinstance(value.get("conclusion"), str))
            and (value.get("startedAt") is None or isinstance(value.get("startedAt"), str))
            and (value.get("completedAt") is None or isinstance(value.get("completedAt"), str))
            and isinstance(suite, dict)
            and isinstance(suite.get("createdAt"), str)
            and suite["createdAt"]
        )
    if kind == "StatusContext":
        return bool(
            isinstance(value.get("context"), str)
            and value["context"]
            and isinstance(value.get("state"), str)
            and value["state"]
            and isinstance(value.get("createdAt"), str)
            and value["createdAt"]
        )
    return False


def _stall_state(
    snapshot: dict[str, Any],
    *,
    required_checks: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    if snapshot.get("disarmed"):
        return None
    enabled_at = _evidence_timestamp(snapshot.get("enabledAt"), "enabledAt", now=now)
    committed_at = _evidence_timestamp(
        snapshot.get("committedDate"),
        "committedDate",
        now=now,
    )
    pushed_raw = snapshot.get("pushedDate")
    pushed_at = (
        _evidence_timestamp(pushed_raw, "pushedDate", now=now)
        if pushed_raw is not None
        else None
    )
    head_at = pushed_at or committed_at
    contexts = snapshot.get("contexts")
    if not isinstance(contexts, list):
        raise _SweepEvidenceError("stalls_output_incomplete", "check contexts are invalid")
    by_name: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for context in contexts:
        anchor = _context_anchor(context, now=now)
        key = _context_name(context)
        by_name.setdefault(key, []).append((anchor, context))

    blockers: list[tuple[str, datetime, str]] = []
    status_inputs: list[dict[str, Any]] = []
    if required_checks:
        missing_required = False
        for requirement in required_checks:
            label = _required_check_label(requirement)
            current = _latest_required_context(
                by_name.get(requirement["context"], []),
                requirement=requirement,
            )
            if current is None:
                missing_required = True
                since = max(enabled_at, head_at)
                blockers.append(
                    (label, since, "head" if head_at >= enabled_at else "auto_merge")
                )
                continue
            context = current[1]
            status_inputs.append(context)
            if _context_success(context):
                continue
            evidence_at = _context_blocking_since(context, now=now)
            since = max(enabled_at, evidence_at)
            blockers.append(
                (label, since, "check" if evidence_at >= enabled_at else "auto_merge")
            )
        if not blockers:
            return None
        check_status = "pending" if missing_required else classify_check_rollup(status_inputs)
    else:
        latest = {
            name: _latest_required_context(
                candidates,
                requirement={"context": name, "app_id": None},
            )
            for name, candidates in by_name.items()
        }
        current_contexts = [value[1] for value in latest.values() if value is not None]
        if current_contexts and classify_check_rollup(current_contexts) == "success":
            return None
        for name, (_anchor, context) in latest.items():
            if _context_success(context):
                continue
            evidence_at = _context_blocking_since(context, now=now)
            since = max(enabled_at, evidence_at)
            blockers.append(
                (name, since, "check" if evidence_at >= enabled_at else "auto_merge")
            )
        if not blockers:
            blockers.append(("required-checks-unavailable", enabled_at, "auto_merge"))
        check_status = classify_check_rollup(current_contexts) if current_contexts else "unknown"

    blocking_since = min(item[1] for item in blockers)
    oldest_sources = sorted({item[2] for item in blockers if item[1] == blocking_since})
    return {
        "blocking_since": blocking_since,
        "check_status": check_status,
        "age_source": "+".join(oldest_sources),
        "head_sha": snapshot["headRefOid"],
        "blocking_checks": sorted(item[0] for item in blockers),
    }


def _required_check_label(requirement: dict[str, Any]) -> str:
    context = str(requirement["context"])
    app_id = requirement["app_id"]
    return context if app_id is None else f"{context}@app:{app_id}"


def _latest_required_context(
    candidates: list[tuple[datetime, dict[str, Any]]],
    *,
    requirement: dict[str, Any],
) -> tuple[datetime, dict[str, Any]] | None:
    app_id = requirement["app_id"]
    matching = [
        candidate
        for candidate in candidates
        if app_id is None
        or (
            candidate[1].get("__typename") == "CheckRun"
            and candidate[1].get("_app_id") == app_id
        )
    ]
    if not matching:
        return None
    newest = max(candidate[0] for candidate in matching)
    latest = [candidate for candidate in matching if candidate[0] == newest]
    if len(latest) != 1:
        raise _SweepEvidenceError(
            "stalls_output_incomplete",
            f"latest evidence is ambiguous for {_required_check_label(requirement)}",
        )
    return latest[0]


def _context_name(context: dict[str, Any]) -> str:
    return str(context["name"] if context["__typename"] == "CheckRun" else context["context"])


def _context_anchor(context: dict[str, Any], *, now: datetime) -> datetime:
    if context["__typename"] == "CheckRun":
        for field in ("startedAt", "completedAt"):
            if context.get(field) is not None:
                _evidence_timestamp(context[field], field, now=now)
        return _evidence_timestamp(context["checkSuite"]["createdAt"], "checkSuite.createdAt", now=now)
    return _evidence_timestamp(context["createdAt"], "StatusContext.createdAt", now=now)


def _context_success(context: dict[str, Any]) -> bool:
    if context["__typename"] == "CheckRun":
        return str(context.get("conclusion") or "").upper() in {"SUCCESS", "SKIPPED", "NEUTRAL"}
    return str(context.get("state") or "").upper() == "SUCCESS"


def _context_blocking_since(context: dict[str, Any], *, now: datetime) -> datetime:
    if context["__typename"] == "StatusContext":
        return _evidence_timestamp(context["createdAt"], "StatusContext.createdAt", now=now)
    status = str(context.get("status") or "").upper()
    if status == "IN_PROGRESS" and context.get("startedAt") is not None:
        return _evidence_timestamp(context["startedAt"], "startedAt", now=now)
    if status == "COMPLETED":
        for field in ("completedAt", "startedAt"):
            if context.get(field) is not None:
                return _evidence_timestamp(context[field], field, now=now)
    return _evidence_timestamp(context["checkSuite"]["createdAt"], "checkSuite.createdAt", now=now)


def _evidence_timestamp(value: Any, field: str, *, now: datetime) -> datetime:
    if not isinstance(value, str):
        raise _SweepEvidenceError("stalls_output_incomplete", f"{field} is invalid")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _SweepEvidenceError("stalls_output_incomplete", f"{field} is invalid") from exc
    if (
        timestamp.tzinfo is None
        or timestamp.utcoffset() != timedelta(0)
        or timestamp > now + SWEEP_CLOCK_SKEW
    ):
        raise _SweepEvidenceError("stalls_output_incomplete", f"{field} is invalid")
    return timestamp


def _strict_sweep_list(stdout: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("sweep returned malformed JSON") from exc
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise RuntimeError("sweep returned a non-list or non-object record")
    return payload


def _complete_open_pr(pr: dict[str, Any]) -> bool:
    required = {
        "number",
        "title",
        "url",
        "autoMergeRequest",
    }
    return (
        required.issubset(pr)
        and isinstance(pr["number"], int)
        and not isinstance(pr["number"], bool)
        and isinstance(pr["title"], str)
        and isinstance(pr["url"], str)
        and (
            pr["autoMergeRequest"] is None
            or (
                isinstance(pr["autoMergeRequest"], dict)
                and isinstance(pr["autoMergeRequest"].get("enabledAt"), str)
                and bool(pr["autoMergeRequest"]["enabledAt"])
            )
        )
    )


def _complete_merged_pr(pr: dict[str, Any]) -> bool:
    required = {"number", "title", "url", "mergedAt"}
    return (
        required.issubset(pr)
        and isinstance(pr["number"], int)
        and not isinstance(pr["number"], bool)
        and isinstance(pr["title"], str)
        and isinstance(pr["url"], str)
        and isinstance(pr["mergedAt"], str)
    )


def _strict_age(raw: str, *, now: datetime) -> timedelta:
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return now - timestamp


def _sweep_error(repo: str, failure_code: str, detail: str) -> dict[str, Any]:
    return {
        "repo": repo,
        "error": " ".join(str(detail).split())[:160],
        "failure_code": failure_code,
    }


def _sweep_failure_codes(items: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(item["failure_code"])
            for item in items
            if isinstance(item, dict) and item.get("failure_code")
        }
    )


def daily_watch(
    *,
    owner: str,
    facts_path: Path,
    min_age: timedelta,
    days: float,
    alert_command: Path,
    alert_destination_alias: str | None = None,
    require_alert_config: bool = False,
    receipt_path: Path | None = None,
    run_id: str | None = None,
    runtime_sha: str | None = None,
    runtime_manifest_sha256: str | None = None,
    destination_target_sha256: str | None = None,
    gh_executable_sha256: str | None = None,
    hermes_runtime_sha: str | None = None,
    hermes_executable_sha256: str | None = None,
    hermes_runtime_manifest_sha256: str | None = None,
    refresh_facts: bool = False,
    runner: Runner = run_command,
) -> int:
    """Run the stalls and orphans sweeps; alert on findings. 0 clean, 2 findings, 3 broken."""
    alert_configured = True
    failure_codes: list[str] = []
    if require_alert_config:
        alert_configured = _validate_alert_configuration(
            alert_command=alert_command,
            destination_alias=alert_destination_alias,
            cancelled_cleanup_callback=lambda failure_code: _commit_terminal_watch_receipt(
                failure_code=failure_code,
                receipt_path=receipt_path,
                refresh_status="not_requested",
                facts_cache="unreadable",
                stalls_count=0,
                orphans_count=0,
                alert_delivery="failed",
                destination_alias=alert_destination_alias,
                failure_codes=failure_codes,
                run_id=run_id,
                runtime_sha=runtime_sha,
                runtime_manifest_sha256=runtime_manifest_sha256,
                destination_target_sha256=destination_target_sha256,
                gh_executable_sha256=gh_executable_sha256,
                hermes_runtime_sha=hermes_runtime_sha,
                hermes_executable_sha256=hermes_executable_sha256,
                hermes_runtime_manifest_sha256=hermes_runtime_manifest_sha256,
            ),
        )
        if alert_configured is None:
            return 3
        if not alert_configured:
            failure_codes.append("alert_unconfigured")

    refresh_error: str | None = None
    refresh_status = "not_requested"
    if refresh_facts:
        try:
            write_cache(collect_all(owner=owner, runner=runner), facts_path)
            refresh_status = "succeeded"
        except (
            KeyError,
            OSError,
            RecursionError,
            RuntimeError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as exc:
            refresh_status = "failed"
            failure_codes.append("repo_facts_refresh_failed")
            refresh_error = f"repo-facts refresh failed, sweeping the existing cache: {exc}"
            print(refresh_error, file=sys.stderr)
    facts_read_error: str | None = None
    try:
        cache = load_cache(facts_path)
    except (KeyError, OSError, RecursionError, json.JSONDecodeError, UnicodeError) as exc:
        cache = {"repos": {}}
        failure_codes.append("repo_facts_read_failed")
        facts_read_error = f"repo-facts cache could not be read ({type(exc).__name__})"
        print(f"{facts_path}: {facts_read_error}", file=sys.stderr)
    if not _valid_watch_cache(cache):
        cache = {"repos": {}}
        failure_codes.append("repo_facts_structure_invalid")
        facts_read_error = "repo-facts cache structure is invalid"
        print(f"{facts_path}: {facts_read_error}", file=sys.stderr)
    if not cache.get("repos"):
        if facts_read_error:
            message = f"{facts_path}: {facts_read_error}; the watch checked nothing"
            facts_cache = "unreadable"
        else:
            failure_codes.append("repo_facts_empty")
            message = f"{facts_path}: repo-facts cache is empty, the watch checked nothing"
            facts_cache = "empty"
            print(message, file=sys.stderr)
        alert_delivery = _watch_alert_delivery(
            configured=alert_configured,
            alert_command=alert_command,
            destination_alias=alert_destination_alias,
            subject="pr-shepherd watch: repo-facts cache empty",
            body=message,
            cancelled_cleanup_callback=lambda failure_code: _commit_terminal_watch_receipt(
                failure_code=failure_code,
                receipt_path=receipt_path,
                refresh_status=refresh_status,
                facts_cache=facts_cache,
                stalls_count=0,
                orphans_count=0,
                alert_delivery="failed",
                destination_alias=alert_destination_alias,
                failure_codes=failure_codes,
                run_id=run_id,
                runtime_sha=runtime_sha,
                runtime_manifest_sha256=runtime_manifest_sha256,
                destination_target_sha256=destination_target_sha256,
                gh_executable_sha256=gh_executable_sha256,
                hermes_runtime_sha=hermes_runtime_sha,
                hermes_executable_sha256=hermes_executable_sha256,
                hermes_runtime_manifest_sha256=hermes_runtime_manifest_sha256,
            ),
        )
        if alert_delivery == "terminal":
            return 3
        if alert_delivery == "failed":
            failure_codes.append("alert_delivery_failed")
        return _finish_watch(
            receipt_path=receipt_path,
            outcome="broken",
            exit_code=3,
            refresh_status=refresh_status,
            facts_cache=facts_cache,
            stalls_count=0,
            orphans_count=0,
            alert_delivery=alert_delivery,
            destination_alias=alert_destination_alias,
            failure_codes=failure_codes,
            run_id=run_id,
            runtime_sha=runtime_sha,
            runtime_manifest_sha256=runtime_manifest_sha256,
            destination_target_sha256=destination_target_sha256,
            gh_executable_sha256=gh_executable_sha256,
            hermes_runtime_sha=hermes_runtime_sha,
            hermes_executable_sha256=hermes_executable_sha256,
            hermes_runtime_manifest_sha256=hermes_runtime_manifest_sha256,
        )
    try:
        stalls = stalled_auto_merges(
            owner=owner,
            facts_path=facts_path,
            min_age=min_age,
            runner=runner,
        )
    except (
        KeyError,
        OSError,
        RecursionError,
        RuntimeError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        stalls = [_sweep_error(owner, "stalls_execution_failed", type(exc).__name__)]
    orphans: list[dict[str, Any]] = []
    failure_codes.extend(_sweep_failure_codes(stalls))
    failure_codes.extend(_sweep_failure_codes(orphans))
    stall_findings = [item for item in stalls if not item.get("failure_code")]
    orphan_findings = [item for item in orphans if not item.get("failure_code")]
    payload: dict[str, Any] = {"orphans": orphans, "stalls": stalls}
    if refresh_error:
        payload["refresh_error"] = refresh_error
    summary = json.dumps(payload, indent=2, sort_keys=True)
    print(summary)
    if stalls or orphans:
        subject = (
            f"pr-shepherd watch: {len(stall_findings)} stalled auto-merge PR(s), "
            "tests-only delivery"
        )
        alert_body = _watch_alert_body(
            stalls=stalls,
            orphans=orphans,
            refresh_error=refresh_error,
        )
    else:
        subject = "pr-shepherd watch: repo-facts refresh failed"
        alert_body = summary
    if stalls or orphans or refresh_error:
        alert_delivery = _watch_alert_delivery(
            configured=alert_configured,
            alert_command=alert_command,
            destination_alias=alert_destination_alias,
            subject=subject,
            body=alert_body,
            cancelled_cleanup_callback=lambda failure_code: _commit_terminal_watch_receipt(
                failure_code=failure_code,
                receipt_path=receipt_path,
                refresh_status=refresh_status,
                facts_cache="available",
                stalls_count=len(stall_findings),
                orphans_count=len(orphan_findings),
                alert_delivery="failed",
                destination_alias=alert_destination_alias,
                failure_codes=failure_codes,
                run_id=run_id,
                runtime_sha=runtime_sha,
                runtime_manifest_sha256=runtime_manifest_sha256,
                destination_target_sha256=destination_target_sha256,
                gh_executable_sha256=gh_executable_sha256,
                hermes_runtime_sha=hermes_runtime_sha,
                hermes_executable_sha256=hermes_executable_sha256,
                hermes_runtime_manifest_sha256=hermes_runtime_manifest_sha256,
            ),
        )
        if alert_delivery == "terminal":
            return 3
        if alert_delivery == "failed":
            failure_codes.append("alert_delivery_failed")
    else:
        alert_delivery = "not_needed" if alert_configured else "unconfigured"

    broken = bool(failure_codes)
    if broken:
        outcome = "broken"
        exit_code = 3
    elif stall_findings or orphan_findings:
        outcome = "findings"
        exit_code = 2
    else:
        outcome = "clean"
        exit_code = 0
    return _finish_watch(
        receipt_path=receipt_path,
        outcome=outcome,
        exit_code=exit_code,
        refresh_status=refresh_status,
        facts_cache="available",
        stalls_count=len(stall_findings),
        orphans_count=len(orphan_findings),
        alert_delivery=alert_delivery,
        destination_alias=alert_destination_alias,
        failure_codes=failure_codes,
        run_id=run_id,
        runtime_sha=runtime_sha,
        runtime_manifest_sha256=runtime_manifest_sha256,
        destination_target_sha256=destination_target_sha256,
        gh_executable_sha256=gh_executable_sha256,
        hermes_runtime_sha=hermes_runtime_sha,
        hermes_executable_sha256=hermes_executable_sha256,
        hermes_runtime_manifest_sha256=hermes_runtime_manifest_sha256,
    )


def _valid_watch_cache(cache: Any) -> bool:
    if not isinstance(cache, dict):
        return False
    repos = cache.get("repos")
    if not isinstance(repos, dict):
        return False
    for name, entry in repos.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            return False
        full_name = entry.get("full_name")
        if full_name is not None and not isinstance(full_name, str):
            return False
    return True


def _watch_alert_delivery(
    *,
    configured: bool,
    alert_command: Path,
    destination_alias: str | None,
    subject: str,
    body: str,
    cancelled_cleanup_callback: Callable[[str], None],
) -> str:
    if not configured:
        return "unconfigured"
    delivered = _deliver_alert(
        alert_command=alert_command,
        destination_alias=destination_alias,
        subject=subject,
        body=body,
        cancelled_cleanup_callback=cancelled_cleanup_callback,
    )
    if delivered is None:
        return "terminal"
    return "delivered" if delivered else "failed"


def _finish_watch(
    *,
    receipt_path: Path | None,
    outcome: str,
    exit_code: int,
    refresh_status: str,
    facts_cache: str,
    stalls_count: int,
    orphans_count: int,
    alert_delivery: str,
    destination_alias: str | None,
    failure_codes: list[str],
    run_id: str | None,
    runtime_sha: str | None,
    runtime_manifest_sha256: str | None,
    destination_target_sha256: str | None,
    gh_executable_sha256: str | None,
    hermes_runtime_sha: str | None,
    hermes_executable_sha256: str | None,
    hermes_runtime_manifest_sha256: str | None,
) -> int:
    if receipt_path is None:
        return exit_code
    record = {
        "schema": WATCH_RECEIPT_SCHEMA,
        "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": "failure" if outcome == "broken" else "success",
        "outcome": outcome,
        "exit_code": exit_code,
        "facts_refresh": refresh_status,
        "facts_cache": facts_cache,
        "stalls_count": stalls_count,
        "orphans_count": orphans_count,
        "alert_delivery": alert_delivery,
        "destination_alias": _bounded_destination_alias(destination_alias),
        "failure_codes": sorted(set(failure_codes)),
        "run_id": run_id,
        "runtime_sha": runtime_sha,
        "runtime_manifest_sha256": runtime_manifest_sha256,
        "destination_target_sha256": destination_target_sha256,
        "gh_executable_sha256": gh_executable_sha256,
        "hermes_runtime_sha": hermes_runtime_sha,
        "hermes_executable_sha256": hermes_executable_sha256,
        "hermes_runtime_manifest_sha256": hermes_runtime_manifest_sha256,
        "process_id": os.getpid(),
    }
    try:
        accepted = append_watch_receipt(receipt_path, record)
    except OSError as exc:
        print(
            f"{receipt_path}: receipt write failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 3
    if not accepted:
        print(
            f"{receipt_path}: receipt rejected a stale or unproven completion",
            file=sys.stderr,
        )
        return 3
    return exit_code


def _commit_terminal_watch_receipt(
    *,
    failure_code: str,
    receipt_path: Path | None,
    refresh_status: str,
    facts_cache: str,
    stalls_count: int,
    orphans_count: int,
    alert_delivery: str,
    destination_alias: str | None,
    failure_codes: list[str],
    run_id: str | None,
    runtime_sha: str | None,
    runtime_manifest_sha256: str | None,
    destination_target_sha256: str | None,
    gh_executable_sha256: str | None,
    hermes_runtime_sha: str | None,
    hermes_executable_sha256: str | None,
    hermes_runtime_manifest_sha256: str | None,
) -> None:
    _finish_watch(
        receipt_path=receipt_path,
        outcome="broken",
        exit_code=3,
        refresh_status=refresh_status,
        facts_cache=facts_cache,
        stalls_count=stalls_count,
        orphans_count=orphans_count,
        alert_delivery=alert_delivery,
        destination_alias=destination_alias,
        failure_codes=[*failure_codes, failure_code],
        run_id=run_id,
        runtime_sha=runtime_sha,
        runtime_manifest_sha256=runtime_manifest_sha256,
        destination_target_sha256=destination_target_sha256,
        gh_executable_sha256=gh_executable_sha256,
        hermes_runtime_sha=hermes_runtime_sha,
        hermes_executable_sha256=hermes_executable_sha256,
        hermes_runtime_manifest_sha256=hermes_runtime_manifest_sha256,
    )


def append_watch_receipt(path: Path, record: dict[str, Any]) -> bool:
    """Append a monotonic receipt through trusted dirfds with tail recovery."""
    if not _valid_watch_receipt_order_record(record):
        raise OSError("receipt does not satisfy the complete producer schema")
    absolute = _trusted_absolute_path(path)
    payload = _receipt_payload(record)
    if len(payload) > MAX_RECEIPT_BYTES:
        raise OSError("receipt exceeds byte limit")
    directory_fd = _open_private_receipt_directory(absolute.parent)
    flags = os.O_APPEND | os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    created = False
    try:
        try:
            fd = os.open(
                absolute.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            created = True
        except FileExistsError:
            fd = os.open(absolute.name, flags, dir_fd=directory_fd)
        try:
            if created:
                os.fchmod(fd, 0o600)
            _validate_private_receipt_file(fd)
            deadline = time.monotonic() + RECEIPT_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise OSError("receipt lock timed out") from exc
                    time.sleep(0.01)
            _validate_private_receipt_file(fd)
            file_size = os.fstat(fd).st_size
            if file_size == 0:
                # A zero-length file is always uninitialized: it may be the
                # remnant of a prior failed name commit. Repeat the complete
                # empty-file transaction on every retry before a qualifying
                # completion can become readable.
                os.fsync(fd)
                _verify_named_receipt_identity(
                    absolute=absolute,
                    original_directory_fd=directory_fd,
                    original_file_fd=fd,
                )
                os.fsync(directory_fd)
                if os.fstat(fd).st_size != 0:
                    raise OSError("receipt changed during durable initialization")
            previous, candidate, incomplete_tail = _read_receipt_tail(fd, file_size)
            selected = record
            accepted = True
            if incomplete_tail:
                selected, accepted = _select_after_incomplete_tail(
                    previous=previous,
                    candidate=candidate,
                    incoming=record,
                )
            elif previous is not None and not _receipt_supersedes(previous, record):
                if previous == record:
                    _verify_named_receipt_identity(
                        absolute=absolute,
                        original_directory_fd=directory_fd,
                        original_file_fd=fd,
                    )
                    return True
                if record.get("status") != "failure":
                    _verify_named_receipt_identity(
                        absolute=absolute,
                        original_directory_fd=directory_fd,
                        original_file_fd=fd,
                    )
                    return False
                previous_at = _watch_completed_at(previous.get("completed_at"))
                assert previous_at is not None
                selected = _receipt_clock_regression_failure(
                    record,
                    completed_at=previous_at,
                )
                accepted = True
            selected_payload = _receipt_payload(selected)
            if len(selected_payload) > MAX_RECEIPT_BYTES:
                raise OSError("receipt exceeds byte limit")
            if incomplete_tail:
                _append_receipt_bytes(fd, RECEIPT_RECOVERY_SEPARATOR)
            _append_receipt_bytes(fd, selected_payload)
            os.fsync(fd)
            _verify_named_receipt_identity(
                absolute=absolute,
                original_directory_fd=directory_fd,
                original_file_fd=fd,
            )
            return accepted
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _receipt_payload(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _append_receipt_bytes(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("receipt append made no progress")
        view = view[written:]


def _read_receipt_tail(
    fd: int,
    file_size: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
    if file_size == 0:
        return None, None, False
    tail_size = min(file_size, (MAX_RECEIPT_BYTES * 2) + 2)
    tail = os.pread(fd, tail_size, file_size - tail_size)
    if len(tail) != tail_size:
        raise OSError("receipt changed during bounded tail read")
    incomplete_tail = not tail.endswith(b"\n")
    complete = tail
    candidate: dict[str, Any] | None = None
    if incomplete_tail:
        final_boundary = tail.rfind(b"\n")
        candidate_bytes = tail[final_boundary + 1 :]
        if (final_boundary >= 0 or file_size == tail_size) and (
            0 < len(candidate_bytes) + 1 <= MAX_RECEIPT_BYTES
        ):
            candidate = _decode_receipt_record(candidate_bytes)
        complete = tail[: final_boundary + 1] if final_boundary >= 0 else b""
    if not complete:
        return None, candidate, incomplete_tail
    content = complete[:-1]
    record_boundary = content.rfind(b"\n")
    record_bytes = content[record_boundary + 1 :]
    if record_boundary < 0 and file_size > tail_size:
        return None, candidate, True
    decoded = (
        _decode_receipt_record(record_bytes)
        if record_bytes and len(record_bytes) + 1 <= MAX_RECEIPT_BYTES
        else None
    )
    if not _valid_watch_receipt_order_record(decoded):
        previous = _previous_complete_order_record(
            content=content,
            record_boundary=record_boundary,
            file_size=file_size,
            tail_size=tail_size,
        )
        return previous, decoded, True
    return decoded, candidate, incomplete_tail


def _previous_complete_order_record(
    *,
    content: bytes,
    record_boundary: int,
    file_size: int,
    tail_size: int,
) -> dict[str, Any] | None:
    if record_boundary < 0:
        return None
    preceding = content[:record_boundary]
    previous_boundary = preceding.rfind(b"\n")
    if previous_boundary < 0 and file_size > tail_size:
        return None
    record_bytes = preceding[previous_boundary + 1 :]
    if not record_bytes or len(record_bytes) + 1 > MAX_RECEIPT_BYTES:
        return None
    decoded = _decode_receipt_record(record_bytes)
    return decoded if _valid_watch_receipt_order_record(decoded) else None


def _decode_receipt_record(record_bytes: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(record_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _select_after_incomplete_tail(
    *,
    previous: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    valid_previous = previous if _valid_watch_receipt_order_record(previous) else None
    valid_candidate = candidate if _valid_watch_receipt_order_record(candidate) else None
    incoming_at = _watch_completed_at(incoming.get("completed_at"))
    incoming_valid = _valid_watch_receipt_order_record(incoming)

    if valid_candidate is None:
        if incoming_valid and incoming.get("status") == "failure":
            return incoming, True
        if valid_previous is None:
            marker_at = max(
                datetime.now(timezone.utc).replace(microsecond=0),
                _latest_receipt_time(incoming),
            )
        else:
            marker_at = _latest_receipt_time(valid_previous, incoming)
        return _receipt_recovery_failure(incoming, completed_at=marker_at), False

    if valid_candidate == incoming:
        return incoming, True
    latest = _latest_order_record(valid_previous, valid_candidate)
    latest_at = _watch_completed_at(latest.get("completed_at"))
    assert latest_at is not None
    if incoming_valid and incoming_at is not None:
        if incoming_at > latest_at:
            return incoming, True
        if incoming_at == latest_at and incoming.get("status") == "failure":
            return incoming, True
    if latest.get("status") == "failure":
        return latest, False
    return _receipt_recovery_failure(incoming, completed_at=latest_at), False


def _valid_watch_receipt_order_record(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict) or set(record) != {
        "schema",
        "completed_at",
        "status",
        "outcome",
        "exit_code",
        "facts_refresh",
        "facts_cache",
        "stalls_count",
        "orphans_count",
        "alert_delivery",
        "destination_alias",
        "destination_target_sha256",
        "failure_codes",
        "run_id",
        "runtime_sha",
        "runtime_manifest_sha256",
        "gh_executable_sha256",
        "hermes_runtime_sha",
        "hermes_executable_sha256",
        "hermes_runtime_manifest_sha256",
        "process_id",
    }:
        return False
    outcome = record.get("outcome")
    status = record.get("status")
    exit_code = record.get("exit_code")
    expected = {
        "clean": ("success", 0),
        "findings": ("success", 2),
        "broken": ("failure", 3),
    }
    if outcome not in expected or (status, exit_code) != expected[outcome]:
        return False
    counts = (record.get("stalls_count"), record.get("orphans_count"))
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        return False
    failure_codes = record.get("failure_codes")
    if (
        not isinstance(failure_codes, list)
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[a-z0-9_]{1,64}", value) is None
            for value in failure_codes
        )
        or failure_codes != sorted(set(failure_codes))
        or (outcome == "broken") != bool(failure_codes)
    ):
        return False
    destination_alias = record.get("destination_alias")
    identity_valid = bool(
        record.get("schema") == WATCH_RECEIPT_SCHEMA
        and _watch_completed_at(record.get("completed_at")) is not None
        and record.get("facts_refresh") in {"not_requested", "succeeded", "failed"}
        and record.get("facts_cache") in {"available", "empty", "unreadable"}
        and record.get("alert_delivery")
        in {"delivered", "failed", "not_needed", "unconfigured"}
        and isinstance(destination_alias, str)
        and (
            destination_alias == "unconfigured"
            or DESTINATION_ALIAS_RE.fullmatch(destination_alias) is not None
        )
        and WATCH_RUN_ID_RE.fullmatch(str(record.get("run_id", ""))) is not None
        and WATCH_RUNTIME_SHA_RE.fullmatch(str(record.get("runtime_sha", ""))) is not None
        and WATCH_MANIFEST_SHA_RE.fullmatch(
            str(record.get("runtime_manifest_sha256", ""))
        )
        is not None
        and WATCH_SHA256_RE.fullmatch(
            str(record.get("destination_target_sha256", ""))
        )
        is not None
        and WATCH_SHA256_RE.fullmatch(str(record.get("gh_executable_sha256", "")))
        is not None
        and WATCH_RUNTIME_SHA_RE.fullmatch(str(record.get("hermes_runtime_sha", "")))
        is not None
        and WATCH_SHA256_RE.fullmatch(
            str(record.get("hermes_executable_sha256", ""))
        )
        is not None
        and WATCH_SHA256_RE.fullmatch(
            str(record.get("hermes_runtime_manifest_sha256", ""))
        )
        is not None
        and isinstance(record.get("process_id"), int)
        and not isinstance(record.get("process_id"), bool)
        and record["process_id"] > 0
    )
    if not identity_valid:
        return False
    finding_count = record["stalls_count"] + record["orphans_count"]
    if outcome == "clean":
        return bool(
            finding_count == 0
            and record.get("facts_cache") == "available"
            and record.get("facts_refresh") != "failed"
            and record.get("alert_delivery") == "not_needed"
        )
    if outcome == "findings":
        return bool(
            finding_count > 0
            and record.get("facts_cache") == "available"
            and record.get("facts_refresh") != "failed"
            and record.get("alert_delivery") == "delivered"
        )
    return True


def _latest_order_record(
    first: dict[str, Any] | None,
    second: dict[str, Any],
) -> dict[str, Any]:
    if first is None:
        return second
    first_at = _watch_completed_at(first.get("completed_at"))
    second_at = _watch_completed_at(second.get("completed_at"))
    assert first_at is not None and second_at is not None
    if second_at > first_at:
        return second
    if second_at == first_at and second.get("status") == "failure":
        return second
    return first


def _latest_receipt_time(*records: dict[str, Any] | None) -> datetime:
    timestamps = [
        timestamp
        for record in records
        if record is not None
        if (timestamp := _watch_completed_at(record.get("completed_at"))) is not None
    ]
    if timestamps:
        return max(timestamps)
    return datetime.now(timezone.utc).replace(microsecond=0)


def _receipt_recovery_failure(
    incoming: dict[str, Any],
    *,
    completed_at: datetime,
) -> dict[str, Any]:
    marker = dict(incoming)
    failure_codes = marker.get("failure_codes")
    if not isinstance(failure_codes, list):
        failure_codes = []
    marker.update(
        {
            "schema": WATCH_RECEIPT_SCHEMA,
            "completed_at": completed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "failure",
            "outcome": "broken",
            "exit_code": 3,
            "failure_codes": sorted({*map(str, failure_codes), "receipt_tail_recovery_unproven"}),
        }
    )
    return marker


def _receipt_clock_regression_failure(
    incoming: dict[str, Any],
    *,
    completed_at: datetime,
) -> dict[str, Any]:
    marker = dict(incoming)
    failure_codes = marker.get("failure_codes")
    if not isinstance(failure_codes, list):
        failure_codes = []
    marker.update(
        {
            "completed_at": completed_at.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "status": "failure",
            "outcome": "broken",
            "exit_code": 3,
            "failure_codes": sorted({*map(str, failure_codes), "receipt_clock_regression"}),
        }
    )
    return marker


def _receipt_supersedes(previous: dict[str, Any], incoming: dict[str, Any]) -> bool:
    previous_at = _watch_completed_at(previous.get("completed_at"))
    incoming_at = _watch_completed_at(incoming.get("completed_at"))
    if previous_at is None or incoming_at is None:
        return True
    if incoming_at < previous_at:
        return False
    if incoming_at > previous_at:
        return True
    if previous == incoming:
        return False
    previous_failed = previous.get("status") == "failure"
    incoming_failed = incoming.get("status") == "failure"
    if previous_failed and not incoming_failed:
        return False
    return True


def _watch_completed_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or WATCH_COMPLETED_AT_RE.fullmatch(value) is None:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.utcoffset() != timedelta(0) or timestamp.microsecond:
        return None
    return timestamp


def _trusted_absolute_path(path: Path) -> Path:
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    if sys.platform == "darwin":
        if absolute == "/var" or absolute.startswith("/var/"):
            absolute = "/private" + absolute
        elif absolute == "/tmp" or absolute.startswith("/tmp/"):
            absolute = "/private" + absolute
    result = Path(absolute)
    if not result.name or result.name in {".", ".."}:
        raise OSError("receipt path must name a file")
    return result


def _open_private_receipt_directory(path: Path) -> int:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current_fd = os.open("/", directory_flags)
    try:
        parts = path.parts[1:]
        if not parts:
            raise OSError("receipt directory cannot be filesystem root")
        for index, component in enumerate(parts):
            if component in {"", ".", ".."}:
                raise OSError("invalid receipt directory component")
            created = False
            try:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    created = True
                except FileExistsError:
                    pass
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            try:
                if created:
                    os.fchmod(next_fd, 0o700)
                _validate_trusted_directory(
                    os.fstat(next_fd),
                    final=index == len(parts) - 1,
                )
                if created:
                    os.fsync(next_fd)
                    os.fsync(current_fd)
            except Exception:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _validate_trusted_directory(info: os.stat_result, *, final: bool) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("receipt ancestor must be a directory")
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid not in {0, os.geteuid()}:
        raise OSError("receipt ancestor has an untrusted owner")
    if mode & 0o022 and not (info.st_uid == 0 and mode & stat.S_ISVTX):
        raise OSError("receipt ancestor is writable by another user")
    if final and (info.st_uid != os.geteuid() or mode != 0o700):
        raise OSError("receipt directory must be owner-only mode 0700")


def _validate_private_receipt_file(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("receipt must be a regular file")
    if info.st_uid != os.geteuid():
        raise OSError("receipt must be owned by the effective user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise OSError("receipt must have mode 0600")
    if info.st_nlink != 1:
        raise OSError("receipt must have exactly one link")


def _verify_named_receipt_identity(
    *,
    absolute: Path,
    original_directory_fd: int,
    original_file_fd: int,
) -> None:
    original_directory = os.fstat(original_directory_fd)
    original_file = os.fstat(original_file_fd)
    fresh_directory_fd = _open_private_receipt_directory(absolute.parent)
    try:
        fresh_directory = os.fstat(fresh_directory_fd)
        if (fresh_directory.st_dev, fresh_directory.st_ino) != (
            original_directory.st_dev,
            original_directory.st_ino,
        ):
            raise OSError("receipt directory identity changed during append")
        read_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        fresh_file_fd = os.open(absolute.name, read_flags, dir_fd=fresh_directory_fd)
        try:
            _validate_private_receipt_file(fresh_file_fd)
            fresh_file = os.fstat(fresh_file_fd)
            if (fresh_file.st_dev, fresh_file.st_ino) != (
                original_file.st_dev,
                original_file.st_ino,
            ):
                raise OSError("receipt file identity changed during append")
        finally:
            os.close(fresh_file_fd)
    finally:
        os.close(fresh_directory_fd)


def _bounded_destination_alias(value: str | None) -> str:
    if value and DESTINATION_ALIAS_RE.fullmatch(value):
        return value
    return "unconfigured"


def validate_alert_configuration(
    *,
    alert_command: Path,
    destination_alias: str | None,
) -> bool:
    result = _validate_alert_configuration(
        alert_command=alert_command,
        destination_alias=destination_alias,
        cancelled_cleanup_callback=None,
    )
    return bool(result)


def _validate_alert_configuration(
    *,
    alert_command: Path,
    destination_alias: str | None,
    cancelled_cleanup_callback: Callable[[str], None] | None,
) -> bool | None:
    """Ask the delivery shim to validate alias + separately supplied target config."""
    if _bounded_destination_alias(destination_alias) == "unconfigured":
        print("pr-shepherd watch: alert destination alias is missing or invalid", file=sys.stderr)
        return False
    if not alert_command.is_file() or not os.access(alert_command, os.X_OK):
        print(f"{alert_command}: alert command missing", file=sys.stderr)
        return False
    try:
        result = _run_alert_process(
            [str(alert_command), "--check-config", "--destination-alias", destination_alias],
            input_text=None,
            timeout_seconds=ALERT_CONFIG_TIMEOUT_SECONDS,
            cancelled_cleanup_callback=cancelled_cleanup_callback,
        )
    except subprocess.TimeoutExpired:
        print(f"{alert_command}: alert configuration check timed out", file=sys.stderr)
        return False
    except _AlertSignalReplayed:
        raise
    except _AlertTerminalCleanupFailure:
        if cancelled_cleanup_callback is not None:
            return None
        return False
    except _AlertCleanupFailure:
        return False
    except OSError as exc:
        print(f"{alert_command}: alert configuration check failed ({type(exc).__name__})", file=sys.stderr)
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        print(f"{alert_command}: alert configuration invalid: {detail}", file=sys.stderr)
        return False
    return True


def _watch_alert_body(
    *,
    stalls: list[dict[str, Any]],
    orphans: list[dict[str, Any]],
    refresh_error: str | None = None,
    limit: int = 8,
) -> str:
    """Human-sized Telegram body for daily watch findings.

    The command still prints full JSON to stdout for logs/debugging. Telegram should carry
    the actionable summary and point operators to the CLI for the full machine payload.
    """
    lines: list[str] = []
    if refresh_error:
        lines.append(f"repo-facts refresh failed: {refresh_error}")
        lines.append("")
    if stalls:
        lines.append("Stalled auto-merge PRs:")
        for item in stalls[:limit]:
            lines.append(
                _format_watch_item(
                    item,
                    include_check_status=True,
                    fallback="stalled PR",
                )
            )
        if len(stalls) > limit:
            lines.append(f"… plus {len(stalls) - limit} more stalled PR(s).")
        lines.append("")
    if orphans:
        lines.append("Orphaned unresolved Codex P1 findings on merged PRs:")
        for item in orphans[:limit]:
            lines.append(_format_watch_item(item, fallback="orphaned finding"))
        if len(orphans) > limit:
            lines.append(f"… plus {len(orphans) - limit} more orphaned finding(s).")
        lines.append("")
    lines.append("Full JSON stays in the pr-shepherd watch stdout/logs; Telegram is summary-only.")
    lines.append("Next action: fix/dismiss findings with evidence, then rerun pr-shepherd watch.")
    return "\n".join(lines).rstrip()


def _format_watch_item(
    item: dict[str, Any],
    *,
    fallback: str,
    include_check_status: bool = False,
) -> str:
    repo = item.get("repo") or "unknown repo"
    number = item.get("number")
    pr_ref = f"{repo}#{number}" if number else str(repo)
    bits = [f"- {pr_ref}"]
    priority = item.get("priority")
    if priority:
        bits.append(str(priority))
    path = item.get("path")
    line = item.get("line")
    if path:
        bits.append(f"{path}:{line}" if line else str(path))
    if include_check_status and item.get("check_status"):
        bits.append(f"checks={item['check_status']}")
    title = item.get("title") or item.get("error") or fallback
    bits.append(_plain_watch_title(title)[:140])
    url = item.get("url")
    if url:
        bits.append(str(url))
    return " — ".join(bits)


def _plain_watch_title(value: Any) -> str:
    """Render bot review titles as Telegram-safe plain text."""
    text = str(value).replace("\n", " ")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("**", " ").replace("__", " ")
    return " ".join(text.split())


def deliver_alert(
    *,
    alert_command: Path,
    subject: str,
    body: str,
    destination_alias: str | None = None,
) -> bool:
    result = _deliver_alert(
        alert_command=alert_command,
        subject=subject,
        body=body,
        destination_alias=destination_alias,
        cancelled_cleanup_callback=None,
    )
    return bool(result)


def _deliver_alert(
    *,
    alert_command: Path,
    subject: str,
    body: str,
    destination_alias: str | None,
    cancelled_cleanup_callback: Callable[[str], None] | None,
) -> bool | None:
    """Like send_alert, but reports failure instead of silently skipping."""
    if len(subject.encode("utf-8", errors="replace")) > MAX_ALERT_SUBJECT_BYTES:
        print("pr-shepherd watch: alert subject exceeds byte limit", file=sys.stderr)
        return False
    if len(body.encode("utf-8", errors="replace")) > MAX_ALERT_BODY_BYTES:
        print("pr-shepherd watch: alert body exceeds byte limit", file=sys.stderr)
        return False
    if not alert_command.is_file() or not os.access(alert_command, os.X_OK):
        print(
            f"{alert_command}: alert command missing, findings were not delivered",
            file=sys.stderr,
        )
        return False
    command = [str(alert_command), "--subject", subject]
    if destination_alias:
        command.extend(["--destination-alias", destination_alias])
    try:
        result = _run_alert_process(
            command,
            input_text=body,
            timeout_seconds=ALERT_DELIVERY_TIMEOUT_SECONDS,
            cancelled_cleanup_callback=cancelled_cleanup_callback,
        )
    except subprocess.TimeoutExpired:
        print(f"{alert_command}: alert delivery timed out", file=sys.stderr)
        return False
    except _AlertSignalReplayed:
        raise
    except _AlertTerminalCleanupFailure:
        if cancelled_cleanup_callback is not None:
            return None
        return False
    except _AlertCleanupFailure:
        return False
    except OSError as exc:
        print(
            f"{alert_command}: alert delivery failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        print(f"{alert_command}: alert delivery failed: {detail}", file=sys.stderr)
        return False
    return True


def _run_alert_process(
    command: list[str],
    *,
    input_text: str | None,
    timeout_seconds: float,
    cancelled_cleanup_callback: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    watched_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    if threading.current_thread() is not threading.main_thread():
        raise OSError("alert subprocess supervision requires the main thread")
    previous_handlers: dict[int, Any] = {}
    cancellation: list[_AlertParentSignal | None] = [None]
    armed = [False]
    cleaning = [False]
    handoff_active = [False]
    restoration_unproven = [False]
    terminal_callback_attempted = [False]
    terminal_failure_code: list[str | None] = [None]
    escaping_failure: BaseException | None = None

    def terminal_callback(failure_code: str) -> None:
        if cancelled_cleanup_callback is None or terminal_callback_attempted[0]:
            return
        if terminal_failure_code[0] is None:
            terminal_failure_code[0] = failure_code
        terminal_callback_attempted[0] = True
        cancelled_cleanup_callback(terminal_failure_code[0])

    owned_terminal_callback = (
        terminal_callback if cancelled_cleanup_callback is not None else None
    )

    def attempt_owned_terminal_work(
        *,
        failure_code: str,
        cleanup_unproven: bool,
    ) -> BaseException | None:
        if terminal_failure_code[0] is None:
            terminal_failure_code[0] = failure_code
        return _attempt_alert_owned_terminal_work(
            callback=owned_terminal_callback,
            failure_code=failure_code,
            cleanup_unproven=cleanup_unproven,
        )

    def cancel_for_parent_signal(signum: int, frame: Any) -> None:
        if restoration_unproven[0]:
            parent_failure = _AlertParentSignal(signum, frame)
            if handoff_active[0] and cancellation[0] is not None:
                _resume_alert_parent_signal(
                    parent_failure,
                    previous_handler=previous_handlers.get(
                        signum,
                        signal.SIG_DFL,
                    ),
                )
            if handoff_active[0]:
                cancellation[0] = parent_failure
            try:
                if not terminal_callback_attempted[0]:
                    if terminal_failure_code[0] is None:
                        terminal_failure_code[0] = "alert_cleanup_unproven"
                    if owned_terminal_callback is not None:
                        _attempt_alert_handoff_callback(
                            owned_terminal_callback,
                            terminal_failure_code[0],
                        )
            finally:
                if handoff_active[0]:
                    raise parent_failure
                _resume_alert_parent_signal(
                    parent_failure,
                    previous_handler=previous_handlers.get(signum, signal.SIG_DFL),
                )
        if cancellation[0] is not None:
            return
        cancellation[0] = _AlertParentSignal(signum, frame)
        if (armed[0] and not cleaning[0]) or handoff_active[0]:
            cleaning[0] = True
            raise cancellation[0]

    def finish_signal_handoff(
        *,
        blocked_mask: set[signal.Signals],
        callback_already_attempted: bool,
    ) -> None:
        handoff_active[0] = True
        try:
            _finish_alert_signal_handoff(
                previous_handlers=previous_handlers,
                blocked_mask=blocked_mask,
                callback=owned_terminal_callback,
                callback_already_attempted=callback_already_attempted,
            )
        except _AlertParentSignal as parent_failure:
            signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
            if not callback_already_attempted:
                attempt_owned_terminal_work(
                    failure_code="alert_cancelled",
                    cleanup_unproven=False,
                )
            _finish_captured_alert_parent(
                parent_failure,
                previous_handlers=previous_handlers,
                watched_signals=watched_signals,
                blocked_mask=blocked_mask,
            )
            raise AssertionError("parent signal replay returned unexpectedly")
        finally:
            handoff_active[0] = False

    process: subprocess.Popen[str] | None = None
    try:
        try:
            for signum in watched_signals:
                previous = signal.getsignal(signum)
                previous_handlers[signum] = previous
                if previous is not signal.SIG_IGN:
                    signal.signal(signum, cancel_for_parent_signal)
            if cancellation[0] is not None:
                raise cancellation[0]
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            armed[0] = True
            if cancellation[0] is not None:
                raise cancellation[0]
            stdout, stderr = process.communicate(
                input=input_text,
                timeout=timeout_seconds,
            )
            _wait_for_alert_process_group_exit(process.pid)
            result = subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout,
                stderr,
            )
            cleaning[0] = True
            armed[0] = False
        except BaseException as failure:
            cleaning[0] = True
            cleanup_error: _AlertCleanupFailure | OSError | None = None
            if process is not None:
                if process.returncode is None:
                    try:
                        _cleanup_alert_process(process)
                    except (_AlertCleanupFailure, OSError) as exc:
                        cleanup_error = exc
                elif isinstance(failure, _AlertCleanupFailure):
                    cleanup_error = failure
                else:
                    try:
                        _wait_for_alert_process_group_exit(process.pid)
                    except _AlertCleanupFailure as exc:
                        cleanup_error = exc
            if cleanup_error is None and isinstance(failure, _AlertCleanupFailure):
                cleanup_error = failure

            blocked_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
            parent_failure = (
                failure if isinstance(failure, _AlertParentSignal) else cancellation[0]
            )
            callback_attempted = False
            deferred_failure: BaseException | None = None
            if cleanup_error is not None:
                deferred_failure = attempt_owned_terminal_work(
                    failure_code="alert_cleanup_unproven",
                    cleanup_unproven=True,
                )
                callback_attempted = cancelled_cleanup_callback is not None
            elif parent_failure is not None:
                deferred_failure = attempt_owned_terminal_work(
                    failure_code="alert_cancelled",
                    cleanup_unproven=False,
                )
                callback_attempted = cancelled_cleanup_callback is not None

            _capture_pending_alert_parent_signal(
                blocked_mask=blocked_mask,
                watched_signals=watched_signals,
            )
            parent_failure = (
                failure if isinstance(failure, _AlertParentSignal) else cancellation[0]
            )
            if (
                cleanup_error is None
                and parent_failure is not None
                and not callback_attempted
            ):
                deferred_failure = attempt_owned_terminal_work(
                    failure_code="alert_cancelled",
                    cleanup_unproven=False,
                )
                _capture_pending_alert_parent_signal(
                    blocked_mask=blocked_mask,
                    watched_signals=watched_signals,
                )
            if parent_failure is not None:
                _finish_captured_alert_parent(
                    parent_failure,
                    previous_handlers=previous_handlers,
                    watched_signals=watched_signals,
                    blocked_mask=blocked_mask,
                )
                raise AssertionError("parent signal replay returned unexpectedly")
            try:
                finish_signal_handoff(
                    blocked_mask=blocked_mask,
                    callback_already_attempted=(
                        callback_attempted or cleanup_error is not None
                    ),
                )
            except _AlertSignalReplayed:
                raise
            except Exception:
                if deferred_failure is not None:
                    raise deferred_failure
                if cleanup_error is not None:
                    raise _AlertTerminalCleanupFailure from cleanup_error
                raise
            if deferred_failure is not None:
                raise deferred_failure
            if cleanup_error is not None:
                raise _AlertTerminalCleanupFailure from cleanup_error
            raise

        blocked_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
        parent_failure = cancellation[0]
        deferred_failure = None
        if parent_failure is not None:
            deferred_failure = attempt_owned_terminal_work(
                failure_code="alert_cancelled",
                cleanup_unproven=False,
            )
        _capture_pending_alert_parent_signal(
            blocked_mask=blocked_mask,
            watched_signals=watched_signals,
        )
        if parent_failure is None and cancellation[0] is not None:
            parent_failure = cancellation[0]
            deferred_failure = attempt_owned_terminal_work(
                failure_code="alert_cancelled",
                cleanup_unproven=False,
            )
        if parent_failure is not None:
            _finish_captured_alert_parent(
                parent_failure,
                previous_handlers=previous_handlers,
                watched_signals=watched_signals,
                blocked_mask=blocked_mask,
            )
            raise AssertionError("parent signal replay returned unexpectedly")
        finish_signal_handoff(
            blocked_mask=blocked_mask,
            callback_already_attempted=False,
        )
        if deferred_failure is not None:
            raise deferred_failure
        return result
    except BaseException as exc:
        escaping_failure = exc
        raise
    finally:
        armed[0] = False
        cleaning[0] = True
        handoff_active[0] = True
        restoration_unproven[0] = bool(previous_handlers)
        try:
            try:
                _restore_alert_signal_handlers_safely(
                    previous_handlers,
                    watched_signals,
                )
                restoration_unproven[0] = False
            except _AlertSignalReplayed:
                raise
            except Exception as restoration_error:
                deferred_failure = attempt_owned_terminal_work(
                    failure_code="alert_cleanup_unproven",
                    cleanup_unproven=True,
                )
                signal_dominates = isinstance(
                    escaping_failure,
                    _AlertSignalReplayed,
                ) or (
                    escaping_failure is not None
                    and not isinstance(escaping_failure, Exception)
                )
                if signal_dominates:
                    pass
                elif deferred_failure is not None:
                    raise deferred_failure
                elif not isinstance(
                    escaping_failure,
                    _AlertTerminalCleanupFailure,
                ):
                    raise _AlertTerminalCleanupFailure from restoration_error
        except _AlertParentSignal as parent_failure:
            outer_blocked_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                watched_signals,
            )
            if not terminal_callback_attempted[0]:
                attempt_owned_terminal_work(
                    failure_code=(
                        "alert_cleanup_unproven"
                        if restoration_unproven[0]
                        else "alert_cancelled"
                    ),
                    cleanup_unproven=restoration_unproven[0],
                )
            _finish_captured_alert_parent(
                parent_failure,
                previous_handlers=previous_handlers,
                watched_signals=watched_signals,
                blocked_mask=outer_blocked_mask,
            )
            raise AssertionError("parent signal replay returned unexpectedly")
        finally:
            handoff_active[0] = False


class _AlertParentSignal(BaseException):
    def __init__(self, signum: int, frame: Any) -> None:
        super().__init__(signum)
        self.signum = signum
        self.frame = frame


class _AlertCleanupFailure(RuntimeError):
    pass


class _AlertTerminalCleanupFailure(RuntimeError):
    pass


class _AlertSignalReplayed(InterruptedError):
    pass


def _attempt_alert_owned_terminal_work(
    *,
    callback: Callable[[str], None] | None,
    failure_code: str,
    cleanup_unproven: bool,
) -> BaseException | None:
    deferred_failure: BaseException | None = None
    if cleanup_unproven:
        try:
            _emit_alert_failure_diagnostic(
                "pr-shepherd: alert cleanup remained unproven"
            )
        except BaseException as exc:
            deferred_failure = exc
    if callback is not None:
        try:
            _attempt_alert_terminal_callback(callback, failure_code)
        except BaseException as exc:
            if deferred_failure is None:
                deferred_failure = exc
    return deferred_failure


def _emit_alert_failure_diagnostic(message: str) -> None:
    try:
        print(message, file=sys.stderr)
    except _AlertSignalReplayed:
        raise
    except Exception:
        pass


def _attempt_alert_terminal_callback(
    callback: Callable[[str], None],
    failure_code: str,
) -> None:
    try:
        callback(failure_code)
    except _AlertSignalReplayed:
        raise
    except Exception:
        try:
            print(
                "pr-shepherd: terminal alert receipt callback failed",
                file=sys.stderr,
            )
        except _AlertSignalReplayed:
            raise
        except Exception:
            pass


def _restore_alert_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    for signum, previous in tuple(previous_handlers.items()):
        signal.signal(signum, previous)
        del previous_handlers[signum]


def _capture_pending_alert_parent_signal(
    *,
    blocked_mask: set[signal.Signals],
    watched_signals: tuple[int, ...],
) -> None:
    signal.pthread_sigmask(signal.SIG_SETMASK, blocked_mask)
    signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)


def _restore_alert_signal_mask(blocked_mask: set[signal.Signals]) -> None:
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked_mask)
    except _AlertSignalReplayed:
        raise
    except (OSError, _AlertCleanupFailure, subprocess.TimeoutExpired) as exc:
        raise _AlertSignalReplayed(
            "alert supervision received a signal after terminal cleanup"
        ) from exc


def _finish_alert_signal_handoff(
    *,
    previous_handlers: dict[int, Any],
    blocked_mask: set[signal.Signals],
    callback: Callable[[str], None] | None,
    callback_already_attempted: bool,
) -> None:
    attempted = [callback_already_attempted]
    watched_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)

    def handoff(signum: int, frame: Any) -> None:
        callback_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
        previous = previous_handlers.get(signum, signal.SIG_DFL)
        signal.signal(signum, previous)
        previous_handlers.pop(signum, None)
        try:
            if not attempted[0]:
                attempted[0] = True
                if callback is not None:
                    _attempt_alert_handoff_callback(callback, "alert_cancelled")
        finally:
            try:
                _restore_alert_signal_mask(callback_mask)
            finally:
                _resume_alert_parent_signal(
                    _AlertParentSignal(signum, frame),
                    previous_handler=previous,
                )

    installed = False
    try:
        for signum, previous in previous_handlers.items():
            if previous is not signal.SIG_IGN:
                signal.signal(signum, handoff)
        installed = True
    finally:
        if not installed:
            _restore_alert_signal_mask(blocked_mask)

    _restore_alert_signal_mask(blocked_mask)
    handoff_restore_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
    restored = False
    try:
        try:
            _restore_alert_signal_handlers(previous_handlers)
        except Exception:
            _restore_alert_signal_handlers(previous_handlers)
        restored = True
    finally:
        if not restored:
            _restore_alert_signal_mask(handoff_restore_mask)
    pending = signal.sigpending().intersection(watched_signals).difference(
        handoff_restore_mask
    )
    _restore_alert_signal_mask(handoff_restore_mask)
    if pending:
        raise _AlertSignalReplayed(
            "caller-owned signal arrived during alert handler transfer"
        )


def _attempt_alert_handoff_callback(
    callback: Callable[[str], None],
    failure_code: str,
) -> None:
    try:
        callback(failure_code)
    except _AlertSignalReplayed:
        raise
    except Exception:
        try:
            print(
                "pr-shepherd: signal-handoff receipt callback failed",
                file=sys.stderr,
            )
        except _AlertSignalReplayed:
            raise
        except Exception:
            pass


def _finish_captured_alert_parent(
    parent_failure: _AlertParentSignal,
    *,
    previous_handlers: dict[int, Any],
    watched_signals: tuple[int, ...],
    blocked_mask: set[signal.Signals],
) -> None:
    previous_handler = previous_handlers.get(parent_failure.signum, signal.SIG_DFL)
    try:
        try:
            _restore_owned_alert_signal_handlers(
                previous_handlers,
                blocked_mask=blocked_mask,
            )
        except BaseException:
            try:
                _restore_alert_signal_handlers_safely(
                    previous_handlers,
                    watched_signals,
                )
            except BaseException:
                pass
        _restore_alert_signal_mask(blocked_mask)
    finally:
        _resume_alert_parent_signal(
            parent_failure,
            previous_handler=previous_handler,
        )


def _restore_owned_alert_signal_handlers(
    previous_handlers: dict[int, Any],
    *,
    blocked_mask: set[signal.Signals],
) -> None:
    restored = False
    try:
        try:
            _restore_alert_signal_handlers(previous_handlers)
        except Exception:
            # A transient handler-restoration failure must not strand a partial map.
            _restore_alert_signal_handlers(previous_handlers)
        restored = True
    finally:
        if not restored:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked_mask)


def _restore_alert_signal_handlers_safely(
    previous_handlers: dict[int, Any],
    watched_signals: tuple[int, ...],
) -> None:
    if not previous_handlers:
        return
    blocked_mask = signal.pthread_sigmask(signal.SIG_BLOCK, watched_signals)
    try:
        _restore_alert_signal_handlers(previous_handlers)
    finally:
        _restore_alert_signal_mask(blocked_mask)


def _resume_alert_parent_signal(
    failure: _AlertParentSignal,
    *,
    previous_handler: Any | None = None,
) -> None:
    previous = (
        signal.getsignal(failure.signum)
        if previous_handler is None
        else previous_handler
    )
    if previous is signal.SIG_DFL:
        if signal.getsignal(failure.signum) is not signal.SIG_DFL:
            signal.signal(failure.signum, signal.SIG_DFL)
        os.kill(os.getpid(), failure.signum)
        raise SystemExit(128 + failure.signum)
    if callable(previous):
        try:
            previous(failure.signum, failure.frame)
        except _AlertSignalReplayed:
            raise
        except Exception as exc:
            raise _AlertSignalReplayed(
                f"alert supervision cancelled by signal {failure.signum}"
            ) from exc
    raise _AlertSignalReplayed(
        f"alert supervision cancelled by signal {failure.signum}"
    )


def _signal_alert_process_group(pgid: int, signum: int) -> bool:
    deadline = time.monotonic() + ALERT_TERM_GRACE_SECONDS
    while True:
        try:
            os.killpg(pgid, signum)
            return True
        except ProcessLookupError:
            return False
        except InterruptedError:
            if time.monotonic() >= deadline:
                raise _AlertCleanupFailure(
                    "alert process group signalling could not be completed"
                ) from None
        except OSError as exc:
            raise _AlertCleanupFailure(
                "alert process group signalling could not be completed"
            ) from exc


def _sleep_alert_term_grace() -> None:
    deadline = time.monotonic() + ALERT_TERM_GRACE_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            time.sleep(remaining)
            return
        except InterruptedError:
            continue


def _wait_for_alert_process_group_exit(pgid: int) -> None:
    deadline = time.monotonic() + ALERT_GROUP_VERIFY_SECONDS
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except InterruptedError:
            pass
        except OSError as exc:
            raise _AlertCleanupFailure(
                "alert process group could not be proven gone"
            ) from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _AlertCleanupFailure(
                "alert process group could not be proven gone"
            )
        try:
            time.sleep(min(ALERT_GROUP_POLL_SECONDS, remaining))
        except InterruptedError:
            continue


def _cleanup_alert_process(process: subprocess.Popen[str]) -> None:
    pgid = process.pid
    signal_error: _AlertCleanupFailure | None = None
    term_sent = False
    try:
        term_sent = _signal_alert_process_group(pgid, signal.SIGTERM)
    except _AlertCleanupFailure as exc:
        signal_error = exc
    if term_sent:
        _sleep_alert_term_grace()
        try:
            # The direct child remains unreaped here, retaining the PGID anchor.
            _signal_alert_process_group(pgid, signal.SIGKILL)
        except _AlertCleanupFailure as exc:
            signal_error = signal_error or exc
    reap_error: _AlertCleanupFailure | None = None
    try:
        process.communicate(timeout=ALERT_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        try:
            process.kill()
            process.communicate(timeout=ALERT_TERM_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as final_exc:
            raise _AlertCleanupFailure(
                "alert process group direct child could not be reaped"
            ) from final_exc
        reap_error = _AlertCleanupFailure(
            "alert process group direct child exceeded kill grace"
        )
        reap_error.__cause__ = exc
    except OSError as exc:
        raise _AlertCleanupFailure(
            "alert process group direct child could not be reaped"
        ) from exc
    # After direct-child reap, only non-destructive signal-0 probes are safe.
    # Darwin has no pidfd for process groups: a reused PGID can conservatively
    # fail this bound, but it can never receive another TERM/KILL from us.
    _wait_for_alert_process_group_exit(pgid)
    if signal_error is not None:
        raise signal_error
    if reap_error is not None:
        raise reap_error


def send_alert(
    *,
    alert_command: Path,
    subject: str,
    body: str,
    destination_alias: str | None = None,
) -> bool:
    """Best-effort safety alert that preserves merge behavior while surfacing failure."""
    return deliver_alert(
        alert_command=alert_command,
        destination_alias=destination_alias,
        subject=subject,
        body=body,
    )


def append_ledger(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        **entry,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _thread_summary(node: dict[str, Any]) -> dict[str, Any]:
    comments = node.get("comments", {}).get("nodes", [])
    first = comments[0] if comments else {}
    author = first.get("author") if isinstance(first.get("author"), dict) else {}
    return {
        "thread_id": node.get("id"),
        "comment_id": first.get("databaseId"),
        "path": node.get("path") or first.get("path"),
        "line": node.get("line") or first.get("line"),
        "author": author.get("login"),
        "body": first.get("body"),
        "isResolved": bool(node.get("isResolved")),
        "outdated": bool(node.get("isOutdated")),
    }


def _json(text: str, fallback: Any) -> Any:
    try:
        return json.loads(text or "")
    except json.JSONDecodeError:
        return fallback


if __name__ == "__main__":
    raise SystemExit(main())
