from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any

from .git import run_command


ORG_OWNER = os.environ.get("GIT_JANITOR_ORG_RUNNER_OWNER", "")
PERSONAL_OWNER = os.environ.get("GIT_JANITOR_HOSTED_OWNER", "")
DEFAULT_OWNER = os.environ.get("GIT_JANITOR_OWNER", "")
DEFAULT_OUTPUT = Path("~/.local/state/git-janitor/repo-facts.json").expanduser()
CODEX_REVIEW_BOT = "chatgpt-codex-connector[bot]"
REPO_INVENTORY_LIMIT = 200
ORG_RUNNER_LIMIT = 100
SCHEMA_VERSION = 3

Runner = Any


@dataclass(frozen=True)
class RepoMeta:
    name: str
    visibility: str
    default_branch: str | None
    full_name: str | None = None
    archived: bool | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fresh_enough(checked_at: str | None, *, max_age: timedelta, now: datetime | None = None) -> bool:
    if not checked_at:
        return False
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    current = now or datetime.now(timezone.utc)
    return current - checked <= max_age


def load_cache(path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "owner": DEFAULT_OWNER, "repos": {}}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_cache(cache: dict[str, Any], path: Path = DEFAULT_OUTPUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect_all(
    *,
    owner: str = DEFAULT_OWNER,
    runner: Runner = run_command,
    timeout: int = 60,
) -> dict[str, Any]:
    metas = list_repo_meta(owner=owner, runner=runner, timeout=timeout)
    generated_at = utc_now()
    pool = (
        read_org_runner_pool(owner=owner, runner=runner, timeout=timeout)
        if uses_org_runner_pool(owner)
        else None
    )
    repos = {
        meta.name: collect_one(
            owner=owner,
            name=meta.name,
            meta=meta,
            pool=pool,
            runner=runner,
            timeout=timeout,
        )
        for meta in metas
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "owner": owner,
        "generated_at": generated_at,
        "repos": repos,
    }


def refresh_one(
    *,
    name: str,
    owner: str = DEFAULT_OWNER,
    output: Path = DEFAULT_OUTPUT,
    runner: Runner = run_command,
    timeout: int = 60,
) -> dict[str, Any]:
    cache = load_cache(output)
    cache["schema_version"] = SCHEMA_VERSION
    cache["owner"] = owner
    cache["generated_at"] = utc_now()
    cache.setdefault("repos", {})
    cache["repos"][name] = collect_one(owner=owner, name=name, runner=runner, timeout=timeout)
    write_cache(cache, output)
    return cache["repos"][name]


def list_repo_meta(
    *,
    owner: str,
    runner: Runner = run_command,
    timeout: int = 60,
    include_archived: bool = False,
) -> list[RepoMeta]:
    fields = "name,visibility,defaultBranchRef"
    if include_archived:
        fields = "name,nameWithOwner,isArchived,visibility,defaultBranchRef"
    args = [
        "gh",
        "repo",
        "list",
        owner,
    ]
    if not include_archived:
        args.append("--no-archived")
    args.extend(
        [
            "--limit",
            str(REPO_INVENTORY_LIMIT + 1),
            "--json",
            fields,
        ]
    )
    result = runner(
        args,
        None,
        timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "gh repo list failed")
    try:
        raw = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("gh repo list returned malformed JSON") from exc
    if not isinstance(raw, list):
        raise RuntimeError("gh repo list returned a non-list inventory")
    if len(raw) > REPO_INVENTORY_LIMIT:
        raise RuntimeError(
            f"gh repo list exceeded the {REPO_INVENTORY_LIMIT}-repository safety cap"
        )
    try:
        return [
            _meta_from_repo(
                item,
                owner=owner if include_archived else None,
                require_inventory_fields=include_archived,
            )
            for item in raw
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("gh repo list returned an incomplete repository record") from exc


def collect_one(
    *,
    owner: str,
    name: str,
    meta: RepoMeta | None = None,
    pool: dict[str, Any] | None = None,
    runner: Runner = run_command,
    timeout: int = 60,
) -> dict[str, Any]:
    meta = meta or read_repo_meta(owner=owner, name=name, runner=runner, timeout=timeout)
    if not meta.default_branch:
        raise RuntimeError(f"GitHub repository {owner}/{name} has no default branch")
    checked_at = utc_now()
    protection = read_branch_protection(
        owner=owner,
        name=name,
        default_branch=meta.default_branch,
        runner=runner,
        timeout=timeout,
    )
    return {
        "name": name,
        "full_name": f"{owner}/{name}",
        "visibility": meta.visibility,
        "default_branch": meta.default_branch,
        "checked_at": checked_at,
        "protected": protection["protected"],
        "required_checks": protection["required_checks"],
        "protection_error": protection.get("error"),
        "codex_review_seen_recently": read_codex_review_seen(
            owner=owner,
            name=name,
            runner=runner,
            timeout=timeout,
        ),
        "runner": read_runner_status(
            owner=owner,
            name=name,
            pool=pool,
            runner=runner,
            timeout=timeout,
        ),
    }


def read_repo_meta(
    *,
    owner: str,
    name: str,
    runner: Runner = run_command,
    timeout: int = 60,
) -> RepoMeta:
    result = runner(
        [
            "gh",
            "repo",
            "view",
            f"{owner}/{name}",
            "--json",
            "name,visibility,defaultBranchRef",
        ],
        None,
        timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or f"gh repo view failed for {owner}/{name}")
    return _meta_from_repo(_json(result.stdout, {}))


def read_branch_protection(
    *,
    owner: str,
    name: str,
    default_branch: str,
    runner: Runner = run_command,
    timeout: int = 60,
) -> dict[str, Any]:
    result = runner(
        ["gh", "api", f"repos/{owner}/{name}/branches/{default_branch}/protection"],
        None,
        timeout,
    )
    if result.returncode != 0:
        return {
            "protected": False,
            "required_checks": [],
            "error": result.stderr or "branch protection unavailable",
        }
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("branch protection returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("branch protection returned a non-object response")
    required = payload.get("required_status_checks")
    if required is None:
        return {"protected": True, "required_checks": []}
    if not isinstance(required, dict):
        raise RuntimeError("required status checks are malformed")
    checks = required.get("checks", [])
    contexts = required.get("contexts", [])
    if not isinstance(checks, list) or not isinstance(contexts, list):
        raise RuntimeError("required status-check inventory is malformed")

    required_pairs: set[tuple[str, int | None]] = set()
    app_bound_contexts: set[str] = set()
    for item in checks:
        if not isinstance(item, dict) or set(item) != {"context", "app_id"}:
            raise RuntimeError("required status-check record is malformed")
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
            or (context, app_id) in required_pairs
        ):
            raise RuntimeError("required status-check record is malformed or duplicated")
        required_pairs.add((context, app_id))
        app_bound_contexts.add(context)
    seen_legacy_contexts: set[str] = set()
    for context in contexts:
        if (
            not isinstance(context, str)
            or not context
            or context.strip() != context
            or context in seen_legacy_contexts
        ):
            raise RuntimeError("required status context is malformed or duplicated")
        seen_legacy_contexts.add(context)
        if context not in app_bound_contexts:
            required_pairs.add((context, None))
    required_checks = [
        {"context": context, "app_id": app_id}
        for context, app_id in sorted(
            required_pairs,
            key=lambda item: (item[0], -1 if item[1] is None else item[1]),
        )
    ]
    return {"protected": True, "required_checks": required_checks}


def read_codex_review_seen(
    *,
    owner: str,
    name: str,
    runner: Runner = run_command,
    timeout: int = 60,
    limit: int = 20,
) -> bool | None:
    prs = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{owner}/{name}",
            "--state",
            "all",
            "--limit",
            str(limit),
            "--json",
            "number",
        ],
        None,
        timeout,
    )
    if prs.returncode != 0:
        return None
    for item in _json(prs.stdout, []):
        number = item.get("number") if isinstance(item, dict) else None
        if not number:
            continue
        reviews = runner(
            ["gh", "api", f"repos/{owner}/{name}/pulls/{number}/reviews"],
            None,
            timeout,
        )
        if reviews.returncode != 0:
            continue
        for review in _json(reviews.stdout, []):
            user = review.get("user") if isinstance(review, dict) else None
            login = user.get("login") if isinstance(user, dict) else None
            if login == CODEX_REVIEW_BOT or (login and "chatgpt-codex" in login):
                return True
    return False


def uses_org_runner_pool(owner: str) -> bool:
    """Use an organization pool only when the operator explicitly configured it."""
    return bool(ORG_OWNER) and owner == ORG_OWNER


def read_org_runner_pool(
    *,
    owner: str,
    runner: Runner = run_command,
    timeout: int = 60,
) -> dict[str, Any]:
    result = runner(["gh", "api", f"orgs/{owner}/actions/runners"], None, timeout)
    if result.returncode != 0:
        return {
            "scope": "org-pool",
            "org": owner,
            "registered": False,
            "online": False,
            "online_count": 0,
            "total_count": 0,
            "runners": [],
            "error": result.stderr or "org runner pool unavailable",
        }
    pool = [
        {
            "name": item.get("name"),
            "status": item.get("status"),
            "busy": item.get("busy"),
            "labels": _labels(item),
        }
        for item in (_json(result.stdout, {}).get("runners") or [])[:ORG_RUNNER_LIMIT]
        if isinstance(item, dict) and item.get("name")
    ]
    pool.sort(key=lambda item: item["name"])
    online = [item for item in pool if item["status"] == "online"]
    return {
        "scope": "org-pool",
        "org": owner,
        "registered": bool(pool),
        "online": bool(online),
        "online_count": len(online),
        "total_count": len(pool),
        "runners": pool,
    }


def read_runner_status(
    *,
    owner: str,
    name: str,
    pool: dict[str, Any] | None = None,
    runner: Runner = run_command,
    timeout: int = 60,
) -> dict[str, Any]:
    if PERSONAL_OWNER and owner == PERSONAL_OWNER:
        # Hosted capacity is not represented by the repository runner API.
        # Actual check runs and the spending budget determine availability.
        return {"scope": "github-hosted", "registered": None, "online": None}
    if uses_org_runner_pool(owner):
        return pool if pool is not None else read_org_runner_pool(
            owner=owner,
            runner=runner,
            timeout=timeout,
        )
    expected = os.environ.get("GIT_JANITOR_RUNNER_NAME_PREFIX", "runner-") + name
    result = runner(["gh", "api", f"repos/{owner}/{name}/actions/runners"], None, timeout)
    if result.returncode != 0:
        return {
            "scope": "repo",
            "expected_name": expected,
            "registered": False,
            "online": False,
            "error": result.stderr or "runner list unavailable",
        }
    for item in (_json(result.stdout, {}).get("runners") or []):
        if not isinstance(item, dict) or item.get("name") != expected:
            continue
        return {
            "scope": "repo",
            "expected_name": expected,
            "registered": True,
            "online": item.get("status") == "online",
            "status": item.get("status"),
            "busy": item.get("busy"),
            "labels": _labels(item),
        }
    return {"scope": "repo", "expected_name": expected, "registered": False, "online": False}


def main(argv: list[str] | None = None, *, runner: Runner = run_command) -> int:
    parser = argparse.ArgumentParser(description="Build or query cached GitHub repo facts.")
    parser.add_argument("--owner", default=DEFAULT_OWNER, required=not bool(DEFAULT_OWNER))
    parser.add_argument("--repo", help="Refresh or print one repository entry.")
    parser.add_argument("--refresh", action="store_true", help="Refresh the selected repo.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a summary.")
    args = parser.parse_args(argv)

    if args.repo:
        if args.refresh:
            entry = refresh_one(
                name=args.repo,
                owner=args.owner,
                output=args.output.expanduser(),
                runner=runner,
            )
            _emit(entry, as_json=args.json)
            return 0
        cache = load_cache(args.output.expanduser())
        entry = cache.get("repos", {}).get(args.repo)
        if cache.get("owner") != args.owner or (
            entry and entry.get("full_name") != f"{args.owner}/{args.repo}"
        ):
            entry = None
        if not entry:
            print(f"{args.repo}: no cached entry; rerun with --refresh", flush=True)
            return 2
        _emit(entry, as_json=args.json)
        return 0

    cache = collect_all(owner=args.owner, runner=runner)
    write_cache(cache, args.output.expanduser())
    _emit(cache, as_json=args.json)
    return 0


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if "repos" in payload:
        print(f"wrote facts for {len(payload['repos'])} repos")
    else:
        print(
            f"{payload['full_name']}: protected={payload['protected']} "
            f"checks={','.join(_required_check_label(item) for item in payload['required_checks']) or '-'} "
            f"runner_online={payload['runner'].get('online')}"
        )


def _meta_from_repo(
    raw: dict[str, Any],
    *,
    owner: str | None = None,
    require_inventory_fields: bool = False,
) -> RepoMeta:
    if not isinstance(raw, dict):
        raise TypeError("repository metadata must be an object")
    name = raw["name"]
    visibility = raw["visibility"]
    default = raw["defaultBranchRef"]
    full_name = raw.get("nameWithOwner")
    archived = raw.get("isArchived")
    default_value = default.get("name") if isinstance(default, dict) else None
    default_name = default_value or None
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(visibility, str)
        or not visibility
        or (
            not require_inventory_fields
            and (not isinstance(default_name, str) or not default_name)
        )
        or (
            require_inventory_fields
            and default is not None
            and (
                not isinstance(default, dict)
                or "name" not in default
                or not isinstance(default_value, str)
            )
        )
    ):
        raise ValueError("repository metadata fields are incomplete")
    if require_inventory_fields:
        expected_full_name = f"{owner}/{name}" if owner else None
        if (
            not isinstance(full_name, str)
            or not full_name
            or not isinstance(archived, bool)
            or not expected_full_name
            or full_name.casefold() != expected_full_name.casefold()
        ):
            raise ValueError("repository inventory identity fields are incomplete or inconsistent")
    return RepoMeta(
        name=name,
        visibility=visibility,
        default_branch=default_name,
        full_name=full_name if isinstance(full_name, str) else None,
        archived=archived if isinstance(archived, bool) else None,
    )


def _required_check_label(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        context = value.get("context")
        app_id = value.get("app_id")
        if isinstance(context, str):
            return context if app_id is None else f"{context}@app:{app_id}"
    return "<invalid>"


def _labels(item: dict[str, Any]) -> list[str]:
    return sorted(
        label.get("name")
        for label in (item.get("labels") or [])
        if isinstance(label, dict) and label.get("name")
    )


def _json(text: str, fallback: Any) -> Any:
    try:
        return json.loads(text or "")
    except json.JSONDecodeError:
        return fallback


if __name__ == "__main__":
    raise SystemExit(main())
