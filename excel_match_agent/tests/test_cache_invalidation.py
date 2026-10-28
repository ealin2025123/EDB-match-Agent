"""清除失败复用不删除历史、不自动执行；全部为离线合成任务。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from excel_match_agent.service import TaskService, invalidate_job_cache
from excel_match_agent.tests.test_service import REQUEST


class CacheInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        profile = self.root/'profile.json'
        profile.write_text('{}',encoding='utf-8')
        self.release = threading.Event()
        self.block = False
        self.calls = 0
        def runner(request,result_path):
            self.calls += 1
            if self.block:
                self.release.wait(5)
            result = {'status':'needs_review','failure_kind':'INPUT_OR_RUNTIME_ERROR'}
            result_path.write_text(json.dumps(result),encoding='utf-8')
            return result
        self.service = TaskService({'runtime_dir':str(self.root/'runtime'),'profiles':{'excel':str(profile)}},runner=runner)
        self.addCleanup(self.service.close)
        self.addCleanup(self.release.set)
        self.database = self.root/'runtime/tasks.sqlite3'

    def failed(self, request=REQUEST):
        first,_ = self.service.submit(request)
        for _ in range(40):
            result = self.service.query(first['job_id'],.1)
            if result['status']=='failed':
                return result
        self.fail('离线任务未结束')

    def test_clear_keeps_records_and_creates_no_task_until_user_submits(self):
        first = self.failed()
        job_id = first['job_id']
        reused,hit = self.service.submit(REQUEST)
        self.assertTrue(hit)
        self.assertEqual(reused['job_id'],job_id)
        with self.service.condition:
            before = tuple(self.service.db.execute('SELECT * FROM tasks WHERE id=?',(job_id,)).fetchone())
        saved = self.root/'runtime'/job_id/'agent-result.json'
        original = saved.read_bytes()
        invalidated = invalidate_job_cache(self.database,job_id)
        self.assertFalse(invalidated['new_job_started'])
        self.assertEqual(self.calls,1)
        self.assertEqual(tuple(self.service.db.execute('SELECT * FROM tasks WHERE id=?',(job_id,)).fetchone()),before)
        self.assertEqual(saved.read_bytes(),original)
        self.assertTrue(self.service.query(job_id)['cache_invalidated'])
        next_job,hit = self.service.submit(REQUEST)
        self.assertFalse(hit)
        self.assertNotEqual(next_job['job_id'],job_id)

    def test_workflow_binding_is_released_with_original_id_preserved_in_audit(self):
        request = {**REQUEST,'workflow_run_id':'same-workflow-test'}
        first = self.failed(request)
        invalidate_job_cache(self.database,first['job_id'])
        invalidate_job_cache(self.database,first['job_id'])
        row = self.service.db.execute('SELECT previous_workflow_id FROM task_cache_invalidations WHERE task_id=?',(first['job_id'],)).fetchone()
        self.assertEqual(row[0],'same-workflow-test')
        payload = self.service.db.execute('SELECT payload FROM tasks WHERE id=?',(first['job_id'],)).fetchone()[0]
        self.assertEqual(json.loads(payload)['workflow_run_id'],'same-workflow-test')
        next_job,hit = self.service.submit(request)
        self.assertFalse(hit)
        self.assertNotEqual(next_job['job_id'],first['job_id'])

    def test_running_task_is_not_cancelled_by_clear(self):
        self.block = True
        first,_ = self.service.submit(REQUEST)
        with self.assertRaisesRegex(ValueError,'已失败或中断'):
            invalidate_job_cache(self.database,first['job_id'])
        self.assertIn(self.service.query(first['job_id'])['status'],{'queued','running'})


if __name__ == '__main__':
    unittest.main()
