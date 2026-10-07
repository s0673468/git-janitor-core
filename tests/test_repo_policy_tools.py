from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_janitor import repo_facts, pr_shepherd
from git_janitor.models import CommandResult
from git_janitor.pr_shepherd import (
    STALL_EVIDENCE_QUERY,
    cached_or_refreshed_facts,
    finding_priority,
    guarded_merge,
    list_threads,
    main as _pr_shepherd_main,
    resolve_thread,
    stalled_auto_merges,
    unresolved_blocking_findings,
)
from git_janitor.repo_facts import (
    DEFAULT_OWNER,
    DEFAULT_OUTPUT,
    collect_all,
    collect_one,
    fresh_enough,
    list_repo_meta,
    main as repo_facts_main,
    read_runner_status,
)
from git_janitor.safe_delete import evaluate_delete, git_safe_delete_main, preserve_branch


ORG_OWNER = "example-org"
PERSONAL_OWNER = "example-owner"


class FixtureOwnerTests(unittest.TestCase):
    """Explicit synthetic operator configuration, restored after every test."""

    def setUp(self):
        configured = patch.multiple(repo_facts, ORG_OWNER=ORG_OWNER, PERSONAL_OWNER=PERSONAL_OWNER)
        configured.start()
        self.addCleanup(configured.stop)
        owner = patch.object(pr_shepherd, "DEFAULT_OWNER", PERSONAL_OWNER)
        owner.start()
        self.addCleanup(owner.stop)


FIXTURES = Path(__file__).parent / "fixtures" / "agent_ops"
REPO_PATH = Path("/repo")
TEST_RUN_ID = "d" * 64
TEST_RUNTIME_SHA = "a" * 40
TEST_MANIFEST_SHA = "c" * 64
TEST_TARGET_SHA = "e" * 64
TEST_GH_SHA = "f" * 64
TEST_HERMES_RUNTIME_SHA = "b" * 40
TEST_HERMES_SHA = "1" * 64
TEST_HERMES_MANIFEST_SHA = "9" * 64
TEST_HEAD_SHA = "2" * 40
TEST_BASE_SHA = "3" * 40
TEST_MERGE_SHA = "4" * 40


class RepoFactsPathTests(unittest.TestCase):
    def test_default_owner_requires_explicit_operator_configuration(self) -> None:
        self.assertEqual(DEFAULT_OWNER, "")

    def test_default_query_rejects_old_organization_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            facts = Path(tmp) / "repo-facts.json"
            facts.write_text(json.dumps({
                "owner": ORG_OWNER,
                "repos": {"git-janitor": {"full_name": f"{ORG_OWNER}/git-janitor"}},
            }))
            output = io.StringIO()
            with redirect_stdout(output):
                status = repo_facts_main(
                    ["--owner", PERSONAL_OWNER, "--repo", "git-janitor", "--output", str(facts)]
                )
        self.assertEqual(status, 2)
        self.assertIn("no cached entry", output.getvalue())

    def test_default_cache_uses_neutral_agent_policy_authority(self) -> None:
        self.assertEqual(
            DEFAULT_OUTPUT,
            Path.home() / ".local" / "state" / "git-janitor" / "repo-facts.json",
        )
        self.assertNotIn(".claude", str(DEFAULT_OUTPUT))


def pr_shepherd_main(
    argv: list[str], *, runner: object
) -> int:
    bound = list(argv)
    if bound[:1] == ["watch"] and "--receipt-path" in bound:
        bound.extend(
            [
                "--run-id",
                TEST_RUN_ID,
                "--runtime-sha",
                TEST_RUNTIME_SHA,
                "--runtime-manifest-sha256",
                TEST_MANIFEST_SHA,
                "--destination-target-sha256",
                TEST_TARGET_SHA,
                "--gh-executable-sha256",
                TEST_GH_SHA,
                "--hermes-runtime-sha",
                TEST_HERMES_RUNTIME_SHA,
                "--hermes-executable-sha256",
                TEST_HERMES_SHA,
                "--hermes-runtime-manifest-sha256",
                TEST_HERMES_MANIFEST_SHA,
            ]
        )
    return _pr_shepherd_main(bound, runner=runner)


def _merged_pr_list_prefix(*, repo: str = "owner/repo") -> list[str]:
    return [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--search",
    ]


def _stall_evidence_prefix() -> list[str]:
    return ["gh", "api", "graphql", "-f", f"query={STALL_EVIDENCE_QUERY}"]


def _add_empty_grok_receipts(fake: "FakeRunner", *, pr: int = 7) -> None:
    for _ in range(2):
        fake.add(
            [
                "gh",
                "api",
                f"repos/owner/repo/issues/{pr}/comments?per_page=100",
                "--paginate",
                "--slurp",
            ],
            stdout="[[]]",
        )


def _add_direct_merge_success(fake: "FakeRunner", *, pr: int = 7) -> None:
    fake.add(
        [
            "gh",
            "pr",
            "checks",
            str(pr),
            "--repo",
            "owner/repo",
            "--required",
            "--json",
            "bucket,state",
        ],
        stdout=json.dumps([{"bucket": "pass", "state": "SUCCESS"}]),
    )
    fake.add(
        [
            "gh",
            "api",
            "--method",
            "PUT",
            f"repos/owner/repo/pulls/{pr}/merge",
            "-f",
            f"sha={TEST_HEAD_SHA}",
            "-f",
            "merge_method=squash",
        ],
        stdout=json.dumps({"merged": True, "sha": TEST_MERGE_SHA}),
    )
    fake.add(
        ["gh", "api", f"repos/owner/repo/git/commits/{TEST_MERGE_SHA}"],
        stdout=json.dumps({"parents": [{"sha": TEST_BASE_SHA}]}),
    )
    fake.add(
        ["gh", "api", f"repos/owner/repo/pulls/{pr}"],
        stdout=json.dumps(
            {
                "merged": True,
                "head": {
                    "ref": "codex/luna-review",
                    "sha": TEST_HEAD_SHA,
                    "repo": {"full_name": "owner/repo"},
                },
            }
        ),
    )


def _check_run(
    name: str,
    *,
    created_at: str,
    app_id: int = 1,
    status: str = "QUEUED",
    conclusion: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> dict[str, object]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": started_at,
        "completedAt": completed_at,
        "checkSuite": {"createdAt": created_at},
        "_fixture_app_id": app_id,
    }


def _stall_evidence(
    *,
    number: int = 8,
    enabled_at: str,
    head_at: str,
    contexts: list[dict[str, object]] | None = None,
    head_sha: str = TEST_HEAD_SHA,
    commit_sha: str | None = None,
    has_next_page: bool = False,
    auto_merge: bool = True,
) -> str:
    check_runs = [
        context for context in (contexts or []) if context.get("__typename") == "CheckRun"
    ]
    status_contexts = [
        context
        for context in (contexts or [])
        if context.get("__typename") == "StatusContext"
    ]
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": number,
                        "headRefOid": head_sha,
                        "autoMergeRequest": (
                            {"enabledAt": enabled_at} if auto_merge else None
                        ),
                        "commits": {
                            "totalCount": 1,
                            "nodes": [
                                {
                                    "commit": {
                                        "oid": commit_sha or head_sha,
                                        "pushedDate": head_at,
                                        "committedDate": head_at,
                                        "checkSuites": {
                                            "pageInfo": {
                                                "hasNextPage": has_next_page,
                                                "endCursor": "cursor" if has_next_page else None,
                                            },
                                            "nodes": [
                                                {
                                                    "createdAt": run["checkSuite"]["createdAt"],
                                                    "checkRuns": {
                                                        "pageInfo": {
                                                            "hasNextPage": False,
                                                            "endCursor": None,
                                                        },
                                                        "nodes": [
                                                            {
                                                                key: value
                                                                for key, value in run.items()
                                                                if key != "_fixture_app_id"
                                                            }
                                                        ],
                                                    },
                                                    "app": {
                                                        "databaseId": run[
                                                            "_fixture_app_id"
                                                        ]
                                                    },
                                                }
                                                for run in check_runs
                                            ],
                                        },
                                        "status": (
                                            {"contexts": status_contexts}
                                            if status_contexts
                                            else None
                                        ),
                                    }
                                }
                            ],
                        },
                    }
                }
            }
        }
    )


class RepoFactsTests(FixtureOwnerTests):
    def test_collect_one_records_protection_reviews_and_org_runner_pool(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "repo",
                "view",
                f"{ORG_OWNER}/git-janitor",
                "--json",
                "name,visibility,defaultBranchRef",
            ],
            stdout=json.dumps(
                {
                    "name": "git-janitor",
                    "visibility": "PRIVATE",
                    "defaultBranchRef": {"name": "main"},
                }
            ),
        )
        fake.add(
            ["gh", "api", f"repos/{ORG_OWNER}/git-janitor/branches/main/protection"],
            stdout=json.dumps(
                {
                    "required_status_checks": {
                        "checks": [
                            {"context": "test", "app_id": 15368},
                            {"context": "test", "app_id": 99},
                        ],
                        "contexts": ["workflow-lint"],
                    }
                }
            ),
        )
        fake.add(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                f"{ORG_OWNER}/git-janitor",
                "--state",
                "all",
                "--limit",
                "20",
                "--json",
                "number",
            ],
            stdout=json.dumps([{"number": 33}]),
        )
        fake.add(
            ["gh", "api", f"repos/{ORG_OWNER}/git-janitor/pulls/33/reviews"],
            stdout=json.dumps([{"user": {"login": "chatgpt-codex-connector[bot]"}}]),
        )
        fake.add(
            ["gh", "api", f"orgs/{ORG_OWNER}/actions/runners"],
            stdout=_org_pool_payload(online=2, total=3),
        )

        facts = collect_one(owner=ORG_OWNER, name="git-janitor", runner=fake)

        self.assertTrue(facts["protected"])
        self.assertEqual(
            facts["required_checks"],
            [
                {"context": "test", "app_id": 99},
                {"context": "test", "app_id": 15368},
                {"context": "workflow-lint", "app_id": None},
            ],
        )
        self.assertTrue(facts["codex_review_seen_recently"])
        self.assertEqual(facts["runner"]["scope"], "org-pool")
        self.assertEqual(facts["runner"]["org"], ORG_OWNER)
        self.assertTrue(facts["runner"]["online"])
        self.assertEqual(facts["runner"]["online_count"], 2)
        self.assertEqual(facts["runner"]["total_count"], 3)
        self.assertEqual(
            [item["name"] for item in facts["runner"]["runners"]],
            ["example-org-runner-1", "example-org-runner-2", "example-org-runner-3"],
        )
        self.assertNotIn("expected_name", facts["runner"])

    def test_org_pool_offline_when_every_pool_runner_is_offline(self) -> None:
        fake = FakeRunner()
        fake.add(
            ["gh", "api", f"orgs/{ORG_OWNER}/actions/runners"],
            stdout=_org_pool_payload(online=0, total=3),
        )

        status = read_runner_status(owner=ORG_OWNER, name="git-janitor", runner=fake)

        self.assertFalse(status["online"])
        self.assertTrue(status["registered"])
        self.assertEqual(status["online_count"], 0)
        self.assertEqual(status["total_count"], 3)

    def test_org_pool_error_reports_offline_without_raising(self) -> None:
        fake = FakeRunner()
        fake.add(
            ["gh", "api", f"orgs/{ORG_OWNER}/actions/runners"],
            stderr="HTTP 403",
            returncode=1,
        )

        status = read_runner_status(owner=ORG_OWNER, name="git-janitor", runner=fake)

        self.assertFalse(status["online"])
        self.assertFalse(status["registered"])
        self.assertEqual(status["error"], "HTTP 403")

    def test_personal_owner_uses_hosted_actions_without_per_repo_runner_probe(self) -> None:
        fake = FakeRunner()
        status = read_runner_status(owner=PERSONAL_OWNER, name="git-janitor", runner=fake)

        self.assertEqual(status["scope"], "github-hosted")
        self.assertIsNone(status["online"])
        self.assertEqual(fake.calls, [])

    def test_collect_all_reads_the_org_runner_pool_once(self) -> None:
        fake = FakeRunner()
        names = ["alpha", "beta"]
        fake.add(
            [
                "gh",
                "repo",
                "list",
                ORG_OWNER,
                "--no-archived",
                "--limit",
                "201",
                "--json",
                "name,visibility,defaultBranchRef",
            ],
            stdout=json.dumps(
                [
                    {
                        "name": name,
                        "visibility": "PRIVATE",
                        "defaultBranchRef": {"name": "main"},
                    }
                    for name in names
                ]
            ),
        )
        fake.add(
            ["gh", "api", f"orgs/{ORG_OWNER}/actions/runners"],
            stdout=_org_pool_payload(),
        )
        for name in names:
            fake.add(
                ["gh", "api", f"repos/{ORG_OWNER}/{name}/branches/main/protection"],
                stdout=json.dumps({"required_status_checks": {"checks": []}}),
            )
            fake.add(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    f"{ORG_OWNER}/{name}",
                    "--state",
                    "all",
                    "--limit",
                    "20",
                    "--json",
                    "number",
                ],
                stdout="[]",
            )

        cache = collect_all(owner=ORG_OWNER, runner=fake)

        pool_calls = [
            call for call in fake.calls if call[:3] == ["gh", "api", f"orgs/{ORG_OWNER}/actions/runners"]
        ]
        self.assertEqual(len(pool_calls), 1)
        self.assertEqual(cache["owner"], ORG_OWNER)
        self.assertEqual(cache["schema_version"], 3)
        self.assertTrue(cache["repos"]["alpha"]["runner"]["online"])
        self.assertTrue(cache["repos"]["beta"]["runner"]["online"])

    def test_fresh_enough_handles_missing_or_stale_timestamps(self) -> None:
        now = datetime(2026, 7, 8, tzinfo=timezone.utc)
        self.assertFalse(fresh_enough(None, max_age=timedelta(days=7), now=now))
        self.assertFalse(fresh_enough("not-a-date", max_age=timedelta(days=7), now=now))
        self.assertTrue(
            fresh_enough(
                "2026-07-07T12:00:00+00:00",
                max_age=timedelta(days=7),
                now=now,
            )
        )

    def test_repo_inventory_rejects_non_objects_and_missing_fields(self) -> None:
        command = [
            "gh",
            "repo",
            "list",
            "owner",
            "--no-archived",
            "--limit",
            "201",
            "--json",
            "name,visibility,defaultBranchRef",
        ]
        for payload in (["not-an-object"], [{"name": "repo"}], {"name": "repo"}):
            with self.subTest(payload=payload):
                fake = FakeRunner()
                fake.add(command, stdout=json.dumps(payload))
                with self.assertRaises(RuntimeError):
                    list_repo_meta(owner="owner", runner=fake)

    def test_repo_inventory_detects_cap_saturation(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "repo",
                "list",
                "owner",
                "--no-archived",
                "--limit",
                "201",
                "--json",
                "name,visibility,defaultBranchRef",
            ],
            stdout=json.dumps(
                [
                    {
                        "name": f"repo-{index}",
                        "visibility": "PRIVATE",
                        "defaultBranchRef": {"name": "main"},
                    }
                    for index in range(201)
                ]
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "200-repository safety cap"):
            list_repo_meta(owner="owner", runner=fake)

    def test_repo_inventory_can_include_archived_with_full_identity(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "repo",
                "list",
                "owner",
                "--limit",
                "201",
                "--json",
                "name,nameWithOwner,isArchived,visibility,defaultBranchRef",
            ],
            stdout=json.dumps(
                [
                    {
                        "name": "archive",
                        "nameWithOwner": "owner/archive",
                        "isArchived": True,
                        "visibility": "PRIVATE",
                        "defaultBranchRef": {"name": "main"},
                    },
                    {
                        "name": "empty-archive",
                        "nameWithOwner": "owner/empty-archive",
                        "isArchived": True,
                        "visibility": "PRIVATE",
                        "defaultBranchRef": {"name": ""},
                    },
                ]
            ),
        )

        repos = list_repo_meta(owner="owner", runner=fake, include_archived=True)

        self.assertEqual(repos[0].full_name, "owner/archive")
        self.assertTrue(repos[0].archived)
        self.assertIsNone(repos[1].default_branch)


class PrShepherdTests(FixtureOwnerTests):
    def test_orphans_compatibility_command_is_an_empty_no_query_result(self) -> None:
        fake = FakeRunner()
        out = io.StringIO()
        with redirect_stdout(out):
            status = pr_shepherd_main(["orphans", "--owner", "owner"], runner=fake)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue()), [])
        self.assertEqual(fake.calls, [])

    def test_threads_returns_compact_json_records(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                _query_prefix(
                    'query { repository(owner: "owner", name: "repo") { pullRequest(number: 4)'
                ),
            ],
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [
                                        {
                                            "id": "T1",
                                            "isResolved": False,
                                            "isOutdated": False,
                                            "path": "README.md",
                                            "line": 10,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "databaseId": 99,
                                                        "author": {"login": "bot"},
                                                        "body": "Please fix",
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            ),
            prefix=True,
        )

        threads = list_threads(repo="owner/repo", pr=4, unresolved=True, runner=fake)

        self.assertEqual(threads[0]["thread_id"], "T1")
        self.assertEqual(threads[0]["comment_id"], 99)
        self.assertEqual(threads[0]["path"], "README.md")

    def test_list_threads_raises_on_graphql_error_payload(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                _query_prefix(
                    'query { repository(owner: "owner", name: "repo") { pullRequest(number: 4)'
                ),
            ],
            stdout=json.dumps(
                {"data": {"repository": None}, "errors": [{"message": "forbidden"}]}
            ),
            prefix=True,
        )

        with self.assertRaises(RuntimeError):
            list_threads(repo="owner/repo", pr=4, unresolved=False, runner=fake)

    def test_list_threads_raises_on_missing_pull_request_data(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                _query_prefix(
                    'query { repository(owner: "owner", name: "repo") { pullRequest(number: 4)'
                ),
            ],
            stdout=json.dumps({"data": {"repository": {"pullRequest": None}}}),
            prefix=True,
        )

        with self.assertRaises(RuntimeError):
            list_threads(repo="owner/repo", pr=4, unresolved=False, runner=fake)

    def test_list_threads_raises_on_incomplete_thread_record(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                _query_prefix(
                    'query { repository(owner: "owner", name: "repo") { pullRequest(number: 4)'
                ),
            ],
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [{"id": "missing-comments"}],
                                }
                            }
                        }
                    }
                }
            ),
            prefix=True,
        )

        with self.assertRaises(RuntimeError):
            list_threads(repo="owner/repo", pr=4, unresolved=False, runner=fake)

    def test_list_threads_paginates_past_first_page(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                _query_prefix(
                    'query { repository(owner: "owner", name: "repo") { pullRequest(number: 4) '
                    "{ reviewThreads(first: 100)"
                ),
            ],
            stdout=_threads_payload(
                [_bot_thread("T1", "P2", "First page finding")],
                has_next_page=True,
                end_cursor="C1",
            ),
            prefix=True,
        )
        fake.add(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                _query_prefix(
                    'query { repository(owner: "owner", name: "repo") { pullRequest(number: 4) '
                    '{ reviewThreads(first: 100, after: "C1")'
                ),
            ],
            stdout=_threads_payload([_bot_thread("T2", "P1", "Second page finding")]),
            prefix=True,
        )

        threads = list_threads(repo="owner/repo", pr=4, unresolved=True, runner=fake)

        self.assertEqual([thread["thread_id"] for thread in threads], ["T1", "T2"])

    def test_list_threads_rejects_repeated_pagination_cursor(self) -> None:
        fake = FakeRunner()
        fake.add(
            ["gh", "api", "graphql"],
            stdout=_threads_payload(
                [_bot_thread("T1", "P2", "First page finding")],
                has_next_page=True,
                end_cursor="C1",
            ),
            prefix=True,
        )

        with self.assertRaisesRegex(RuntimeError, "cursor did not advance"):
            list_threads(repo="owner/repo", pr=4, unresolved=True, runner=fake)

        graphql_calls = [call for call in fake.calls if call[:3] == ["gh", "api", "graphql"]]
        self.assertEqual(len(graphql_calls), 2)

    def test_resolve_refuses_empty_reply(self) -> None:
        fake = FakeRunner()
        with redirect_stderr(io.StringIO()):
            status = resolve_thread(repo="owner/repo", pr=4, thread_id="T1", reply=" ", runner=fake)
        self.assertEqual(status, 2)
        self.assertEqual(fake.calls, [])

    def test_resolve_replies_using_pull_number_endpoint(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                'query=query { node(id:"T1")',
            ],
            stdout=json.dumps(
                {
                    "data": {
                        "node": {
                            "comments": {
                                "nodes": [
                                    {
                                        "databaseId": 99,
                                    }
                                ]
                            }
                        }
                    }
                }
            ),
            prefix=True,
        )
        fake.add(
            [
                "gh",
                "api",
                "repos/owner/repo/pulls/4/comments/99/replies",
                "-f",
                "body=fixed",
            ]
        )
        fake.add(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                'query=mutation { resolveReviewThread(input:{threadId:"T1"})',
            ],
            prefix=True,
            stdout=json.dumps(
                {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {
                                "isResolved": True,
                            }
                        }
                    }
                }
            ),
        )

        with redirect_stdout(io.StringIO()):
            status = resolve_thread(repo="owner/repo", pr=4, thread_id="T1", reply="fixed", runner=fake)

        self.assertEqual(status, 0)

    def test_guarded_merge_refuses_ungated_repo(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            facts = Path(tmp) / "repo-facts.json"
            facts.write_text(
                json.dumps(
                    {
                        "repos": {
                            "repo": {
                                "full_name": "owner/repo",
                                "visibility": "PRIVATE",
                                "checked_at": datetime.now(timezone.utc).isoformat(),
                                "protected": False,
                                "required_checks": [],
                                "runner": {"online": True},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            fake.add(
                ["gh", "pr", "view", "7", "--repo", "owner/repo", "--json", "number,baseRefName,baseRefOid,headRefOid,url,title,state,isDraft"],
                stdout=json.dumps(
                    {
                        "number": 7, "state": "OPEN", "isDraft": False,
                        "baseRefName": "main",
                        "baseRefOid": TEST_BASE_SHA,
                        "headRefOid": TEST_HEAD_SHA,
                    }
                ),
            )

            with redirect_stderr(io.StringIO()):
                status = guarded_merge(
                    repo="owner/repo",
                    pr=7,
                    facts_path=facts,
                    alert_command=Path("/missing/fleet-send"),
                    runner=fake,
                )

        self.assertEqual(status, 3)
        self.assertNotIn(["gh", "pr", "merge", "7"], fake.calls)

    def test_guarded_merge_writes_ledger_on_success(self) -> None:
        fake = FakeRunner()
        fake.add(["gh", "pr", "checks", "7", "--repo", "owner/repo", "--required", "--json", "bucket,state"],
                 returncode=8, stdout='[{"bucket":"pending"}]', prefix=True)
        with tempfile.TemporaryDirectory() as tmp:
            facts = Path(tmp) / "repo-facts.json"
            ledger = Path(tmp) / "merge.jsonl"
            facts.write_text(
                json.dumps(
                    {
                        "repos": {
                            "repo": {
                                "full_name": "owner/repo",
                                "visibility": "PRIVATE",
                                "checked_at": datetime.now(timezone.utc).isoformat(),
                                "protected": True,
                                "required_checks": ["test"],
                                "runner": {"online": True},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            fake.add(
                ["gh", "pr", "view", "7", "--repo", "owner/repo", "--json", "number,baseRefName,baseRefOid,headRefOid,url,title,state,isDraft"],
                stdout=json.dumps(
                    {
                        "number": 7, "state": "OPEN", "isDraft": False,
                        "baseRefName": "main",
                        "baseRefOid": TEST_BASE_SHA,
                        "headRefOid": TEST_HEAD_SHA,
                    }
                ),
            )
            _add_empty_grok_receipts(fake)
            fake.add(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    _query_prefix(
                        'query { repository(owner: "owner", name: "repo") { pullRequest(number: 7)'
                    ),
                ],
                stdout=_threads_payload([]),
                prefix=True,
            )
            fake.add(
                [
                    "gh",
                    "pr",
                    "merge",
                    "7",
                    "--auto",
                    "--squash",
                    "--match-head-commit",
                    TEST_HEAD_SHA,
                    "--repo",
                    "owner/repo",
                ],
                stdout="merge scheduled",
            )

            with redirect_stdout(io.StringIO()):
                status = guarded_merge(
                    repo="owner/repo",
                    pr=7,
                    facts_path=facts,
                    alert_command=Path("/missing/fleet-send"),
                    ledger_path=ledger,
                    runner=fake,
                )

            entry = json.loads(ledger.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(entry["status"], "armed")
        self.assertEqual(entry["command"][:3], ["gh", "pr", "merge"])

    def test_guarded_merge_runner_alert_uses_alias_and_delivery_failure_does_not_mutate_policy(self) -> None:
        fake = FakeRunner()
        fake.add(["gh", "pr", "checks", "7", "--repo", "owner/repo", "--required", "--json", "bucket,state"],
                 returncode=8, stdout='[{"bucket":"pending"}]', prefix=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = tmp_path / "repo-facts.json"
            facts.write_text(
                json.dumps(
                    {
                        "repos": {
                            "repo": {
                                "full_name": "owner/repo",
                                "visibility": "PRIVATE",
                                "checked_at": datetime.now(timezone.utc).isoformat(),
                                "protected": True,
                                "required_checks": ["test"],
                                "runner": {
                                    "scope": "org-pool",
                                    "org": ORG_OWNER,
                                    "online": False,
                                    "registered": True,
                                    "online_count": 0,
                                    "total_count": 3,
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            alert = tmp_path / "fleet-send"
            alert.write_text(
                "#!/bin/sh\n"
                f'printf \'%s\\n\' "$@" > "{tmp_path / "alert-args.txt"}"\n'
                "cat >/dev/null\n"
                "exit 1\n",
                encoding="utf-8",
            )
            alert.chmod(0o755)
            fake.add(
                ["gh", "pr", "view", "7", "--repo", "owner/repo", "--json", "number,baseRefName,baseRefOid,headRefOid,url,title,state,isDraft"],
                stdout=json.dumps(
                    {
                        "number": 7, "state": "OPEN", "isDraft": False,
                        "baseRefName": "main",
                        "baseRefOid": TEST_BASE_SHA,
                        "headRefOid": TEST_HEAD_SHA,
                    }
                ),
            )
            _add_empty_grok_receipts(fake)
            fake.add(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    _query_prefix(
                        'query { repository(owner: "owner", name: "repo") { pullRequest(number: 7)'
                    ),
                ],
                stdout=_threads_payload([]),
                prefix=True,
            )
            fake.add(
                [
                    "gh",
                    "pr",
                    "merge",
                    "7",
                    "--auto",
                    "--squash",
                    "--match-head-commit",
                    TEST_HEAD_SHA,
                    "--repo",
                    "owner/repo",
                ],
                stdout="merge scheduled",
            )

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = guarded_merge(
                    repo="owner/repo",
                    pr=7,
                    facts_path=facts,
                    alert_command=alert,
                    alert_destination_alias="fleet-ops",
                    runner=fake,
                )
            args = (tmp_path / "alert-args.txt").read_text(encoding="utf-8").splitlines()

        self.assertEqual(status, 0)
        self.assertIn("--destination-alias", args)
        self.assertIn("fleet-ops", args)
        subject = args[args.index("--subject") + 1]
        self.assertIn(f"{ORG_OWNER} runner pool (0 of 3 online) offline", subject)
        self.assertNotIn("runner-repo", subject)
        self.assertTrue(any(call[:3] == ["gh", "pr", "merge"] for call in fake.calls))

    def test_finding_priority_parses_codex_badge_markup(self) -> None:
        badge = (
            "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)"
            "</sub></sub>  Preserve newer queued quest edits**"
        )
        self.assertEqual(finding_priority(badge), "P1")
        self.assertEqual(finding_priority("![P2 Badge](x) Guard the sync path"), "P2")
        self.assertIsNone(finding_priority("Nice cleanup, thanks!"))
        self.assertIsNone(finding_priority(None))

    def test_only_p1_codex_threads_block_from_agent_ops_fixtures(self) -> None:
        cases = json.loads(
            (FIXTURES / "pr_shepherd_one_pass_review.json").read_text(
                encoding="utf-8"
            )
        )["thread_priorities"]
        for case in cases:
            with self.subTest(priority=case["priority"]):
                fake = FakeRunner()
                fake.add(
                    ["gh", "api", "graphql"],
                    stdout=_threads_payload(
                        [
                            _bot_thread(
                                "T1",
                                case["priority"],
                                "Concrete shipped failure",
                            )
                        ]
                    ),
                    prefix=True,
                )

                findings = unresolved_blocking_findings(
                    repo="owner/repo",
                    pr=7,
                    runner=fake,
                )

                self.assertEqual(bool(findings), case["blocking"])












    def test_stalled_auto_merges_reports_pending_old_prs(self) -> None:
        fake = FakeRunner()
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(
            microsecond=0
        ).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            facts = Path(tmp) / "repo-facts.json"
            facts.write_text(
                json.dumps(
                    {
                        "repos": {
                            "repo": {
                                "full_name": "owner/repo",
                                "required_checks": ["test"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            fake.add(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    "owner/repo",
                    "--state",
                    "open",
                    "--limit",
                    "201",
                    "--json",
                    "number,title,url,autoMergeRequest",
                ],
                stdout=json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Waiting",
                            "url": "https://github.com/owner/repo/pull/8",
                            "autoMergeRequest": {"enabledAt": old},
                        }
                    ]
                ),
            )
            fake.add(
                _stall_evidence_prefix(),
                stdout=_stall_evidence(
                    enabled_at=old,
                    head_at=old,
                    contexts=[_check_run("test", created_at=old)],
                ),
                prefix=True,
            )

            stalls = stalled_auto_merges(
                owner="owner",
                facts_path=facts,
                min_age=timedelta(hours=2),
                runner=fake,
            )

        self.assertEqual(stalls[0]["number"], 8)
        self.assertEqual(stalls[0]["check_status"], "pending")
        self.assertEqual(stalls[0]["age_source"], "check")
        self.assertEqual(stalls[0]["head_sha"], TEST_HEAD_SHA)
        self.assertNotIn("updatedAt", fake.calls[0][-1])

    def test_stall_age_uses_missing_required_check_head_time_and_latest_rerun(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        old = (now - timedelta(hours=4)).isoformat()
        fresh = (now - timedelta(minutes=10)).isoformat()
        success = _check_run(
            "test",
            created_at=old,
            status="COMPLETED",
            conclusion="SUCCESS",
            started_at=old,
            completed_at=old,
        )
        scenarios = (
            (
                "missing-required",
                ["test", "workflow-lint"],
                old,
                [success],
                True,
                ["workflow-lint"],
            ),
            (
                "latest-rerun",
                ["test"],
                old,
                [
                    _check_run("test", created_at=old),
                    _check_run("test", created_at=fresh),
                ],
                False,
                [],
            ),
            (
                "new-head-missing",
                ["test"],
                fresh,
                [],
                False,
                [],
            ),
        )
        for name, required, head_at, contexts, expected_stall, blocking in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                fake = FakeRunner()
                facts = Path(tmp) / "repo-facts.json"
                facts.write_text(
                    json.dumps(
                        {
                            "repos": {
                                "repo": {
                                    "full_name": "owner/repo",
                                    "required_checks": required,
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                fake.add(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        "owner/repo",
                        "--state",
                        "open",
                        "--limit",
                        "201",
                        "--json",
                        "number,title,url,autoMergeRequest",
                    ],
                    stdout=json.dumps(
                        [
                            {
                                "number": 8,
                                "title": "Waiting",
                                "url": "https://github.com/owner/repo/pull/8",
                                "autoMergeRequest": {"enabledAt": old},
                            }
                        ]
                    ),
                )
                fake.add(
                    _stall_evidence_prefix(),
                    stdout=_stall_evidence(
                        enabled_at=old,
                        head_at=head_at,
                        contexts=contexts,
                    ),
                    prefix=True,
                )

                stalls = stalled_auto_merges(
                    owner="owner",
                    facts_path=facts,
                    min_age=timedelta(hours=2),
                    runner=fake,
                )

            self.assertEqual(bool(stalls), expected_stall)
            if expected_stall:
                self.assertEqual(stalls[0]["blocking_checks"], blocking)
                self.assertEqual(stalls[0]["age_source"], "head")

    def test_stall_age_falls_back_to_auto_merge_enablement_without_required_names(
        self,
    ) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(
            microsecond=0
        ).isoformat()
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            facts = Path(tmp) / "repo-facts.json"
            facts.write_text(
                json.dumps(
                    {
                        "repos": {
                            "repo": {
                                "full_name": "owner/repo",
                                "required_checks": [],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            fake.add(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    "owner/repo",
                    "--state",
                    "open",
                    "--limit",
                    "201",
                    "--json",
                    "number,title,url,autoMergeRequest",
                ],
                stdout=json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Waiting",
                            "url": "https://github.com/owner/repo/pull/8",
                            "autoMergeRequest": {"enabledAt": old},
                        }
                    ]
                ),
            )
            fake.add(
                _stall_evidence_prefix(),
                stdout=_stall_evidence(enabled_at=old, head_at=old, contexts=[]),
                prefix=True,
            )

            stalls = stalled_auto_merges(
                owner="owner",
                facts_path=facts,
                min_age=timedelta(hours=2),
                runner=fake,
            )

        self.assertEqual(stalls[0]["age_source"], "auto_merge")
        self.assertEqual(stalls[0]["blocking_checks"], ["required-checks-unavailable"])

    def test_stall_evidence_malformed_or_saturated_fails_closed(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        old = (now - timedelta(hours=3)).isoformat()
        base = json.loads(
            _stall_evidence(
                enabled_at=old,
                head_at=old,
                contexts=[_check_run("test", created_at=old)],
            )
        )
        scenarios: list[tuple[str, dict[str, object], str]] = []
        for field in ("enabledAt", "pushedDate", "committedDate", "suiteCreatedAt"):
            payload = json.loads(json.dumps(base))
            pr = payload["data"]["repository"]["pullRequest"]
            commit = pr["commits"]["nodes"][0]["commit"]
            if field == "enabledAt":
                pr["autoMergeRequest"]["enabledAt"] = "not-a-timestamp"
            elif field == "suiteCreatedAt":
                commit["checkSuites"]["nodes"][0]["createdAt"] = "not-a-timestamp"
            else:
                commit[field] = "not-a-timestamp"
            scenarios.append((field, payload, "stalls_output_incomplete"))
        oid_mismatch = json.loads(json.dumps(base))
        oid_mismatch["data"]["repository"]["pullRequest"]["commits"]["nodes"][0][
            "commit"
        ]["oid"] = "3" * 40
        scenarios.append(("oid-mismatch", oid_mismatch, "stalls_output_incomplete"))
        saturated = json.loads(json.dumps(base))
        saturated["data"]["repository"]["pullRequest"]["commits"]["nodes"][0][
            "commit"
        ]["checkSuites"]["pageInfo"]["hasNextPage"] = True
        scenarios.append(("saturated", saturated, "stalls_check_contexts_saturated"))
        missing_app = json.loads(json.dumps(base))
        missing_app["data"]["repository"]["pullRequest"]["commits"]["nodes"][0][
            "commit"
        ]["checkSuites"]["nodes"][0].pop("app")
        scenarios.append(("missing-suite-app", missing_app, "stalls_output_incomplete"))
        same_suite_rerun = json.loads(json.dumps(base))
        same_suite_runs = same_suite_rerun["data"]["repository"]["pullRequest"][
            "commits"
        ]["nodes"][0]["commit"]["checkSuites"]["nodes"][0]["checkRuns"]["nodes"]
        same_suite_runs.append(
            _check_run(
                "test",
                created_at=old,
                status="COMPLETED",
                conclusion="SUCCESS",
                started_at=(now - timedelta(minutes=20)).isoformat(),
                completed_at=(now - timedelta(minutes=10)).isoformat(),
            )
        )
        scenarios.append(
            ("same-suite-rerun-ambiguous", same_suite_rerun, "stalls_output_incomplete")
        )
        tied_suites = json.loads(
            _stall_evidence(
                enabled_at=old,
                head_at=old,
                contexts=[
                    _check_run("test", app_id=15368, created_at=old),
                    _check_run("test", app_id=15368, created_at=old),
                ],
            )
        )
        scenarios.append(("same-app-suite-tie", tied_suites, "stalls_output_incomplete"))

        for name, evidence, expected_code in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                fake = FakeRunner()
                facts = Path(tmp) / "repo-facts.json"
                facts.write_text(
                    json.dumps(
                        {
                            "repos": {
                                "repo": {
                                    "full_name": "owner/repo",
                                    "required_checks": ["test"],
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                fake.add(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        "owner/repo",
                        "--state",
                        "open",
                        "--limit",
                        "201",
                        "--json",
                        "number,title,url,autoMergeRequest",
                    ],
                    stdout=json.dumps(
                        [
                            {
                                "number": 8,
                                "title": "Waiting",
                                "url": "https://github.com/owner/repo/pull/8",
                                "autoMergeRequest": {"enabledAt": old},
                            }
                        ]
                    ),
                )
                fake.add(
                    _stall_evidence_prefix(),
                    stdout=json.dumps(evidence),
                    prefix=True,
                )

                stalls = stalled_auto_merges(
                    owner="owner",
                    facts_path=facts,
                    min_age=timedelta(hours=2),
                    runner=fake,
                )

            self.assertEqual(stalls[0]["failure_code"], expected_code)
        self.assertIn("checkType:LATEST", STALL_EVIDENCE_QUERY)

    def test_legacy_name_only_required_check_accepts_current_success(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(
            microsecond=0
        ).isoformat()
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            facts = Path(tmp) / "repo-facts.json"
            facts.write_text(
                json.dumps(
                    {
                        "repos": {
                            "repo": {
                                "full_name": "owner/repo",
                                "required_checks": ["test"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            fake.add(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    "owner/repo",
                    "--state",
                    "open",
                    "--limit",
                    "201",
                    "--json",
                    "number,title,url,autoMergeRequest",
                ],
                stdout=json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Waiting",
                            "url": "https://github.com/owner/repo/pull/8",
                            "autoMergeRequest": {"enabledAt": old},
                        }
                    ]
                ),
            )
            fake.add(
                _stall_evidence_prefix(),
                stdout=_stall_evidence(
                    enabled_at=old,
                    head_at=old,
                    contexts=[
                        _check_run(
                            "test",
                            created_at=old,
                            status="COMPLETED",
                            conclusion="SUCCESS",
                            started_at=old,
                            completed_at=old,
                        )
                    ],
                ),
                prefix=True,
            )

            stalls = stalled_auto_merges(
                owner="owner",
                facts_path=facts,
                min_age=timedelta(hours=2),
                runner=fake,
            )

        self.assertEqual(stalls, [])

    def test_app_bound_required_check_uses_only_exact_check_run_app(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(
            microsecond=0
        ).isoformat()
        status_success = {
            "__typename": "StatusContext",
            "context": "test",
            "state": "SUCCESS",
            "createdAt": old,
        }
        wrong_app_success = _check_run(
            "test",
            app_id=7,
            created_at=old,
            status="COMPLETED",
            conclusion="SUCCESS",
            started_at=old,
            completed_at=old,
        )
        exact_app_pending = _check_run("test", app_id=15368, created_at=old)
        exact_app_success = _check_run(
            "test",
            app_id=15368,
            created_at=old,
            status="COMPLETED",
            conclusion="SUCCESS",
            started_at=old,
            completed_at=old,
        )
        scenarios = (
            ("wrong-app-and-status", [wrong_app_success, status_success], True),
            (
                "exact-pending-beats-wrong-success",
                [wrong_app_success, status_success, exact_app_pending],
                True,
            ),
            ("exact-success", [wrong_app_success, status_success, exact_app_success], False),
        )
        for name, contexts, expected_stall in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                fake = FakeRunner()
                facts = Path(tmp) / "repo-facts.json"
                facts.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "repos": {
                                "repo": {
                                    "full_name": "owner/repo",
                                    "required_checks": [
                                        {"context": "test", "app_id": 15368}
                                    ],
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                fake.add(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        "owner/repo",
                        "--state",
                        "open",
                        "--limit",
                        "201",
                        "--json",
                        "number,title,url,autoMergeRequest",
                    ],
                    stdout=json.dumps(
                        [
                            {
                                "number": 8,
                                "title": "Waiting",
                                "url": "https://github.com/owner/repo/pull/8",
                                "autoMergeRequest": {"enabledAt": old},
                            }
                        ]
                    ),
                )
                fake.add(
                    _stall_evidence_prefix(),
                    stdout=_stall_evidence(
                        enabled_at=old,
                        head_at=old,
                        contexts=contexts,
                    ),
                    prefix=True,
                )

                stalls = stalled_auto_merges(
                    owner="owner",
                    facts_path=facts,
                    min_age=timedelta(hours=2),
                    runner=fake,
                )

            self.assertEqual(bool(stalls), expected_stall)
            if expected_stall:
                self.assertEqual(stalls[0]["blocking_checks"], ["test@app:15368"])
                self.assertEqual(stalls[0]["check_status"], "pending")
        self.assertIn("app{databaseId}", STALL_EVIDENCE_QUERY)

    def test_stalls_subcommand_does_not_infer_current_pr(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            facts = Path(tmp) / "repo-facts.json"
            facts.write_text(
                json.dumps({"repos": {"repo": {"full_name": "owner/repo"}}}),
                encoding="utf-8",
            )
            fake.add(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    "owner/repo",
                    "--state",
                    "open",
                    "--limit",
                    "201",
                    "--json",
                    "number,title,url,autoMergeRequest",
                ],
                stdout="[]",
            )

            with redirect_stdout(io.StringIO()):
                status = pr_shepherd_main(
                    ["stalls", "--repo-facts", str(facts), "--min-age-hours", "2"],
                    runner=fake,
                )

        self.assertEqual(status, 0)
        self.assertEqual(fake.calls[0][:3], ["gh", "pr", "list"])

    def test_stale_cache_refreshes_before_merge(self) -> None:
        fake = _runner_for_repo_fact_refresh()
        with tempfile.TemporaryDirectory() as tmp:
            facts = Path(tmp) / "repo-facts.json"
            facts.write_text(json.dumps({"repos": {}}), encoding="utf-8")

            entry = cached_or_refreshed_facts(
                repo=f"{ORG_OWNER}/repo",
                repo_name="repo",
                facts_path=facts,
                runner=fake,
            )
            cache = json.loads(facts.read_text(encoding="utf-8"))

        self.assertTrue(entry["protected"])
        self.assertTrue(entry["runner"]["online"])
        self.assertEqual(entry["runner"]["scope"], "org-pool")
        self.assertEqual(cache["owner"], ORG_OWNER)
        self.assertEqual(cache["schema_version"], 3)

    def test_transferred_repo_does_not_reuse_fresh_old_owner_facts(self) -> None:
        fresh_time = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            facts = Path(tmp) / "repo-facts.json"
            facts.write_text(
                json.dumps({
                    "owner": ORG_OWNER,
                    "repos": {"repo": {
                        "full_name": f"{ORG_OWNER}/repo",
                        "checked_at": fresh_time,
                        "protected": True,
                        "runner": {"scope": "org-pool", "online": True},
                    }},
                }),
                encoding="utf-8",
            )
            current = {
                "full_name": f"{PERSONAL_OWNER}/repo",
                "checked_at": fresh_time,
                "protected": False,
                "runner": {"scope": "github-hosted", "online": None},
            }
            with patch("git_janitor.pr_shepherd.collect_one", return_value=current) as refresh:
                entry = cached_or_refreshed_facts(
                    repo=f"{PERSONAL_OWNER}/repo",
                    repo_name="repo",
                    facts_path=facts,
                )
            cache = json.loads(facts.read_text(encoding="utf-8"))

        refresh.assert_called_once()
        self.assertEqual(entry, current)
        self.assertEqual(cache["owner"], PERSONAL_OWNER)


class PrShepherdWatchTests(FixtureOwnerTests):
    OPEN_CMD = [
        "gh",
        "pr",
        "list",
        "--repo",
        "owner/repo",
        "--state",
        "open",
        "--limit",
        "201",
        "--json",
        "number,title,url,autoMergeRequest",
    ]
    MERGED_CMD_PREFIX = _merged_pr_list_prefix()

    def _facts_file(self, directory: Path) -> Path:
        facts = directory / "repo-facts.json"
        facts.write_text(
            json.dumps({"repos": {"repo": {"full_name": "owner/repo"}}}),
            encoding="utf-8",
        )
        return facts

    def test_watch_prints_summary_and_exits_zero_when_clean(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = self._facts_file(tmp_path)
            alert = _recording_alert(tmp_path)
            receipts = tmp_path / "private" / "watch.jsonl"
            fake.add(self.OPEN_CMD, stdout="[]")
            fake.add(self.MERGED_CMD_PREFIX, stdout="[]", prefix=True)

            out = io.StringIO()
            with redirect_stdout(out):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--owner",
                        "owner",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                        "--receipt-path",
                        str(receipts),
                    ],
                    runner=fake,
                )
            alert_used = (tmp_path / "alert-args.txt").exists()
            receipt_text = receipts.read_text(encoding="utf-8")
            receipt = json.loads(receipt_text)
            receipt_mode = receipts.stat().st_mode & 0o777
            receipt_dir_mode = receipts.parent.stat().st_mode & 0o777

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue()), {"orphans": [], "stalls": []})
        self.assertFalse(alert_used)
        self.assertEqual(receipt["schema"], "pr-shepherd.watch-receipt/v2")
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["outcome"], "clean")
        self.assertTrue(receipt["completed_at"].endswith("+00:00"))
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["alert_delivery"], "not_needed")
        self.assertEqual(receipt["stalls_count"], 0)
        self.assertEqual(receipt["orphans_count"], 0)
        self.assertEqual(receipt["run_id"], TEST_RUN_ID)
        self.assertEqual(receipt["runtime_sha"], TEST_RUNTIME_SHA)
        self.assertEqual(receipt["runtime_manifest_sha256"], TEST_MANIFEST_SHA)
        self.assertEqual(
            receipt["hermes_runtime_manifest_sha256"],
            TEST_HERMES_MANIFEST_SHA,
        )
        self.assertEqual(receipt_mode, 0o600)
        self.assertEqual(receipt_dir_mode, 0o700)
        self.assertTrue(receipt_text.endswith("\n"))
        self.assertEqual(len(receipt_text.splitlines()), 1)

    def test_watch_alerts_with_human_summary_and_exits_two_on_findings(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = self._facts_file(tmp_path)
            alert = _recording_alert(tmp_path)
            receipts = tmp_path / "private" / "watch.jsonl"
            fake.add(
                self.OPEN_CMD,
                stdout=json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Waiting",
                            "url": "https://github.com/owner/repo/pull/8",
                            "updatedAt": "2020-01-01T00:00:00Z",
                            "autoMergeRequest": {"enabledAt": "2020-01-01T00:00:00Z"},
                            "statusCheckRollup": [{"status": "QUEUED"}],
                        }
                    ]
                ),
            )
            fake.add(
                _stall_evidence_prefix(),
                stdout=_stall_evidence(
                    enabled_at="2020-01-01T00:00:00Z",
                    head_at="2020-01-01T00:00:00Z",
                    contexts=[],
                ),
                prefix=True,
            )
            merged_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            fake.add(
                self.MERGED_CMD_PREFIX,
                stdout=json.dumps(
                    [
                        {
                            "number": 9,
                            "title": "Recent sync change",
                            "url": "https://github.com/owner/repo/pull/9",
                            "mergedAt": merged_at,
                        }
                    ]
                ),
                prefix=True,
            )
            fake.add(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    _query_prefix(
                        'query { repository(owner: "owner", name: "repo") { pullRequest(number: 9)'
                    ),
                ],
                stdout=_threads_payload([_bot_thread("T9", "P1", "Fix ordering")]),
                prefix=True,
            )

            out = io.StringIO()
            with redirect_stdout(out):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--owner",
                        "owner",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                        "--alert-destination-alias",
                        "fleet-ops",
                        "--require-alert-config",
                        "--receipt-path",
                        str(receipts),
                    ],
                    runner=fake,
                )
            args = (tmp_path / "alert-args.txt").read_text(encoding="utf-8").splitlines()
            body = (tmp_path / "alert-body.txt").read_text(encoding="utf-8")
            receipt = json.loads(receipts.read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertEqual(args[0], "--subject")
        self.assertIn("1 stalled", args[1])
        self.assertIn("tests-only delivery", args[1])
        self.assertEqual(json.loads(out.getvalue())["stalls"][0]["number"], 8)
        self.assertEqual(json.loads(out.getvalue())["orphans"], [])
        self.assertIn("Stalled auto-merge PRs", body)
        self.assertIn("owner/repo#8", body)
        self.assertNotIn("Orphaned unresolved Codex P1 findings", body)
        self.assertNotIn("owner/repo#9", body)
        self.assertNotIn('"stalls"', body)
        self.assertNotIn('"orphans"', body)
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["outcome"], "findings")
        self.assertEqual(receipt["exit_code"], 2)
        self.assertEqual(receipt["alert_delivery"], "delivered")
        self.assertEqual(receipt["destination_alias"], "fleet-ops")
        self.assertEqual(receipt["stalls_count"], 1)
        self.assertEqual(receipt["orphans_count"], 0)
        self.assertNotIn("subject", receipt)
        self.assertNotIn("body", receipt)
        self.assertNotIn("target", receipt)

    def test_watch_alerts_and_exits_three_when_repo_facts_empty(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = tmp_path / "repo-facts.json"
            facts.write_text(json.dumps({"repos": {}}), encoding="utf-8")
            alert = _recording_alert(tmp_path)

            with redirect_stderr(io.StringIO()):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                    ],
                    runner=fake,
                )
            args = (tmp_path / "alert-args.txt").read_text(encoding="utf-8").splitlines()

        self.assertEqual(status, 3)
        self.assertEqual(fake.calls, [])
        self.assertIn("repo-facts", args[1])

    def test_watch_exits_three_when_alert_command_missing(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = self._facts_file(tmp_path)
            receipts = tmp_path / "watch.jsonl"
            fake.add(
                self.OPEN_CMD,
                stdout=json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Waiting",
                            "url": "https://github.com/owner/repo/pull/8",
                            "updatedAt": "2020-01-01T00:00:00Z",
                            "autoMergeRequest": {"enabledAt": "2020-01-01T00:00:00Z"},
                            "statusCheckRollup": [{"status": "QUEUED"}],
                        }
                    ]
                ),
            )
            fake.add(
                _stall_evidence_prefix(),
                stdout=_stall_evidence(
                    enabled_at="2020-01-01T00:00:00Z",
                    head_at="2020-01-01T00:00:00Z",
                    contexts=[],
                ),
                prefix=True,
            )
            fake.add(self.MERGED_CMD_PREFIX, stdout="[]", prefix=True)

            out = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--owner",
                        "owner",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(tmp_path / "missing-fleet-send"),
                        "--receipt-path",
                        str(receipts),
                    ],
                    runner=fake,
                )
            receipt = json.loads(receipts.read_text(encoding="utf-8"))

        self.assertEqual(status, 3)
        self.assertEqual(json.loads(out.getvalue())["stalls"][0]["number"], 8)
        self.assertIn("not delivered", err.getvalue())
        self.assertEqual(receipt["status"], "failure")
        self.assertEqual(receipt["outcome"], "broken")
        self.assertEqual(receipt["exit_code"], 3)
        self.assertEqual(receipt["alert_delivery"], "failed")
        self.assertIn("alert_delivery_failed", receipt["failure_codes"])

    def test_watch_missing_required_alert_config_is_broken_even_when_clean(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = self._facts_file(tmp_path)
            alert = _recording_alert(tmp_path)
            receipts = tmp_path / "watch.jsonl"
            fake.add(self.OPEN_CMD, stdout="[]")
            fake.add(self.MERGED_CMD_PREFIX, stdout="[]", prefix=True)

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--owner",
                        "owner",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                        "--require-alert-config",
                        "--receipt-path",
                        str(receipts),
                    ],
                    runner=fake,
                )
            receipt = json.loads(receipts.read_text(encoding="utf-8"))

        self.assertEqual(status, 3)
        self.assertEqual(receipt["status"], "failure")
        self.assertEqual(receipt["outcome"], "broken")
        self.assertEqual(receipt["alert_delivery"], "unconfigured")
        self.assertIn("alert_unconfigured", receipt["failure_codes"])

    def test_watch_receipt_write_failure_is_not_silent_success(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = self._facts_file(tmp_path)
            alert = _recording_alert(tmp_path)
            blocked_parent = tmp_path / "not-a-directory"
            blocked_parent.write_text("blocked", encoding="utf-8")
            fake.add(self.OPEN_CMD, stdout="[]")
            fake.add(self.MERGED_CMD_PREFIX, stdout="[]", prefix=True)

            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--owner",
                        "owner",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                        "--receipt-path",
                        str(blocked_parent / "watch.jsonl"),
                    ],
                    runner=fake,
                )

        self.assertEqual(status, 3)
        self.assertIn("receipt write failed", err.getvalue())

    def test_watch_invalid_repo_facts_writes_broken_receipt(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = tmp_path / "repo-facts.json"
            facts.write_text("not-json", encoding="utf-8")
            alert = _recording_alert(tmp_path)
            receipts = tmp_path / "watch.jsonl"

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                        "--receipt-path",
                        str(receipts),
                    ],
                    runner=fake,
                )
            receipt = json.loads(receipts.read_text(encoding="utf-8"))

        self.assertEqual(status, 3)
        self.assertEqual(receipt["status"], "failure")
        self.assertEqual(receipt["outcome"], "broken")
        self.assertEqual(receipt["facts_cache"], "unreadable")
        self.assertIn("repo_facts_read_failed", receipt["failure_codes"])

    def test_watch_structurally_invalid_repo_facts_writes_broken_receipt(self) -> None:
        fake = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = tmp_path / "repo-facts.json"
            facts.write_text("[]", encoding="utf-8")
            alert = _recording_alert(tmp_path)
            receipts = tmp_path / "watch.jsonl"

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                        "--receipt-path",
                        str(receipts),
                    ],
                    runner=fake,
                )
            receipt = json.loads(receipts.read_text(encoding="utf-8"))

        self.assertEqual(status, 3)
        self.assertEqual(receipt["outcome"], "broken")
        self.assertIn("repo_facts_structure_invalid", receipt["failure_codes"])

    def test_watch_api_failure_is_broken_not_findings(self) -> None:
        fake = FakeRunner()
        fake.add(self.OPEN_CMD, returncode=1, stderr="HTTP 503")
        fake.add(self.MERGED_CMD_PREFIX, stdout="[]", prefix=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = self._facts_file(tmp_path)
            alert = _recording_alert(tmp_path)
            receipts = tmp_path / "watch.jsonl"

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--owner",
                        "owner",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                        "--receipt-path",
                        str(receipts),
                    ],
                    runner=fake,
                )
            receipt = json.loads(receipts.read_text(encoding="utf-8"))

        self.assertEqual(status, 3)
        self.assertEqual(receipt["outcome"], "broken")
        self.assertEqual(receipt["stalls_count"], 0)
        self.assertIn("stalls_api_failed", receipt["failure_codes"])

    def test_watch_requests_past_thirty_open_prs_and_finds_the_last_stall(self) -> None:
        fake = FakeRunner()
        open_prs = [
            {
                "number": index,
                "title": f"Open {index}",
                "url": f"https://github.com/owner/repo/pull/{index}",
                "updatedAt": "2020-01-01T00:00:00Z",
                "autoMergeRequest": None if index < 31 else {"enabledAt": "2020-01-01T00:00:00Z"},
                "statusCheckRollup": [],
            }
            for index in range(1, 32)
        ]
        fake.add(self.OPEN_CMD, stdout=json.dumps(open_prs))
        fake.add(
            _stall_evidence_prefix(),
            stdout=_stall_evidence(
                number=31,
                enabled_at="2020-01-01T00:00:00Z",
                head_at="2020-01-01T00:00:00Z",
                contexts=[],
            ),
            prefix=True,
        )
        fake.add(self.MERGED_CMD_PREFIX, stdout="[]", prefix=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = self._facts_file(tmp_path)
            alert = _recording_alert(tmp_path)
            receipts = tmp_path / "private" / "watch.jsonl"
            with redirect_stdout(io.StringIO()):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--owner",
                        "owner",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                        "--receipt-path",
                        str(receipts),
                    ],
                    runner=fake,
                )
            receipt = json.loads(receipts.read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertEqual(receipt["stalls_count"], 1)

    def test_watch_fails_closed_when_open_scan_saturates(self) -> None:
        recent = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
        complete_open = [
            {
                "number": index,
                "title": f"Open {index}",
                "url": f"https://github.com/owner/repo/pull/{index}",
                "updatedAt": recent.isoformat().replace("+00:00", "Z"),
                "autoMergeRequest": None,
                "statusCheckRollup": [],
            }
            for index in range(1, 202)
        ]
        scenarios = (
            (complete_open, [], "stalls_scan_saturated"),
        )
        for open_payload, merged_payload, failure_code in scenarios:
            with self.subTest(failure_code=failure_code), tempfile.TemporaryDirectory() as tmp:
                fake = FakeRunner()
                fake.add(self.OPEN_CMD, stdout=json.dumps(open_payload))
                fake.add(
                    self.MERGED_CMD_PREFIX,
                    stdout=json.dumps(merged_payload),
                    prefix=True,
                )
                tmp_path = Path(tmp)
                facts = self._facts_file(tmp_path)
                alert = _recording_alert(tmp_path)
                receipts = tmp_path / "private" / "watch.jsonl"
                with redirect_stdout(io.StringIO()):
                    status = pr_shepherd_main(
                        [
                            "watch",
                            "--owner",
                            "owner",
                            "--repo-facts",
                            str(facts),
                            "--alert-command",
                            str(alert),
                            "--receipt-path",
                            str(receipts),
                        ],
                        runner=fake,
                    )
                receipt = json.loads(receipts.read_text(encoding="utf-8"))

            self.assertEqual(status, 3)
            self.assertEqual(receipt["outcome"], "broken")
            self.assertIn(failure_code, receipt["failure_codes"])

    def test_watch_structural_refresh_exceptions_still_write_broken_receipt(self) -> None:
        for exception in (KeyError("missing"), RecursionError("recursive")):
            with self.subTest(exception=type(exception).__name__), tempfile.TemporaryDirectory() as tmp:
                fake = FakeRunner()
                fake.add(self.OPEN_CMD, stdout="[]")
                fake.add(self.MERGED_CMD_PREFIX, stdout="[]", prefix=True)
                tmp_path = Path(tmp)
                facts = self._facts_file(tmp_path)
                alert = _recording_alert(tmp_path)
                receipts = tmp_path / "private" / "watch.jsonl"
                with patch("git_janitor.pr_shepherd.collect_all", side_effect=exception):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        status = pr_shepherd_main(
                            [
                                "watch",
                                "--refresh-facts",
                                "--owner",
                                "owner",
                                "--repo-facts",
                                str(facts),
                                "--alert-command",
                                str(alert),
                                "--receipt-path",
                                str(receipts),
                            ],
                            runner=fake,
                        )
                receipt = json.loads(receipts.read_text(encoding="utf-8"))

            self.assertEqual(status, 3)
            self.assertEqual(receipt["outcome"], "broken")
            self.assertIn("repo_facts_refresh_failed", receipt["failure_codes"])

    def test_watch_malformed_and_incomplete_sweep_payloads_are_broken(self) -> None:
        scenarios = (
            ("malformed-open", "{", "[]", "stalls_output_invalid"),
            (
                "incomplete-open-record",
                json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Waiting",
                            "autoMergeRequest": {"enabledAt": "2026-07-15T12:00:00Z"},
                        }
                    ]
                ),
                "[]",
                "stalls_output_incomplete",
            ),
            (
                "incomplete-auto-merge-request",
                json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Waiting",
                            "url": "https://github.com/owner/repo/pull/8",
                            "updatedAt": "2026-07-15T12:00:00Z",
                            "autoMergeRequest": {},
                            "statusCheckRollup": [],
                        }
                    ]
                ),
                "[]",
                "stalls_output_incomplete",
            ),
        )
        for name, open_stdout, merged_stdout, failure_code in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                fake = FakeRunner()
                fake.add(self.OPEN_CMD, stdout=open_stdout)
                fake.add(self.MERGED_CMD_PREFIX, stdout=merged_stdout, prefix=True)
                tmp_path = Path(tmp)
                facts = self._facts_file(tmp_path)
                alert = _recording_alert(tmp_path)
                receipts = tmp_path / "watch.jsonl"

                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    status = pr_shepherd_main(
                        [
                            "watch",
                            "--owner",
                            "owner",
                            "--repo-facts",
                            str(facts),
                            "--alert-command",
                            str(alert),
                            "--receipt-path",
                            str(receipts),
                        ],
                        runner=fake,
                    )
                receipt = json.loads(receipts.read_text(encoding="utf-8"))

            self.assertEqual(status, 3)
            self.assertEqual(receipt["outcome"], "broken")
            self.assertIn(failure_code, receipt["failure_codes"])

    def test_watch_does_not_query_retired_review_graphql(self) -> None:
        fake = FakeRunner()
        fake.add(self.OPEN_CMD, stdout="[]")
        merged_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        fake.add(
            self.MERGED_CMD_PREFIX,
            stdout=json.dumps(
                [
                    {
                        "number": 9,
                        "title": "Merged change",
                        "url": "https://github.com/owner/repo/pull/9",
                        "mergedAt": merged_at,
                    }
                ]
            ),
            prefix=True,
        )
        fake.add(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                _query_prefix(
                    'query { repository(owner: "owner", name: "repo") { pullRequest(number: 9)'
                ),
            ],
            stdout=json.dumps({"errors": [{"message": "temporarily unavailable"}]}),
            prefix=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = self._facts_file(tmp_path)
            alert = _recording_alert(tmp_path)
            receipts = tmp_path / "watch.jsonl"

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--owner",
                        "owner",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                        "--receipt-path",
                        str(receipts),
                    ],
                    runner=fake,
                )
            receipt = json.loads(receipts.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(receipt["orphans_count"], 0)
        self.assertEqual(receipt["failure_codes"], [])
        self.assertFalse(any(call[:3] == ["gh", "api", "graphql"] for call in fake.calls))

    def test_watch_survives_refresh_failure_sweeps_stale_cache_and_exits_three(self) -> None:
        fake = FakeRunner()
        fake.add(
            [
                "gh",
                "repo",
                "list",
                "owner",
                "--no-archived",
                "--limit",
                "201",
                "--json",
                "name,visibility,defaultBranchRef",
            ],
            returncode=1,
            stderr="HTTP 503",
        )
        fake.add(self.OPEN_CMD, stdout="[]")
        fake.add(self.MERGED_CMD_PREFIX, stdout="[]", prefix=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            facts = self._facts_file(tmp_path)
            alert = _recording_alert(tmp_path)

            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--refresh-facts",
                        "--owner",
                        "owner",
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                    ],
                    runner=fake,
                )
            args = (tmp_path / "alert-args.txt").read_text(encoding="utf-8").splitlines()

        self.assertEqual(status, 3)
        self.assertIn("refresh failed", err.getvalue())
        self.assertIn("refresh failed", args[1])
        sweeps = [call for call in fake.calls if call[:3] == ["gh", "pr", "list"]]
        self.assertEqual(len(sweeps), 1)

    def test_watch_refresh_facts_populates_cache_before_sweep(self) -> None:
        fake = _runner_for_repo_fact_refresh()
        fake.add(
            [
                "gh",
                "repo",
                "list",
                ORG_OWNER,
                "--no-archived",
                "--limit",
                "201",
                "--json",
                "name,visibility,defaultBranchRef",
            ],
            stdout=json.dumps(
                [{"name": "repo", "visibility": "PRIVATE", "defaultBranchRef": {"name": "main"}}]
            ),
        )
        fake.add(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                f"{ORG_OWNER}/repo",
                "--state",
                "open",
                "--limit",
                "201",
                "--json",
                "number,title,url,autoMergeRequest",
            ],
            stdout="[]",
        )
        fake.add(
            _merged_pr_list_prefix(repo=f"{ORG_OWNER}/repo"),
            stdout="[]",
            prefix=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            alert = _recording_alert(tmp_path)
            facts = tmp_path / "repo-facts.json"

            with redirect_stdout(io.StringIO()):
                status = pr_shepherd_main(
                    [
                        "watch",
                        "--refresh-facts",
                        "--owner",
                        ORG_OWNER,
                        "--repo-facts",
                        str(facts),
                        "--alert-command",
                        str(alert),
                    ],
                    runner=fake,
                )
            cache = json.loads(facts.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertTrue(cache["repos"]["repo"]["protected"])


class SafeDeleteTests(FixtureOwnerTests):
    def test_fixture_cleanup_proofs(self) -> None:
        for case in _load_cleanup_cases():
            with self.subTest(case=case["name"]):
                fake = _runner_for_cleanup_scenario(case["scenario"], case["branch"])
                report = evaluate_delete(case["branch"], repo_path=REPO_PATH, runner=fake)

                self.assertEqual(report.passed, case["expected_passed"])
                if case["expected_passed"]:
                    self.assertEqual(report.command, case["expected_command"])
                else:
                    failed = [proof.name for proof in report.proofs if not proof.passed]
                    self.assertIn(case["failed_proof"], failed)

    def test_invalid_open_pr_json_blocks_otherwise_proved_branch(self) -> None:
        for stdout in ("", " ", "{", "[", '[{"number": 1}', "not-json", "[] trailing"):
            with self.subTest(stdout=stdout):
                fake = _runner_for_cleanup_scenario("ancestor_pass", "old-docs", open_stdout=stdout)
                report = evaluate_delete("old-docs", repo_path=REPO_PATH, runner=fake)

                self.assertFalse(report.passed)
                failed = [proof for proof in report.proofs if not proof.passed]
                self.assertEqual([proof.name for proof in failed], ["no_open_pr"])
                self.assertIn("invalid JSON", failed[0].detail)

    def test_invalid_open_pr_shapes_block_otherwise_proved_branch(self) -> None:
        for stdout in (
            "null", "{}", '{"error": "unavailable"}', "false", "0", '"[]"',
            "[null]", "[1]", "[[]]", '[{"number": 1}, null]',
        ):
            with self.subTest(stdout=stdout):
                fake = _runner_for_cleanup_scenario("ancestor_pass", "old-docs", open_stdout=stdout)
                report = evaluate_delete("old-docs", repo_path=REPO_PATH, runner=fake)

                self.assertFalse(report.passed)
                failed = [proof for proof in report.proofs if not proof.passed]
                self.assertEqual([proof.name for proof in failed], ["no_open_pr"])
                self.assertIn("invalid PR list", failed[0].detail)

    def test_valid_empty_open_pr_list_permits_otherwise_proved_branch(self) -> None:
        for stdout in ("[]", " \n [] \t"):
            with self.subTest(stdout=stdout):
                fake = _runner_for_cleanup_scenario("ancestor_pass", "old-docs", open_stdout=stdout)
                report = evaluate_delete("old-docs", repo_path=REPO_PATH, runner=fake)

                self.assertTrue(report.passed)
                proof = next(proof for proof in report.proofs if proof.name == "no_open_pr")
                self.assertEqual(proof.detail, "no open PR")

    def test_open_pr_and_failed_query_block_otherwise_proved_branch(self) -> None:
        cases = (
            ('[{"number": 1, "title": "Pending", "url": "https://github.com/owner/repo/pull/1"}]',
             0, "", '"number": 1'),
            ("[]", 1, "query unavailable", "query unavailable"),
            ("[]", 1, "", "gh pr list failed"),
        )
        for stdout, returncode, stderr, detail in cases:
            with self.subTest(stdout=stdout, returncode=returncode, stderr=stderr):
                fake = _runner_for_cleanup_scenario(
                    "ancestor_pass", "old-docs", open_stdout=stdout,
                    open_returncode=returncode, open_stderr=stderr,
                )
                report = evaluate_delete("old-docs", repo_path=REPO_PATH, runner=fake)

                self.assertFalse(report.passed)
                failed = [proof for proof in report.proofs if not proof.passed]
                self.assertEqual([proof.name for proof in failed], ["no_open_pr"])
                self.assertIn(detail, failed[0].detail)

    def test_invalid_open_pr_json_blocks_execute_and_records_failed_proof(self) -> None:
        fake = _runner_for_cleanup_scenario("ancestor_pass", "old-docs", open_stdout="{")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "delete.jsonl"
            with redirect_stdout(io.StringIO()):
                status = git_safe_delete_main(
                    ["old-docs", "--execute", "--repo", str(REPO_PATH), "--ledger", str(ledger)],
                    runner=fake,
                )
            entry = json.loads(ledger.read_text(encoding="utf-8"))

        self.assertEqual(status, 3)
        self.assertEqual(entry["status"], "blocked")
        self.assertIsNone(entry["exit_code"])
        failed = [proof for proof in entry["proofs"] if not proof["passed"]]
        self.assertEqual([proof["name"] for proof in failed], ["no_open_pr"])
        self.assertIn("invalid JSON", failed[0]["detail"])
        self.assertFalse(any(call[:2] == ["git", "branch"] for call in fake.calls))
        self.assertFalse(any(call[:2] == ["git", "update-ref"] for call in fake.calls))

    def test_preserve_branch_writes_verified_bundle_and_manifest(self) -> None:
        fake = FakeRunner()
        fake.add(["git", "rev-parse", "old-docs"], stdout="abc123")
        fake.add_bundle_create(["git", "bundle", "create"], contents="bundle data")
        fake.add(["git", "bundle", "verify"], stdout="ok", prefix=True)
        with tempfile.TemporaryDirectory() as tmp:
            result = preserve_branch(
                "old-docs",
                repo_path=REPO_PATH,
                reason="test",
                validation="unit test",
                output_dir=Path(tmp),
                runner=fake,
            )

            self.assertTrue(result["ok"], result)
            self.assertGreater(Path(result["bundle"]).stat().st_size, 0)
            manifest = Path(result["manifest"]).read_text(encoding="utf-8")
            self.assertIn("- worktree_path: /repo", manifest)
            self.assertIn("- pr_mapping: not checked", manifest)

    def test_preserve_branch_reports_manifest_write_failure(self) -> None:
        fake = FakeRunner()
        with tempfile.NamedTemporaryFile() as tmp:
            result = preserve_branch(
                "old-docs",
                repo_path=REPO_PATH,
                reason="test",
                validation="unit test",
                output_dir=Path(tmp.name),
                runner=fake,
            )

        self.assertFalse(result["ok"])
        self.assertIn("cannot create preservation directory", result["error"])

    def test_git_safe_delete_execute_writes_ledger(self) -> None:
        fake = _runner_for_cleanup_scenario("ancestor_pass", "old-docs")
        fake.add(["git", "branch", "-d", "old-docs"])
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "delete.jsonl"
            with redirect_stdout(io.StringIO()):
                status = git_safe_delete_main(
                    [
                        "old-docs",
                        "--execute",
                        "--repo",
                        str(REPO_PATH),
                        "--ledger",
                        str(ledger),
                    ],
                    runner=fake,
                )
            entry = json.loads(ledger.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(entry["status"], "applied")
        self.assertEqual(entry["command"], ["git", "branch", "-d", "old-docs"])


class FakeRunner:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, ...], list[CommandResult]] = defaultdict(list)
        self.prefix_responses: list[tuple[tuple[str, ...], CommandResult]] = []
        self.bundle_prefixes: list[tuple[tuple[str, ...], str]] = []
        self.calls: list[list[str]] = []

    def add(
        self,
        args: Sequence[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        prefix: bool = False,
    ) -> None:
        result = CommandResult(
            args=list(args),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if prefix:
            self.prefix_responses.append((tuple(args), result))
        else:
            self.responses[tuple(args)].append(result)

    def add_bundle_create(self, args_prefix: Sequence[str], *, contents: str) -> None:
        self.bundle_prefixes.append((tuple(args_prefix), contents))

    def __call__(
        self,
        args: list[str],
        cwd: Path | None = None,
        timeout: int = 45,
    ) -> CommandResult:
        del cwd, timeout
        self.calls.append(list(args))
        key = tuple(args)
        for prefix, contents in self.bundle_prefixes:
            if key[: len(prefix)] == prefix:
                Path(args[-2]).write_text(contents, encoding="utf-8")
                return CommandResult(args=list(args), returncode=0, stdout="", stderr="")
        if self.responses[key]:
            if key[:3] == ("gh", "pr", "view") and len(self.responses[key]) == 1:
                return self.responses[key][0]
            return self.responses[key].pop(0)
        for prefix, result in self.prefix_responses:
            if _matches_prefix(key, prefix):
                return result
        raise AssertionError(f"unexpected command {args!r}")


def _org_pool_payload(*, online: int = 3, total: int = 3) -> str:
    return json.dumps(
        {
            "runners": [
                {
                    "name": f"example-org-runner-{index + 1}",
                    "status": "online" if index < online else "offline",
                    "busy": False,
                    "labels": [{"name": "self-hosted"}, {"name": "runner-pool"}],
                }
                for index in range(total)
            ]
        }
    )


def _runner_for_repo_fact_refresh() -> FakeRunner:
    fake = FakeRunner()
    fake.add(
        [
            "gh",
            "repo",
            "view",
            f"{ORG_OWNER}/repo",
            "--json",
            "name,visibility,defaultBranchRef",
        ],
        stdout=json.dumps(
            {"name": "repo", "visibility": "PRIVATE", "defaultBranchRef": {"name": "main"}}
        ),
    )
    fake.add(
        ["gh", "api", f"repos/{ORG_OWNER}/repo/branches/main/protection"],
        stdout=json.dumps(
            {"required_status_checks": {"checks": [{"context": "test", "app_id": 1}]}}
        ),
    )
    fake.add(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{ORG_OWNER}/repo",
            "--state",
            "all",
            "--limit",
            "20",
            "--json",
            "number",
        ],
        stdout="[]",
    )
    fake.add(
        ["gh", "api", f"orgs/{ORG_OWNER}/actions/runners"],
        stdout=_org_pool_payload(),
    )
    return fake


def _runner_for_cleanup_scenario(
    name: str,
    branch: str,
    *,
    open_stdout: str | None = None,
    open_returncode: int = 0,
    open_stderr: str = "",
) -> FakeRunner:
    fake = FakeRunner()
    fake.add(["git", "remote", "get-url", "origin"], stdout="https://github.com/owner/repo.git")
    fake.add(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], stdout="origin/main")
    fake.add(["git", "rev-parse", "--verify", branch], stdout="branch-sha")
    current_branch = branch if name == "active_branch" else "main"
    fake.add(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout=current_branch)
    status = " M src/app.py" if name == "dirty_worktree" else ""
    fake.add(["git", "status", "--porcelain=v1"], stdout=status)
    worktree_output = "worktree /repo\nHEAD abc\nbranch refs/heads/main\n"
    if name == "linked_worktree":
        worktree_output += f"\nworktree /other\nHEAD def\nbranch refs/heads/{branch}\n"
    fake.add(["git", "worktree", "list", "--porcelain"], stdout=worktree_output)
    fake.add(
        ["git", "for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}"],
        stdout=f"origin/{branch}",
    )
    fake.add(
        ["git", "rev-parse", "--verify", "--quiet", f"origin/{branch}"],
        returncode=1,
    )
    ancestor_code = 1 if name == "squash_tree_equivalent" else 0
    fake.add(["git", "merge-base", "--is-ancestor", branch, "origin/main"], returncode=ancestor_code)
    open_prs = [{"number": 1}] if name == "open_pr" else []
    fake.add(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "open",
            "--head",
            branch,
            "--json",
            "number,title,url",
        ],
        stdout=json.dumps(open_prs) if open_stdout is None else open_stdout,
        returncode=open_returncode,
        stderr=open_stderr,
    )
    merged = []
    if name == "squash_tree_equivalent":
        merged = [{"number": 2, "mergeCommit": {"oid": "merge-sha"}}]
    fake.add(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "merged",
            "--head",
            branch,
            "--json",
            "number,title,url,mergeCommit",
        ],
        stdout=json.dumps(merged),
    )
    if name == "squash_tree_equivalent":
        fake.add(["git", "diff", "--quiet", "merge-sha", branch])
    changed = "config/.env\n" if name == "secret_path" else ""
    fake.add(["git", "diff", "--name-only", f"origin/main...{branch}"], stdout=changed)
    return fake


def _query_prefix(prefix: str) -> str:
    return f"query={prefix}"


def _protected_facts_file(directory: Path) -> Path:
    facts = directory / "repo-facts.json"
    facts.write_text(
        json.dumps(
            {
                "repos": {
                    "repo": {
                        "full_name": "owner/repo",
                        "visibility": "PRIVATE",
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                        "protected": True,
                        "required_checks": ["test"],
                        "runner": {"online": True},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return facts


def _recording_alert(directory: Path) -> Path:
    script = directory / "fleet-send"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" > "{directory}/alert-args.txt"\n'
        f'cat > "{directory}/alert-body.txt"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _bot_thread(thread_id: str, priority: str, title: str, *, resolved: bool = False) -> dict:
    body = (
        f"**<sub><sub>![{priority} Badge](https://img.shields.io/badge/{priority}-orange"
        f"?style=flat)</sub></sub>  {title}**"
    )
    return _thread_node(thread_id, "chatgpt-codex-connector", body, resolved=resolved)


def _human_thread(thread_id: str, body: str) -> dict:
    return _thread_node(thread_id, "example-owner", body, resolved=False)


def _thread_node(thread_id: str, login: str, body: str, *, resolved: bool) -> dict:
    return {
        "id": thread_id,
        "isResolved": resolved,
        "isOutdated": False,
        "path": "src/app.py",
        "line": 10,
        "comments": {
            "nodes": [
                {
                    "databaseId": 99,
                    "author": {"login": login},
                    "body": body,
                    "path": "src/app.py",
                    "line": 10,
                }
            ]
        },
    }


def _threads_payload(
    nodes: list[dict],
    *,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {
                                "hasNextPage": has_next_page,
                                "endCursor": end_cursor,
                            },
                            "nodes": nodes,
                        }
                    }
                }
            }
        }
    )


def _matches_prefix(key: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    if len(key) < len(prefix):
        return False
    for actual, expected in zip(key, prefix):
        if actual == expected:
            continue
        if actual.startswith(expected):
            continue
        return False
    return True


def _load_cleanup_cases() -> list[dict]:
    with (FIXTURES / "branch_cleanup_tools.json").open(encoding="utf-8") as handle:
        return json.load(handle)["cases"]


if __name__ == "__main__":
    unittest.main()
