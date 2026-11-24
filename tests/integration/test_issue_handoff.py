import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evo.adapters.persistence.sqlite_store import ConflictError
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.interfaces.workbench import Workbench
from evo.ports.harness import ArtifactRef, RunResult


class IssueHandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        self.project = {"id": "fixture", "name": "Fixture", "directory": self.state}
        self.app = Workbench(SQLiteApplication(self.state), project=self.project)
        self.issue = self.app.dispatch('create', {'prompt': 'Build and review a web app',
                                                   'squad': 'default'})['task']['id']
        self.lead = self.app._assignee(self.app.store.get_task(self.issue))

    def test_members_receive_archived_source_and_assets_before_leader_resumes(self):
        files = {'app.py': "print('hello')\n", 'static/app.js': 'console.log("hello");',
                 'static/style.css': 'body { color: green; }'}
        seen = []

        def factory(state, store, run, *, comment, assign_work, change_status, issue_artifacts, output_dir, **options):
            workspace = WorkspaceFilesystem(state, run['agent'], workspace_dir=output_dir)
            harness = Mock()
            harness.events.return_value = ()
            evidence = []

            def start(request):
                seen.append((run['kind'], run['agent']))
                if run['kind'] == 'assignment':
                    for path, content in files.items():
                        workspace.write_file(path, content, purpose='code')
                        evidence.append(ArtifactRef(path, 'code', hashlib.sha256(content.encode()).hexdigest()))
                    for teammate in ('builder', 'reviewer'):
                        handoff = assign_work(teammate, 'Verify the app')
                        self.assertTrue(handoff['finish_current_run'])
                    self.assertIsNone(self.app.queue.claim())
                    with self.assertRaisesRegex(ConflictError, 'Finish this run'):
                        self.app.queue.set_run_status(run, 'in_review')
                    self.assertEqual(self.app.dispatch('task', {'task_id': self.issue})['lane'], 'in_progress')
                else:
                    received = {item['path']: self.app.dispatch('artifact', {
                        'task_id': self.issue, 'event_id': item['event_id']})['content'] for item in issue_artifacts}
                    for path, content in files.items():
                        self.assertEqual(received[path], content)
                    if run['kind'] == 'mention':
                        reported = comment('@researcher Verification finished')
                        self.assertEqual(reported['run_ids'], [])
                        workspace.write_file(run['agent'] + '.md', 'Reviewed the actual source')
                        evidence.append(ArtifactRef(run['agent'] + '.md', 'document',
                            hashlib.sha256(b'Reviewed the actual source').hexdigest()))
                    else:
                        self.assertEqual({item['path'] for item in issue_artifacts},
                                         set(files) | {'builder.md', 'reviewer.md'})
                        self.assertTrue(change_status('in_review')['deferred'])
                harness.wait.return_value = RunResult('success', 'Finished', artifacts=tuple(evidence))

            harness.start.side_effect = start
            return workspace, harness

        with patch('evo.interfaces.execution_factory.create_issue_harness', side_effect=factory):
            for expected in [('assignment', 'researcher'), ('mention', 'builder'),
                             ('mention', 'reviewer'), ('handoff', 'researcher')]:
                run = self.app.queue.claim()
                self.assertEqual((run['kind'], run['agent']), expected)
                self.app._execute(run)
                if expected[0] != 'handoff':
                    self.assertEqual(self.app.dispatch('task', {'task_id': self.issue})['lane'], 'in_progress')
        self.assertEqual(len(seen), 4)
        self.assertEqual(self.app.dispatch('task', {'task_id': self.issue})['lane'], 'in_review')
        self.assertIsNone(self.app.queue.claim())

    def test_failed_producer_stops_dependents_instead_of_running_empty_qa(self):
        run = self.app.queue.claim()
        children = self.app.queue.comment(self.issue, '@builder @reviewer', self.app._agents(),
                                           sender=run['agent'], parent_run_id=run['id'],
                                           targets=('builder', 'reviewer'))['run_ids']
        self.app.queue.finish(run, 'failed', 'Artifact export failed', assignee=self.lead)
        self.assertIsNone(self.app.queue.claim())
        statuses = {item['id']: item['status'] for item in self.app.queue.list(self.issue)}
        self.assertEqual([statuses[item] for item in children], ['stopped', 'stopped'])
        self.assertEqual(self.app.dispatch('task', {'task_id': self.issue})['lane'], 'blocked')

    def test_failed_member_returns_control_to_leader_after_all_members_finish(self):
        parent = self.app.queue.claim()
        self.app.queue.comment(self.issue, '@builder @reviewer', self.app._agents(),
                               sender=parent['agent'], parent_run_id=parent['id'], targets=('builder', 'reviewer'))
        self.app.queue.finish(parent, 'completed', 'Source archived', assignee=self.lead)
        first = self.app.queue.claim()
        self.app.queue.finish(first, 'failed', 'QA could not verify', assignee=self.lead)
        second = self.app.queue.claim()
        self.app.queue.finish(second, 'completed', 'Release checked', assignee=self.lead)
        resumed = self.app.queue.claim()
        self.assertEqual((resumed['kind'], resumed['agent']), ('handoff', self.lead.name))

    def test_recovery_does_not_run_children_of_interrupted_producer(self):
        parent = self.app.queue.claim()
        self.app.queue.comment(self.issue, '@builder', self.app._agents(),
                               sender=parent['agent'], parent_run_id=parent['id'], targets=('builder',))
        self.app.queue.recover_interrupted()
        self.assertIsNone(self.app.queue.claim())

    def test_leader_handoff_waits_for_nested_member_work(self):
        parent = self.app.queue.claim()
        self.app.queue.comment(self.issue, '@builder @reviewer', self.app._agents(),
                               sender=parent['agent'], parent_run_id=parent['id'], targets=('builder', 'reviewer'))
        self.app.queue.finish(parent, 'completed', 'Ready', assignee=self.lead)
        member = self.app.queue.claim()
        self.app.queue.comment(self.issue, '@reviewer nested check', self.app._agents(),
                               sender=member['agent'], parent_run_id=member['id'], targets=('reviewer',))
        self.app.queue.finish(member, 'completed', 'Ready', assignee=self.lead)
        self.app.queue.finish(self.app.queue.claim(), 'completed', 'Sibling ready', assignee=self.lead)
        nested = self.app.queue.claim()
        self.app.queue.comment(self.issue, '@builder deeper check', self.app._agents(),
                               sender=nested['agent'], parent_run_id=nested['id'], targets=('builder',))
        self.app.queue.finish(nested, 'completed', 'Ready', assignee=self.lead)
        deeper = self.app.queue.claim()
        self.assertEqual(deeper['kind'], 'mention')
        self.assertEqual(deeper['parent_run_id'], nested['id'])
        self.assertIsNone(self.app.queue.claim())
        self.app.queue.finish(deeper, 'completed', 'Verified', assignee=self.lead)
        self.assertEqual(self.app.queue.claim()['kind'], 'handoff')

    def test_stop_before_start_is_stopped_and_does_not_launch_harness(self):
        run = self.app.queue.claim()
        self.app.queue.stop(self.issue, run['id'])
        harness = Mock()
        with patch('evo.interfaces.execution_factory.create_issue_harness',
                   return_value=(Mock(), harness)):
            self.app._execute(run)
        harness.start.assert_not_called()
        self.assertEqual(self.app.queue.list(self.issue)[0]['status'], 'stopped')
