"""Private Free delivery exercises real command routing with recorded API shapes."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_janitor import pr_shepherd as p
from git_janitor.models import CommandResult
from test_tests_only_delivery import BASE, HEAD, Runner


def evidence():
    return {'data': {'repository': {'pullRequest': {
        'headRefOid': HEAD, 'baseRefOid': BASE, 'baseRefName': 'main',
        'state': 'OPEN', 'isDraft': False, 'mergeable': 'MERGEABLE',
        'commits': {'nodes': [{'commit': {'oid': HEAD, 'statusCheckRollup': {
            'contexts': {'totalCount': 2, 'pageInfo': {'hasNextPage': False}, 'nodes': [
                {'__typename': 'CheckRun', 'name': 'test',
                 'status': 'COMPLETED', 'conclusion': 'SUCCESS'},
                {'__typename': 'StatusContext', 'context': 'workflow-lint', 'state': 'SUCCESS'},
            ]}
        }}}]}
    }}}}


def inventory(payload):
    return payload['data']['repository']['pullRequest']['commits']['nodes'][0]['commit']['statusCheckRollup']['contexts']


class PrivateRunner(Runner):
    def __init__(self, snapshots=None, query_code=0, **kwargs):
        super().__init__(**kwargs)
        self.snapshots = snapshots if snapshots is not None else [evidence()]
        self.query_code = query_code
        self.inventory_reads = 0

    def __call__(self, args, cwd, timeout):
        if args[:3] == ['gh', 'api', 'graphql']:
            self.calls.append(args)
            payload = self.snapshots[min(self.inventory_reads, len(self.snapshots) - 1)]
            self.inventory_reads += 1
            return CommandResult(args, self.query_code, json.dumps(payload), '')
        return super().__call__(args, cwd, timeout)


class UnprotectedDeliveryTests(unittest.TestCase):
    def invoke(self, runner, *, visibility='PRIVATE', required_checks=('test', 'workflow-lint'),
               expected_head=HEAD, **kwargs):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(p, 'cached_or_refreshed_facts', return_value={
                 'protected': False, 'required_checks': [], 'visibility': visibility,
                 'protection_error': 'Upgrade to GitHub Pro (HTTP 403)',
             }), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            ledger = Path(tmp) / 'ledger.jsonl'
            code = p.guarded_merge(
                repo='o/r', pr=8, facts_path=Path(tmp)/'facts', alert_command=Path('/unused'),
                lock_dir=Path(tmp)/'locks', ledger_path=ledger, required_checks=required_checks,
                expected_head=expected_head, runner=runner, **kwargs)
            self.receipt = json.loads(ledger.read_text().splitlines()[-1])
            return code

    def assert_blocked(self, runner, **kwargs):
        self.assertNotEqual(self.invoke(runner, **kwargs), 0)
        self.assertEqual(runner.mutations, [])
        self.assertEqual(self.receipt['status'], 'blocked')

    def test_green_private_free_pr_merges_with_head_precondition_and_readbacks(self):
        runner = PrivateRunner()
        self.assertEqual(self.invoke(runner), 0)
        self.assertEqual(runner.inventory_reads, 2)
        self.assertEqual(runner.reads, 3)
        self.assertEqual(len(runner.mutations), 1)
        self.assertIn('sha=' + HEAD, runner.mutations[0])
        self.assertEqual(runner.mutations[0][:4], ['gh', 'api', '--method', 'PUT'])
        self.assertFalse(any('--auto' in args or '--admin' in args for args in runner.calls))
        self.assertFalse(any(args[:3] == ['gh', 'pr', 'checks'] for args in runner.calls))
        self.assertEqual(self.receipt['expected_checks'], ['test', 'workflow-lint'])
        self.assertFalse(self.receipt['protected'])
        self.assertEqual(self.receipt['status'], 'applied')

    def test_missing_or_invalid_explicit_contract_and_public_repo_stay_closed(self):
        for kwargs in (
            {'required_checks': ()}, {'required_checks': ('test', 'test')},
            {'required_checks': ('',)}, {'required_checks': (' test',)},
            {'required_checks': ('test', 3)}, {'expected_head': None},
            {'expected_head': '9'*40}, {'visibility': 'PUBLIC'}, {'visibility': None},
        ):
            with self.subTest(kwargs=kwargs):
                self.assert_blocked(PrivateRunner(), **kwargs)

    def test_expected_check_missing_even_when_other_checks_pass(self):
        self.assert_blocked(PrivateRunner(), required_checks=('not-yet-reported',))

    def test_all_observed_checks_must_pass_including_unnamed_optional_checks(self):
        for result in ('FAILURE', 'SKIPPED', 'CANCELLED', 'NEUTRAL', 'TIMED_OUT', 'ACTION_REQUIRED', None):
            with self.subTest(result=result):
                payload = evidence()
                check = inventory(payload)['nodes'][0]
                check['name'], check['conclusion'] = 'optional', result
                self.assert_blocked(PrivateRunner([payload]), required_checks=('workflow-lint',))

    def test_pending_checks_return_monitor_without_arming_auto_merge(self):
        for rerun in (False, True):
            payload = evidence()
            inventory(payload)['nodes'][0].update(status='IN_PROGRESS', conclusion=None)
            runner = PrivateRunner([evidence(), payload] if rerun else [payload])
            self.assertEqual(self.invoke(runner), 2)
            self.assertEqual(runner.mutations, [])

    def test_failure_on_second_read_prevents_merge(self):
        payload = evidence()
        inventory(payload)['nodes'][0]['conclusion'] = 'FAILURE'
        self.assert_blocked(PrivateRunner([evidence(), payload]))

    def test_incomplete_unknown_or_ambiguous_inventory_is_not_green(self):
        payloads = [None, {}, {'errors': [{'message': 'unavailable'}]}, {'data': None}]
        for change in (
            {'nodes': [], 'totalCount': 0}, {'totalCount': 3}, {'totalCount': True},
            {'pageInfo': {'hasNextPage': True}}, {'pageInfo': {}}, {'nodes': [None, None]},
            {'nodes': [{'__typename': 'Unknown'}, {}]},
        ):
            payload = evidence()
            inventory(payload).update(change)
            payloads.append(payload)
        duplicate = evidence()
        inventory(duplicate)['nodes'][1]['context'] = 'test'
        payloads.append(duplicate)
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assert_blocked(PrivateRunner([payload]))
        self.assert_blocked(PrivateRunner(query_code=1))

    def test_current_head_identity_readiness_and_mergeability_are_mandatory(self):
        for key, value in (
            ('headRefOid', '9'*40), ('baseRefOid', '9'*40), ('baseRefName', 'other'),
            ('state', 'CLOSED'), ('isDraft', True), ('mergeable', 'UNKNOWN'),
            ('mergeable', 'CONFLICTING'),
        ):
            with self.subTest(key=key):
                payload = evidence()
                payload['data']['repository']['pullRequest'][key] = value
                self.assert_blocked(PrivateRunner([payload]))
        payload = evidence()
        payload['data']['repository']['pullRequest']['commits']['nodes'][0]['commit']['oid'] = '9'*40
        self.assert_blocked(PrivateRunner([payload]))

    def test_pr_drift_after_inventory_never_merges(self):
        original = Runner().states[0]
        for field, value in (('headRefOid', '9'*40), ('baseRefOid', '9'*40), ('isDraft', True)):
            for index in (1, 2):
                with self.subTest(field=field, index=index):
                    changed = {**original, field: value}
                    self.assert_blocked(PrivateRunner(states=[deepcopy(original)]*index+[changed]))

    def test_unknown_status_context_and_checkrun_state_never_merge(self):
        for kind, key, value in ((0, 'status', 'UNKNOWN'), (1, 'state', 'ERROR'), (1, 'state', None)):
            payload = evidence()
            inventory(payload)['nodes'][kind][key] = value
            self.assert_blocked(PrivateRunner([payload]))

    def test_merge_cli_passes_explicit_contract_to_guard(self):
        with patch.object(p, 'guarded_merge', return_value=0) as merge:
            self.assertEqual(p.main(['--repo', 'o/r', '--pr', '8', 'merge',
                '--expected-head', HEAD, '--required-check', 'test',
                '--required-check', 'workflow-lint']), 0)
        self.assertEqual(merge.call_args.kwargs['expected_head'], HEAD)
        self.assertEqual(merge.call_args.kwargs['required_checks'], ('test', 'workflow-lint'))

    def test_finish_cli_keeps_contract_through_delivery(self):
        with patch('git_janitor.delivery.finish_delivery', return_value=0) as finish:
            self.assertEqual(p.main(['--repo', 'o/r', '--pr', '8', 'finish',
                '--worktree', '/owned/task', '--expected-head', HEAD,
                '--required-check', 'test', '--required-check', 'workflow-lint']), 0)
        self.assertEqual(finish.call_args.kwargs['merge_options']['required_checks'], ('test', 'workflow-lint'))
