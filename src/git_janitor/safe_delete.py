from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .git import parse_github_remote, parse_worktree_porcelain, run_command


Runner = Any
DEFAULT_PRESERVATION_DIR = Path("~/.local/share/git-janitor/preservation").expanduser()
DEFAULT_DELETE_LEDGER = Path("reports/branch-cleanup-audit.jsonl")


@dataclass(frozen=True)
class Proof:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class DeleteReport:
    branch: str
    repo: str | None
    default_ref: str
    merge_commit: str | None
    command: list[str]
    proofs: tuple[Proof, ...]

    @property
    def passed(self) -> bool:
        return all(proof.passed for proof in self.proofs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "repo": self.repo,
            "default_ref": self.default_ref,
            "merge_commit": self.merge_commit,
            "command": self.command,
            "passed": self.passed,
            "proofs": [
                {"name": proof.name, "passed": proof.passed, "detail": proof.detail}
                for proof in self.proofs
            ],
        }


def git_safe_delete_main(argv: list[str] | None = None, *, runner: Runner = run_command) -> int:
    parser = argparse.ArgumentParser(description="Safely delete a proved-merged local branch.")
    parser.add_argument("branch")
    parser.add_argument("--execute", action="store_true", help="Actually delete the branch.")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ledger", type=Path, default=DEFAULT_DELETE_LEDGER)
    args = parser.parse_args(argv)
    report = evaluate_delete(args.branch, repo_path=args.repo, runner=runner)
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    if not report.passed:
        append_ledger(args.ledger, report=report, status="blocked", exit_code=None, detail="proof failed")
        return 3
    if not args.execute:
        append_ledger(args.ledger, report=report, status="dry-run", exit_code=None, detail="no command ran")
        return 0
    result = runner(report.command, args.repo, 45)
    if result.returncode != 0:
        print(result.stderr or "git branch deletion failed")
    append_ledger(
        args.ledger,
        report=report,
        status="applied" if result.returncode == 0 else "failed",
        exit_code=result.returncode,
        detail=result.stderr or result.stdout,
    )
    return result.returncode


def preserve_branch_main(argv: list[str] | None = None, *, runner: Runner = run_command) -> int:
    parser = argparse.ArgumentParser(description="Bundle and document a branch before cleanup.")
    parser.add_argument("branch")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--reason", default="cleanup proof incomplete")
    parser.add_argument("--validation", default="not validated")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_PRESERVATION_DIR)
    args = parser.parse_args(argv)
    result = preserve_branch(
        args.branch,
        repo_path=args.repo,
        reason=args.reason,
        validation=args.validation,
        output_dir=args.output_dir.expanduser(),
        runner=runner,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 3


def evaluate_delete(
    branch: str,
    *,
    repo_path: Path,
    runner: Runner = run_command,
    owned_worktree: Path | None = None,
    expected_head: str | None = None,
    verified_merge_commit: str | None = None,
) -> DeleteReport:
    proofs: list[Proof] = []
    repo = _github_repo(repo_path, runner)
    default_ref = _default_ref(repo_path, runner)
    merge_commit: str | None = None

    branch_ref = runner(["git", "rev-parse", "--verify", branch], repo_path, 30)
    proofs.append(_proof("branch_exists", branch_ref.returncode == 0, branch_ref.stdout or branch_ref.stderr))

    current = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_path, 30)
    proofs.append(
        _proof(
            "not_active_checkout",
            current.returncode == 0 and current.stdout != branch,
            current.stdout or current.stderr,
        )
    )

    status_args = ["git", "status", "--porcelain=v1"]
    if owned_worktree is not None:
        try:
            owned_relative = owned_worktree.relative_to(repo_path)
        except ValueError:
            owned_relative = Path(".")
        scoped_owner = owned_relative != Path(".") and expected_head and verified_merge_commit
        proofs.append(_proof("owned_worktree_scope", bool(scoped_owner), "exact pinned task subtree required"))
        if scoped_owner:
            # Only the named task subtree is exempt from primary-checkout dirt.
            # --untracked-files=all keeps sibling files/worktrees visible rather
            # than dropping a collapsed ?? .worktrees/ row.
            status_args += ["--untracked-files=all", "--", ".",
                            f":(exclude,literal){owned_relative.as_posix()}"]
    status = runner(status_args, repo_path, 30)
    proofs.append(
        _proof(
            "worktree_clean",
            status.returncode == 0 and not status.stdout.strip(),
            status.stdout or status.stderr or "clean",
        )
    )

    worktree = runner(["git", "worktree", "list", "--porcelain"], repo_path, 30)
    active_path = _branch_worktree(branch, worktree.stdout) if worktree.returncode == 0 else None
    owned = owned_worktree is not None and active_path == str(owned_worktree)
    if owned_worktree is not None:
        owned_status = runner(["git", "status", "--porcelain=v1", "--untracked-files=all"], owned_worktree, 30)
        proofs.append(_proof("owned_worktree_clean", owned_status.returncode == 0 and not owned_status.stdout,
                             owned_status.stdout or owned_status.stderr or "clean"))
    proofs.append(
        _proof(
            "branch_not_checked_out_elsewhere",
            worktree.returncode == 0 and (active_path is None or owned),
            active_path or worktree.stderr or "not checked out",
        )
    )

    upstream = runner(
        ["git", "for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}"],
        repo_path,
        30,
    )
    upstream_name = upstream.stdout.strip()
    upstream_gone = False
    if upstream_name:
        upstream_ref = runner(["git", "rev-parse", "--verify", "--quiet", upstream_name], repo_path, 30)
        upstream_gone = upstream_ref.returncode != 0
    ancestor = runner(["git", "merge-base", "--is-ancestor", branch, default_ref], repo_path, 30)
    pinned_squash = False
    if expected_head is not None or verified_merge_commit is not None:
        exact_head = bool(expected_head) and branch_ref.stdout.strip() == expected_head
        equivalent = runner(
            ["git", "diff", "--quiet", str(verified_merge_commit), str(expected_head)],
            repo_path, 30,
        ) if exact_head and verified_merge_commit else None
        pinned_squash = bool(equivalent and equivalent.returncode == 0)
        proofs.append(_proof("pinned_merge_tree", pinned_squash, "exact head and merge tree required"))
    upstream_or_ancestor = upstream_gone or ancestor.returncode == 0 or pinned_squash
    proofs.append(
        _proof(
            "upstream_gone_or_ancestor",
            upstream_or_ancestor,
            f"upstream={upstream_name or '-'} upstream_gone={upstream_gone} ancestor={ancestor.returncode == 0}",
        )
    )

    open_prs = _open_prs(repo, branch, repo_path, runner)
    proofs.append(
        _proof(
            "no_open_pr",
            open_prs == [],
            json.dumps(open_prs) if open_prs else "no open PR",
        )
    )

    merged_prs = _merged_prs(repo, branch, repo_path, runner)
    if merged_prs:
        merge_commit = merged_prs[0].get("mergeCommit", {}).get("oid") or merged_prs[0].get(
            "mergeCommit"
        )
    if verified_merge_commit and merge_commit != verified_merge_commit:
        proofs.append(_proof("merged_receipt_matches", False, "merge readback changed"))
    readback_or_ancestry = bool(merged_prs) or ancestor.returncode == 0
    proofs.append(
        _proof(
            "merged_pr_readback_or_ancestry",
            readback_or_ancestry,
            json.dumps(merged_prs) if merged_prs else f"ancestor={ancestor.returncode == 0}",
        )
    )

    tree_equivalent = False
    if merge_commit:
        diff = runner(["git", "diff", "--quiet", str(merge_commit), branch], repo_path, 30)
        tree_equivalent = diff.returncode == 0
        proofs.append(
            _proof(
                "squash_tree_equivalence",
                tree_equivalent,
                "empty diff" if tree_equivalent else (diff.stderr or "non-empty diff"),
            )
        )
    else:
        proofs.append(_proof("squash_tree_equivalence", True, "not a squash-merge branch"))

    secret_risk = _secret_risk(branch, default_ref, repo_path, runner)
    proofs.append(_proof("no_suspected_secret", not secret_risk, secret_risk or "none"))

    command = ["git", "branch", "-d", branch]
    if merge_commit and tree_equivalent and ancestor.returncode != 0:
        command = ["git", "branch", "-D", branch]
    if expected_head is not None:
        # Compare-and-delete refuses a concurrently advanced local branch.
        command = ["git", "update-ref", "-d", f"refs/heads/{branch}", expected_head]
    return DeleteReport(
        branch=branch,
        repo=repo,
        default_ref=default_ref,
        merge_commit=merge_commit,
        command=command,
        proofs=tuple(proofs),
    )


def preserve_branch(
    branch: str,
    *,
    repo_path: Path,
    reason: str,
    validation: str,
    output_dir: Path,
    runner: Runner = run_command,
) -> dict[str, Any]:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"cannot create preservation directory: {exc}"}
    repo_name = _repo_name(repo_path)
    safe_branch = branch.replace("/", "-")
    bundle = output_dir / f"{repo_name}-{safe_branch}.bundle"
    manifest = output_dir / f"{repo_name}-{safe_branch}.md"
    head = runner(["git", "rev-parse", branch], repo_path, 30)
    if head.returncode != 0:
        return {"ok": False, "error": head.stderr or "branch not found"}
    bundle_result = runner(["git", "bundle", "create", str(bundle), branch], repo_path, 120)
    if bundle_result.returncode != 0:
        return {"ok": False, "error": bundle_result.stderr or "bundle create failed"}
    verify = runner(["git", "bundle", "verify", str(bundle)], repo_path, 120)
    if verify.returncode != 0:
        return {"ok": False, "error": verify.stderr or "bundle verify failed"}
    try:
        manifest.write_text(
            "\n".join(
                [
                    f"# Preserved branch: {repo_name}/{branch}",
                    "",
                    f"- repo: {repo_path}",
                    f"- branch: {branch}",
                    f"- worktree_path: {repo_path}",
                    f"- commit: {head.stdout}",
                    "- pr_mapping: not checked",
                    f"- preserved_at: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}",
                    f"- reason: {reason}",
                    f"- validation: {validation}",
                    f"- bundle: {bundle}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        ok = (
            bundle.exists()
            and bundle.stat().st_size > 0
            and manifest.exists()
            and manifest.stat().st_size > 0
        )
    except OSError as exc:
        ok = False
        write_error = str(exc)
    else:
        write_error = ""
    return {
        "ok": ok,
        "bundle": str(bundle),
        "manifest": str(manifest),
        "commit": head.stdout,
        "verify": verify.stdout or verify.stderr,
        "error": "" if ok else (write_error or "bundle or manifest missing/empty after write"),
    }


def append_ledger(
    path: Path,
    *,
    report: DeleteReport,
    status: str,
    exit_code: int | None,
    detail: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "tool": "git-safe-delete",
        "status": status,
        "branch": report.branch,
        "repo": report.repo,
        "command": report.command,
        "proofs": [
            {"name": proof.name, "passed": proof.passed, "detail": proof.detail}
            for proof in report.proofs
        ],
        "exit_code": exit_code,
        "detail": detail,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _github_repo(repo_path: Path, runner: Runner) -> str | None:
    remote = runner(["git", "remote", "get-url", "origin"], repo_path, 30)
    if remote.returncode != 0:
        return None
    return parse_github_remote(remote.stdout)


def _default_ref(repo_path: Path, runner: Runner) -> str:
    origin_head = runner(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        repo_path,
        30,
    )
    if origin_head.returncode == 0 and origin_head.stdout:
        return origin_head.stdout
    return "origin/main"


def _open_prs(repo: str | None, branch: str, repo_path: Path, runner: Runner) -> list[dict[str, Any]]:
    if not repo:
        return []
    result = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--head",
            branch,
            "--json",
            "number,title,url",
        ],
        repo_path,
        30,
    )
    if result.returncode != 0:
        return [{"error": result.stderr or "gh pr list failed"}]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [{"error": "gh pr list returned invalid JSON"}]
    if not isinstance(payload, list) or any(not isinstance(pr, dict) for pr in payload):
        return [{"error": "gh pr list returned an invalid PR list"}]
    return payload


def _merged_prs(repo: str | None, branch: str, repo_path: Path, runner: Runner) -> list[dict[str, Any]]:
    if not repo:
        return []
    result = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--head",
            branch,
            "--json",
            "number,title,url,mergeCommit",
        ],
        repo_path,
        30,
    )
    if result.returncode != 0:
        return []
    return _json(result.stdout, [])


def _secret_risk(branch: str, default_ref: str, repo_path: Path, runner: Runner) -> str:
    changed = runner(["git", "diff", "--name-only", f"{default_ref}...{branch}"], repo_path, 30)
    if changed.returncode != 0:
        return changed.stderr or "cannot inspect changed paths for secret risk"
    risky = [
        path
        for path in changed.stdout.splitlines()
        if ".env" in path
        or "secret" in path.lower()
        or "token" in path.lower()
        or path.endswith((".pem", ".key", ".p12"))
    ]
    return ", ".join(risky)


def _branch_worktree(branch: str, worktree_output: str) -> str | None:
    for entry in parse_worktree_porcelain(worktree_output):
        if entry.branch == branch:
            return entry.path
    return None


def _repo_name(path: Path) -> str:
    return path.resolve().name


def _proof(name: str, passed: bool, detail: str) -> Proof:
    return Proof(name=name, passed=passed, detail=detail.strip())


def _json(text: str, fallback: Any) -> Any:
    try:
        return json.loads(text or "")
    except json.JSONDecodeError:
        return fallback


if __name__ == "__main__":
    raise SystemExit(git_safe_delete_main())
