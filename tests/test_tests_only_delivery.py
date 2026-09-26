from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from git_janitor import pr_shepherd as p, review_chain
from git_janitor.models import CommandResult

HEAD, BASE, MERGED = '1' * 40, '2' * 40, '3' * 40


class Runner:
    def __init__(self, checks=None, states=None, check_codes=None, auto_error=''):
        self.checks = checks if checks is not None else [[{'bucket': 'pass'}]]
        self.states = states or [{'number': 8, 'headRefOid': HEAD, 'baseRefOid': BASE,
                                 'baseRefName': 'main', 'state': 'OPEN', 'isDraft': False}]
        self.check_codes = check_codes or [0]
        self.auto_error = auto_error
        self.calls, self.reads, self.check_reads = [], 0, 0

    def __call__(self, args, cwd, timeout):
        self.calls.append(args)
        code, result, error = 0, '', ''
        if args[:3] == ['gh', 'pr', 'view']:
            result = json.dumps(self.states[min(self.reads, len(self.states) - 1)])
            self.reads += 1
        elif args[:3] == ['gh', 'pr', 'checks']:
            result = json.dumps(self.checks[min(self.check_reads, len(self.checks) - 1)])
            code = self.check_codes[min(self.check_reads, len(self.check_codes) - 1)]
            self.check_reads += 1
        elif args[:3] == ['gh', 'pr', 'merge']:
            code, error = (1, self.auto_error) if self.auto_error else (0, '')
        elif args[:4] == ['gh', 'api', '--method', 'PUT']:
            result = json.dumps({'merged': True, 'sha': MERGED})
        elif args == ['gh', 'api', f'repos/o/r/git/commits/{MERGED}']:
            result = json.dumps({'parents': [{'sha': BASE}]})
        elif args == ['gh', 'api', 'repos/o/r/pulls/8']:
            result = json.dumps({'merged': True, 'head': {'sha': HEAD}})
        else:
            raise AssertionError(f'Unexpected external action: {args}')
        return CommandResult(args, code, result, error)

    @property
    def mutations(self):
        return [a for a in self.calls if a[:3] == ['gh', 'pr', 'merge'] or a[:4] == ['gh', 'api', '--method', 'PUT']]


class TestsOnlyDeliveryTests(unittest.TestCase):
    def invoke(self, runner, *, protected=True, required=True, **kwargs):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(p, 'cached_or_refreshed_facts', return_value={'protected': protected, 'required_checks': ['tests'] if required else []}), \
             patch.object(p, 'grok_review_receipt_state', side_effect=AssertionError('receipt gate invoked')), \
             patch.object(p, 'unresolved_blocking_findings', side_effect=AssertionError('thread gate invoked')), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return p.guarded_merge(repo='o/r', pr=8, facts_path=Path(tmp)/'facts',
                                   alert_command=Path('/unused'), runner=runner, **kwargs)

    def test_review_cli_and_provider_compatibility_have_zero_external_calls(self):
        runner = Mock(side_effect=AssertionError('external access'))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(p.main(['review', '--high-risk', '--force'], runner=runner), 2)
            self.assertEqual(p.main(['review-outcome'], runner=runner), 2)
            self.assertEqual(p.run_provider_review(repo='o/r', pr=8, high_risk=True, runner=runner), 2)
            self.assertEqual(p.request_review_once(repo='o/r', pr=8, runner=runner), 'error')
        request = review_chain.ReviewRequest(Path('/missing'), 'o/r', 8, 'main', HEAD, BASE)
        result = review_chain.run_luna_review(request, runner=runner)
        self.assertEqual(result.status, review_chain.ReviewStatus.HARD_FAILURE)
        self.assertEqual(result.attempts, 0)
        runner.assert_not_called()
        self.assertFalse(p.CODE_REVIEW_ENABLED)
        self.assertEqual(p.MERGE_POLICY, 'tests-only')
        self.assertFalse(Path(p.__file__).with_name('glm_review_worker.py').exists())

    def test_green_required_tests_merge_without_review_readbacks(self):
        runner = Runner()
        self.assertEqual(self.invoke(runner), 0)
        self.assertEqual(len(runner.mutations), 1)
        self.assertIn('sha='+HEAD, runner.mutations[0])
        self.assertNotIn('--delete-branch', runner.mutations[0])
        self.assertEqual(runner.check_reads, 2)
        self.assertEqual(runner.reads, 3)

    def test_pending_checks_arm_protected_head_pinned_auto_merge(self):
        runner = Runner(checks=[[{'bucket':'pass'}, {'bucket':'pending'}]], check_codes=[8])
        self.assertEqual(self.invoke(runner), 0)
        self.assertIn('--auto', runner.mutations[0])
        self.assertEqual(runner.mutations[0][runner.mutations[0].index('--match-head-commit')+1], HEAD)

    def test_unavailable_auto_merge_returns_monitor_boundary(self):
        runner = Runner(checks=[[{'bucket':'pending'}]], check_codes=[8], auto_error='GraphQL: EnablePullRequestAutoMerge not allowed')
        self.assertEqual(self.invoke(runner), 2)
        self.assertEqual(len(runner.mutations), 1)

    def test_failed_missing_unknown_skipped_and_unreadable_checks_never_merge(self):
        for checks, code in [([],0), ({},0), ([{}],0), ([{'bucket':'fail'}],1), ([{'bucket':'skipping'}],0), ([{'bucket':'cancel'}],0), ([{'bucket':'pending'}],1)]:
            with self.subTest(checks=checks, code=code):
                runner=Runner(checks=[checks], check_codes=[code])
                self.assertNotEqual(self.invoke(runner, allow_unresolved=True, fix_forward=True, summary='old flags'), 0)
                self.assertEqual(runner.mutations, [])

    def test_missing_protection_requires_explicit_private_delivery_contract(self):
        for protected, required in [(False,True),(True,False)]:
            runner=Runner()
            self.assertEqual(self.invoke(runner, protected=protected, required=required),3)
            self.assertEqual(runner.mutations,[])

    def test_changed_head_base_or_draft_state_requires_fresh_validation(self):
        for field, value in [('headRefOid','4'*40),('baseRefOid','5'*40),('baseRefName','other'),('isDraft',True),('state','CLOSED')]:
            for read_number in (1,2):
                with self.subTest(field=field, read_number=read_number):
                    initial=Runner().states[0]
                    changed={**initial,field:value}
                    runner=Runner(states=[initial]*read_number+[changed])
                    self.assertEqual(self.invoke(runner),4)
                    self.assertEqual(runner.mutations,[])

    def test_check_rerun_or_failure_is_revalidated_before_mutation(self):
        runner=Runner(checks=[[{'bucket':'pass'}],[{'bucket':'fail'}]])
        self.assertEqual(self.invoke(runner),4)
        self.assertEqual(runner.mutations,[])
        runner=Runner(checks=[[{'bucket':'pass'}],[{'bucket':'pending'}]],check_codes=[0,8])
        self.assertEqual(self.invoke(runner),0)
        self.assertIn('--auto',runner.mutations[0])

    def test_delivery_head_mismatch_never_merges(self):
        runner=Runner()
        self.assertEqual(self.invoke(runner,expected_head='9'*40),4)
        self.assertEqual(runner.mutations,[])

    def test_squash_readback_detects_base_race_and_unverified_merge(self):
        for broken_part in ('parent', 'merged_head', 'merge_response'):
            with self.subTest(broken_part=broken_part):
                underlying = Runner()
                def runner(args, cwd, timeout):
                    result = underlying(args, cwd, timeout)
                    if broken_part == 'parent' and args == ['gh', 'api', f'repos/o/r/git/commits/{MERGED}']:
                        return CommandResult(args, 0, json.dumps({'parents': [{'sha': '9'*40}]}), '')
                    if broken_part == 'merged_head' and args == ['gh', 'api', 'repos/o/r/pulls/8']:
                        return CommandResult(args, 0, json.dumps({'merged': True, 'head': {'sha': '9'*40}}), '')
                    if broken_part == 'merge_response' and args[:4] == ['gh', 'api', '--method', 'PUT']:
                        return CommandResult(args, 0, '{}', '')
                    return result
                self.assertEqual(self.invoke(runner), 4)
                self.assertEqual(len(underlying.mutations), 1)
                self.assertFalse(any('--delete-branch' in a for a in underlying.calls))

    def test_review_decision_does_not_create_scanner_or_executor_gate(self):
        from git_janitor.autonomy import AutomationPolicy, decide_pull_request
        from git_janitor.config import ScannerConfig
        from git_janitor.execute import _merge_pr_drift_detail
        from git_janitor.models import PullRequestState
        for decision in ('REVIEW_REQUIRED', 'CHANGES_REQUESTED'):
            pr = PullRequestState(repo='o/r', number=8, title='Fix parser result', url=None,
                head_ref='fix/parser', base_ref='main', is_draft=False, merge_state='CLEAN',
                review_decision=decision, check_status='success', updated_at=None)
            result = decide_pull_request(pr, ScannerConfig(), AutomationPolicy(auto_merge_green_prs=True))
            self.assertEqual(result.category, 'merge-green-pr')
            self.assertEqual(_merge_pr_drift_detail({'state':'OPEN', 'is_draft':False,
                'check_status':'success', 'merge_state':'CLEAN', 'review_decision':decision}), '')

    def test_watch_never_queries_orphan_reviews(self):
        with patch.object(p,'load_cache',return_value={'repos':{'r':{}}}), \
             patch.object(p,'stalled_auto_merges',return_value=[]), \
             patch.object(p,'orphaned_findings',side_effect=AssertionError('review sweep')), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(p.daily_watch(owner='o',facts_path=Path('/unused'),min_age=timedelta(hours=2),days=7,alert_command=Path('/unused')),0)
        runner=Mock(side_effect=AssertionError('GitHub query'))
        self.assertEqual(p.orphaned_findings(owner='o',facts_path=Path('/unused'),days=7,runner=runner),[])
        runner.assert_not_called()
