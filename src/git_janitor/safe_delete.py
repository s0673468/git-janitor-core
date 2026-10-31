from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Any

from .git import (
    CLEANUP_STATUS_FLAGS,
    cleanup_blockers,
    parse_github_remote,
    parse_worktree_porcelain,
    run_command,
)


Runner = Any
DEFAULT_PRESERVATION_DIR = Path("~/.local/share/git-janitor/preservation").expanduser()
DEFAULT_DELETE_LEDGER = Path("~/.local/state/git-janitor/branch-cleanup-audit.jsonl").expanduser()


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
    default_ref, default_unknown = _default_ref(repo_path, runner)
    merge_commit: str | None = None

    local_ref = f"refs/heads/{branch}"
    branch_ref = runner(["git", "show-ref", "--verify", "--hash", local_ref], repo_path, 30)
    proofs.append(_proof("branch_exists", branch_ref.returncode == 0, branch_ref.stdout or branch_ref.stderr))
    # The default branch is never a cleanup target; compare full (possibly nested) names.
    # Without a readable origin/HEAD the default is unknown, so no branch is proved non-default.
    default_branch = default_ref.removeprefix("origin/")
    proofs.append(
        _proof(
            "not_default_branch",
            not default_unknown and branch != default_branch,
            default_unknown or f"default={default_branch}",
        )
    )

    current = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_path, 30)
    proofs.append(
        _proof(
            "not_active_checkout",
            current.returncode == 0 and current.stdout != branch,
            current.stdout or current.stderr,
        )
    )

    status_args = ["git", "status", "--porcelain=v1", *CLEANUP_STATUS_FLAGS]
    scoped_owner = False
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
            status_args += ["-z", "--", ".",
                            f":(exclude,literal){owned_relative.as_posix()}"]
    status = runner(status_args, repo_path, 30)
    status_output = status.stdout
    if scoped_owner:
        # Git still emits an ignored directory despite the exclusion pathspec.
        # Exempt only that exact subtree's row; its own full status is checked
        # below. NUL records keep spaces, Unicode and quoted paths unambiguous.
        ignored_owner = f"!! {owned_relative.as_posix()}/"
        status_output = "\0".join(row for row in status_output.split("\0") if row != ignored_owner)
    proofs.append(
        _proof(
            "worktree_clean",
            status.returncode == 0 and not status_output.strip(),
            status_output or status.stderr or "clean",
        )
    )
    blockers = cleanup_blockers(repo_path, runner, 30)
    proofs.append(_proof("saved_and_nested_work_preserved", not blockers, "; ".join(blockers) or "none"))

    worktree = runner(["git", "worktree", "list", "--porcelain"], repo_path, 30)
    active_path = _branch_worktree(branch, worktree.stdout) if worktree.returncode == 0 else None
    owned = owned_worktree is not None and active_path == str(owned_worktree)
    if owned_worktree is not None:
        owned_status = runner(["git", "status", "--porcelain=v1", *CLEANUP_STATUS_FLAGS], owned_worktree, 30)
        proofs.append(_proof("owned_worktree_clean", owned_status.returncode == 0 and not owned_status.stdout,
                             owned_status.stdout or owned_status.stderr or "clean"))
        owned_blockers = cleanup_blockers(owned_worktree, runner, 30)
        proofs.append(_proof("owned_saved_and_nested_work_preserved", not owned_blockers,
                             "; ".join(owned_blockers) or "none"))
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
        # Only a clean "ref missing" exit proves gone; timeouts and fatal errors do not.
        upstream_gone = upstream_ref.returncode == 1
    comparison_ref = f"refs/remotes/{default_ref}"
    ancestor = runner(["git", "merge-base", "--is-ancestor", local_ref, comparison_ref], repo_path, 30)
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
        merge_commit = _merge_commit_oid(merged_prs[0])
    if verified_merge_commit and merge_commit != verified_merge_commit:
        proofs.append(_proof("merged_receipt_matches", False, "merge readback changed"))
    # A merged readback is evidence only when it names the merge commit to compare.
    readback_or_ancestry = bool(merge_commit) or ancestor.returncode == 0
    proofs.append(
        _proof(
            "merged_pr_readback_or_ancestry",
            readback_or_ancestry,
            json.dumps(merged_prs) if merged_prs else f"ancestor={ancestor.returncode == 0}",
        )
    )

    tree_equivalent = False
    if merge_commit:
        merged_ancestor = runner(
            ["git", "merge-base", "--is-ancestor", str(merge_commit), comparison_ref], repo_path, 30,
        )
        proofs.append(_proof("merge_reachable_from_default", merged_ancestor.returncode == 0,
                             "merge commit must be retained on the default branch"))
        diff = runner(["git", "diff", "--quiet", str(merge_commit), local_ref], repo_path, 30)
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

    secret_risk = _secret_risk(local_ref, comparison_ref, repo_path, runner)
    proofs.append(_proof("no_suspected_secret", not secret_risk, secret_risk or "none"))

    command = ["git", "branch", "-d", branch]
    if merge_commit and tree_equivalent and ancestor.returncode != 0:
        # Git's own -d check cannot vouch for a squash merge, so delete only the
        # proved head: a concurrent advance, or a same-name tag that the bare-name
        # proofs inspected instead of the branch, makes the compare-and-delete fail.
        command = ["git", "update-ref", "-d", f"refs/heads/{branch}", branch_ref.stdout.strip()]
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
    # Lock the directory inode: the kernel releases this lock even after a crash,
    # so the next run can recover publication without removing a live run's lock.
    # Keep respecting lock files left by older versions of the tool.
    lock = output_dir / f"{repo_name}-{safe_branch}.preserve-lock"
    try:
        directory_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        return {"ok": False, "error": f"cannot lock preservation {bundle}: {exc}"}
    try:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": False, "error": f"preservation in progress in {output_dir}"}
        if lock.exists() or lock.is_symlink():
            return {"ok": False, "error": f"preservation in progress: {lock} exists; remove it only if no run is active"}
        # Recover before reading the branch or checking saved history. In
        # particular, even a now-deleted or rewritten branch cannot strand a pair.
        journal = bundle.with_suffix(".preserve-journal")
        if journal.exists() or journal.is_symlink():
            _finish_preservation_publication(journal, bundle, manifest)
        return _preserve_locked(
            branch, repo_path=repo_path, reason=reason, validation=validation,
            repo_name=repo_name, bundle=bundle, manifest=manifest, runner=runner,
        )
    except OSError as exc:
        return {"ok": False, "error": f"preservation publication failed; retry to recover: {exc}"}
    finally:
        os.close(directory_fd)


def _preserve_locked(
    branch: str,
    *,
    repo_path: Path,
    reason: str,
    validation: str,
    repo_name: str,
    bundle: Path,
    manifest: Path,
    runner: Runner,
) -> dict[str, Any]:
    head = runner(["git", "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], repo_path, 30)
    if head.returncode != 0:
        return {"ok": False, "error": head.stderr or "branch not found"}
    if bundle.exists():
        conflict = _existing_bundle_conflict(bundle, branch, head.stdout, repo_path, runner)
        if conflict:
            return {"ok": False, "error": conflict}
    try:
        # Stage and verify the whole pair before publishing a recoverable journal.
        with TemporaryDirectory(prefix=f".{bundle.stem}-", dir=bundle.parent) as staging:
            pending_bundle = Path(staging) / bundle.name
            pending_manifest = Path(staging) / manifest.name
            bundle_result = runner(
                ["git", "bundle", "create", str(pending_bundle), f"refs/heads/{branch}"],
                repo_path, 120,
            )
            if bundle_result.returncode != 0:
                return {"ok": False, "error": bundle_result.stderr or "bundle create failed"}
            verify = runner(["git", "bundle", "verify", str(pending_bundle)], repo_path, 120)
            if verify.returncode != 0:
                return {"ok": False, "error": verify.stderr or "bundle verify failed"}
            listed = runner(["git", "bundle", "list-heads", str(pending_bundle)], repo_path, 120)
            if listed.returncode != 0 or listed.stdout.split() != [head.stdout, f"refs/heads/{branch}"]:
                return {"ok": False, "error": "branch moved during preservation; retry with its new head"}
            # Compare-and-swap the inspected ref to itself. Together with the
            # bundle head check, this pins both artifacts to the commit whose
            # ancestry was checked, even if the branch moves after this CAS.
            pinned = runner(
                ["git", "update-ref", "--no-deref", f"refs/heads/{branch}", head.stdout, head.stdout],
                repo_path, 30,
            )
            if pinned.returncode != 0:
                return {"ok": False, "error": "branch moved during preservation; retry: " + pinned.stderr}
            pending_manifest.write_text(
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
                pending_bundle.exists()
                and pending_bundle.stat().st_size > 0
                and pending_manifest.stat().st_size > 0
            )
            if ok:
                for artifact in (pending_bundle, pending_manifest):
                    with artifact.open("rb") as handle:
                        os.fsync(handle.fileno())
                _sync_preservation_directory(Path(staging))
                journal = bundle.with_suffix(".preserve-journal")
                # One atomic rename makes BOTH durable candidates recoverable.
                # TemporaryDirectory only owns the old name after this point.
                Path(staging).rename(journal)
                _sync_preservation_directory(bundle.parent)
                _finish_preservation_publication(journal, bundle, manifest)
    except OSError as exc:
        return {"ok": False, "error": f"preservation publication failed; retry to recover: {exc}"}
    return {
        "ok": ok,
        "bundle": str(bundle),
        "manifest": str(manifest),
        "commit": head.stdout,
        "verify": verify.stdout or verify.stderr,
        "error": "" if ok else "bundle or manifest missing/empty after write",
    }


def _sync_preservation_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _finish_preservation_publication(journal: Path, bundle: Path, manifest: Path) -> None:
    """Replay publication under the directory lock, including after process death.

    Journal originals survive both renames. Only after both destination entries
    are durable do we remove the journal manifest (the completion marker), then
    clean up. A crash during cleanup can therefore also be recovered safely.
    """
    if journal.is_symlink() or not journal.is_dir():
        raise OSError(f"invalid preservation journal: {journal}")
    saved_manifest = journal / manifest.name
    if saved_manifest.exists():
        for destination in (bundle, manifest):
            source = journal / destination.name
            if source.is_symlink() or not source.is_file():
                raise OSError(f"incomplete preservation journal: {journal}")
            # Keep a durable original in the journal until BOTH outputs exist.
            # Rename replaces aliases instead of writing through them.
            pending = source.with_name(source.name + ".publish")
            pending.unlink(missing_ok=True)
            os.link(source, pending)
            pending.replace(destination)
            _sync_preservation_directory(destination.parent)
        saved_manifest.unlink()
        _sync_preservation_directory(journal)
    shutil.rmtree(journal)
    _sync_preservation_directory(bundle.parent)


def _existing_bundle_conflict(
    bundle: Path, branch: str, head: str, repo_path: Path, runner: Runner,
) -> str:
    """Allow replacing a bundle only by an identical or fast-forwarded copy of the same ref.

    Flattened names collide (feature/x and feature-x, or two checkouts named alike),
    and a rewritten branch would otherwise silently discard the preserved history.
    """
    refusal = f"refusing to overwrite existing preservation {bundle}"
    listed = runner(["git", "bundle", "list-heads", str(bundle)], repo_path, 120)
    heads = listed.stdout.split() if listed.returncode == 0 else []
    if len(heads) != 2 or heads[1] != f"refs/heads/{branch}":
        return f"{refusal}: it holds {listed.stdout or listed.stderr or 'unreadable refs'}"
    if heads[0] == head:
        return ""
    contained = runner(["git", "merge-base", "--is-ancestor", heads[0], head], repo_path, 30)
    if contained.returncode != 0:
        return f"{refusal}: preserved {heads[0]} is not contained in {branch} at {head}"
    return ""


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


def _default_ref(repo_path: Path, runner: Runner) -> tuple[str, str]:
    """Return the default ref, plus why it is unknown when origin/HEAD cannot be read."""
    origin_head = runner(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        repo_path,
        30,
    )
    if origin_head.returncode == 0 and origin_head.stdout:
        return origin_head.stdout, ""
    reason = origin_head.stderr or "origin/HEAD is not set"
    return "origin/main", f"default branch unknown ({reason}); set it with git remote set-head origin <default>"


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
    payload = _json(result.stdout, [])
    if not isinstance(payload, list) or any(not isinstance(pr, dict) for pr in payload):
        return []
    return payload


def _merge_commit_oid(pr: dict[str, Any]) -> str | None:
    merge = pr.get("mergeCommit")
    oid = merge.get("oid") if isinstance(merge, dict) else merge
    return oid if isinstance(oid, str) and oid else None


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
