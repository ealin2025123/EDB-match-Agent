import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from excel_match_agent.app import BackendUnavailable, run_job
from excel_match_agent.deepseek import DeepSeekBackend, completion_url, validate_decision, model_context, review_context
from excel_match_agent.tests.test_runtime import CODE, TestExecutor
from excel_match_agent.rule_derivation import FIELDS as DERIVATION_FIELDS


def answer(code=CODE, **changes):
    decision = {"decision": "generate", "python_code": code, "explanation": "按传入Skill生成",
                "derived_rules": {name: "离线CSV夹具：单位读取C列；期间读取D列，位置以CSV真实表头为准" for name in DERIVATION_FIELDS}}
    choice = {"message": {"content": json.dumps(decision)}, "finish_reason": "stop"}
    choice.update(changes)
    return io.BytesIO(json.dumps({"model": "dc", "choices": [choice]}).encode("utf-8"))


def stream_answer(*events):
    return io.BytesIO(b''.join(b'data: ' + (b'[DONE]' if event == '[DONE]' else json.dumps(event, ensure_ascii=False).encode('utf-8')) + b'\n\n' for event in events))


def csv_preflight_answer(payload):
    if '[source_preflight_v1]' in payload['messages'][0]['content']:
        return answer(message={'content':json.dumps({'decision':'has_relevant_data','reason':'离线夹具包含GDP数值',
            'related_indicator_names':['GDP'],'evidence':['GDP | 0 | unit']})})
    return None


class DeepSeekTests(unittest.TestCase):
    def test_review_cache_revalidates_original_response_without_call(self):
        row = {'indicator_name': 'GDP', 'source': {'sheet': 's', 'cell': 'B1'}}
        review = {'name_status': 'correct', 'explanation': 'fixture original evidence', 'issues': []}
        cache = self.folder / 'old'
        cache.mkdir()
        raw = json.dumps({'content': json.dumps(review)}, ensure_ascii=False)
        (cache / 'model-response-raw.json').write_text(raw, encoding='utf-8')
        (cache / 'model-response.json').write_text(json.dumps(review), encoding='utf-8')
        current = self.folder / 'current'
        current.mkdir()
        def forbidden(*args, **kwargs):
            raise AssertionError('Valid original review must not request the model again')
        result = self.backend(forbidden).review_names({'parsed_data': [row], 'GZKJG': [],
            '_review_cache_dir': str(cache)}, current)
        self.assertEqual(result, review)
        self.assertEqual((current / 'model-response-raw.json').read_text(encoding='utf-8'), raw)

    def test_scoped_review_rejects_other_batches_even_if_numeric_source_exists(self):
        rows = [{'indicator_name': 'GDP', 'source': {'sheet': 's', 'cell': 'B1'}}]
        review = {'name_status': 'incorrect', 'explanation': 'fixture', 'issues': [
            {'indicator_name': 'elsewhere', 'source': {'sheet': 's', 'cell': 'B2'}, 'reason': 'fixture omitted'}]}
        def transport(request, timeout):
            return answer(message={'content': json.dumps(review)})
        from excel_match_agent.app import NameReviewProtocolError
        with self.assertRaisesRegex(NameReviewProtocolError, '分批审核只能引用本批'):
            self.backend(transport).review_names({'parsed_data': rows, 'GZKJG': [],
                'review_scope': 'only B1', 'workbook': {'sheets': [{'name': 's', 'rows': [['GDP', 1], ['elsewhere', 2]]}]}}, self.folder)

    def test_review_context_deduplicates_only_identical_evidence(self):
        import copy
        rule = {'rule_indicator_name': 'GDP', 'name_reference': [{'row': 1, 'parts': ['GDP']}]}
        ctx = {'skill': 'generation only', 'parser_code': 'generation code', 'matching': {'OUTDATA': []},
               'workbook': {'sheets': [{'name': 's', 'rows': [['GDP', 1]]}]},
               'name_comparison_rules': [rule], 'near_name_candidates': [
                   {'candidates': [copy.deepcopy(rule)], 'source_evidence': {
                       'sheet': 's', 'nearby_rows': [{'row': 1, 'cells': ['GDP', 1]}]}},
                   {'source_evidence': {'sheet': 's', 'nearby_rows': [{'row': 1, 'cells': ['DIFFERENT', 1]}]}}]}
        before = copy.deepcopy(ctx)
        compact = review_context(ctx)
        self.assertEqual(ctx, before)
        self.assertNotIn('skill', compact)
        self.assertEqual(compact['workbook'], ctx['workbook'])
        candidate = compact['near_name_candidates'][0]
        self.assertEqual(candidate['source_evidence']['workbook_row_numbers'], [1])
        self.assertEqual(candidate['candidates'][0]['name_reference_lookup'], 'GDP')
        self.assertIn('nearby_rows', compact['near_name_candidates'][1]['source_evidence'])

    def test_batch_review_covers_all_rows_and_keeps_errors(self):
        rows = [{'indicator_name': 'wrong' if i == 12 else 'GDP', 'source': {'sheet': 's', 'cell': f'B{i+1}'}}
                for i in range(13)]
        rule = {'rule_indicator_name': 'GDP', 'rule_indicator_code': '1'}
        contexts = []
        def transport(request, timeout):
            ctx = json.loads(json.loads(request.data)['messages'][1]['content'].split('\n', 1)[1])
            contexts.append(ctx)
            issues = [{**r, 'reason': 'fixture wrong', 'target_indicator_name': 'GDP'}
                      for r in ctx['parsed_data'] if r['indicator_name'] == 'wrong']
            reviews = [{**c, 'decision': 'same_indicator', 'target_indicator_name': 'GDP', 'reason': 'fixture',
                        'compatibility': {'rule_indicator_name': 'GDP', 'unit': 'unknown', 'frequency': 'unknown', 'reason': 'fixture'}}
                       for c in ctx['parsed_data'] if c['indicator_name'] == 'wrong']
            result = {'name_status': 'incorrect' if issues else 'correct', 'explanation': 'fixture',
                      'issues': issues, 'candidate_reviews': reviews}
            return answer(message={'content': json.dumps(result)})
        result = self.backend(transport).review_names({'parsed_data': rows, 'GZKJG': [rule],
            'near_name_candidates': [{**rows[-1], 'candidates': [rule]}],
            'workbook': {'sheets': [{'name': 's', 'rows': [['GDP', i] for i in range(13)]}]}}, self.folder)
        self.assertEqual([len(c['parsed_data']) for c in contexts], [12, 1])
        self.assertEqual(result['name_status'], 'incorrect')
        self.assertEqual(result['issues'][0]['source']['cell'], 'B13')
        self.assertTrue(all(len(c['workbook']['sheets'][0]['rows']) == 13 for c in contexts))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"TEST_DEEPSEEK_KEY": "test-placeholder-only"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def backend(self, transport, **kwargs):
        return DeepSeekBackend("https://api.example.invalid/v1", "dc", "TEST_DEEPSEEK_KEY", transport=transport, **kwargs)

    def test_request_and_secret_handling(self):
        requests = []
        def transport(req, timeout):
            requests.append(req)
            return answer()
        result = self.backend(transport).generate({"GZKJG": [], "skill": "生成Python", "history": []}, self.folder)
        self.assertEqual(result["python_code"], CODE)
        request = requests[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.example.invalid/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-placeholder-only")
        self.assertEqual(body["model"], "dc")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        for path in self.folder.iterdir():
            self.assertNotIn("test-placeholder-only", path.read_text(encoding="utf-8"))

    def test_private_service_can_disable_json_mode_and_auth(self):
        captured = []
        backend = DeepSeekBackend("http://localhost:8000/v1", "dc", "MISSING_TEST_KEY", require_api_key=False,
                                 json_mode=False, transport=lambda req, timeout: (captured.append(req) or answer()))
        backend.generate({}, self.folder)
        self.assertNotIn("response_format", json.loads(captured[0].data))
        self.assertIsNone(captured[0].get_header("Authorization"))

    def test_missing_key_does_not_call_transport(self):
        backend = DeepSeekBackend("https://example.invalid", "dc", "NO_SUCH_AGENT_TEST_KEY",
                                 transport=lambda *a, **k: self.fail("没有key不应调用接口"))
        with self.assertRaises(BackendUnavailable):
            backend.generate({}, self.folder)

    def test_http_error_does_not_leak_response_body(self):
        def transport(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "bad test-placeholder-only", {}, io.BytesIO(b"test-placeholder-only"))
        with self.assertRaises(BackendUnavailable) as caught:
            self.backend(transport).generate({}, self.folder)
        self.assertNotIn("test-placeholder-only", str(caught.exception))

    def test_reasoning_is_not_code(self):
        backend = self.backend(lambda req, timeout: answer(message={"content": "", "reasoning_content": CODE}))
        with self.assertRaises(ValueError):
            backend.generate({}, self.folder)

    def test_streamed_code_joins_chunks_and_ignores_reasoning(self):
        content = json.dumps({'decision': 'generate', 'python_code': CODE, 'explanation': '真实content'}, ensure_ascii=False)
        sent = []
        def transport(request, timeout):
            sent.append(request)
            return stream_answer(
                {'model': 'dc-stream', 'choices': [{'index': 0, 'delta': {'reasoning_content': 'REASONING_ONLY_MARKER'}}]},
                {'choices': [{'index': 0, 'delta': {'content': content[:30]}}]},
                {'choices': [{'index': 0, 'delta': {'content': content[30:]}, 'finish_reason': 'stop'}]},
                {'choices': [], 'usage': {'completion_tokens': 10}}, '[DONE]')
        result = self.backend(transport, stream=True).generate({}, self.folder)
        self.assertEqual(result['python_code'], CODE)
        self.assertTrue(json.loads(sent[0].data)['stream'])
        self.assertEqual(sent[0].get_header('Accept'), 'text/event-stream')
        meta = json.loads((self.folder/'model-metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['model'], 'dc-stream')
        self.assertEqual(meta['usage']['completion_tokens'], 10)
        for path in self.folder.iterdir():
            self.assertNotIn('REASONING_ONLY_MARKER', path.read_text(encoding='utf-8'))

    def test_stream_without_finish_cannot_execute_partial_code(self):
        transport = lambda *a, **k: stream_answer({'choices': [{'delta': {'content': '{'}}]})
        with self.assertRaisesRegex(BackendUnavailable, 'finish_reason'):
            self.backend(transport, stream=True).generate({}, self.folder)
        self.assertFalse((self.folder/'model-response.json').exists())

    def test_stream_reasoning_cannot_replace_content(self):
        transport = lambda *a, **k: stream_answer({'choices': [{'delta': {'reasoning_content': CODE}, 'finish_reason': 'stop'}]}, '[DONE]')
        with self.assertRaisesRegex(ValueError, '空content'):
            self.backend(transport, stream=True).generate({}, self.folder)

    def test_stream_length_is_reported_as_truncated(self):
        transport = lambda *a, **k: stream_answer({'choices': [{'delta': {'content': '{'}, 'finish_reason': 'length'}]}, '[DONE]')
        with self.assertRaisesRegex(ValueError, '截断'):
            self.backend(transport, stream=True).generate({}, self.folder)

    def test_stream_error_does_not_echo_upstream_secrets(self):
        transport = lambda *a, **k: stream_answer({'error': {'message': 'test-placeholder-only'}})
        with self.assertRaises(BackendUnavailable) as caught:
            self.backend(transport, stream=True).generate({}, self.folder)
        self.assertNotIn('test-placeholder-only', str(caught.exception))

    def test_truncation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "截断"):
            self.backend(lambda req, timeout: answer(finish_reason="length")).generate({}, self.folder)

    def test_decision_validation(self):
        for content in ('{}', '{"decision":"generate","python_code":"","explanation":"x"}',
                        '{"decision":"wrong","python_code":"x","explanation":"x"}'):
            with self.assertRaises(ValueError):
                validate_decision(content)

    def test_python_fence_is_unwrapped_without_changing_generated_source(self):
        response = {"decision": "generate", "python_code": "```python\n" + CODE.rstrip('\n') + "\n```", "explanation": "x"}
        self.assertEqual(validate_decision(json.dumps(response))['python_code'], CODE.rstrip('\n'))
        response['python_code'] = 'some prose\n' + response['python_code']
        self.assertTrue(validate_decision(json.dumps(response))['python_code'].startswith('some prose'))

    def test_context_compaction_preserves_evidence_and_avoids_duplicate_candidates(self):
        candidates = [{'indicator_name': 'source name', 'evidence': 'x' * 400000}]
        context = {'workbook': {'sheets': ['original source']}, 'near_name_candidates': candidates,
                   'parsed_data': [{'indicator_data': '0'}], 'parser_code': CODE,
                   'matching': {'metrics': {'parsed_count': 1}, 'near_name_candidates': candidates},
                   'history': [{'python_code': CODE, 'evaluation': {'name_review': {'issues': ['actual feedback']},
                                                                  'near_name_candidates': candidates, 'OUTDATA': []}}]}
        compact = model_context(context)
        self.assertLess(len(json.dumps(compact)), 500000)
        self.assertEqual(compact['workbook'], context['workbook'])
        self.assertEqual(compact['near_name_candidates'], candidates)
        self.assertEqual(compact['history'][0]['evaluation']['name_review']['issues'], ['actual feedback'])
        self.assertIn('near_name_candidates', context['matching'])

    def test_invalid_review_reasks_review_without_generating_a_new_parser(self):
        sent = []
        review = {'name_status': 'correct', 'explanation': '名称正确', 'issues': [], 'candidate_reviews': []}
        def transport(request, timeout):
            sent.append(json.loads(request.data))
            return answer(message={'content': '{}' if len(sent) == 1 else json.dumps(review)})
        result = self.backend(transport).review_names({'parsed_data': [], 'GZKJG': [], 'near_name_candidates': []}, self.folder)
        self.assertEqual(result, review)
        self.assertEqual(len(sent), 2)
        context = json.loads(sent[1]['messages'][1]['content'].split('\n', 1)[1])
        self.assertIn('review_protocol_feedback', context)
        self.assertEqual(context['parsed_data'], [])

    def test_negative_name_audit_can_recover_a_scope_prefix_omission(self):
        row = {'indicator_name': '工业增加值同比', 'source': {'sheet': 's', 'cell': 'B3'}}
        target = '规模以上工业增加值同比'
        comparison = {'rule_indicator_name': target, 'rule_indicator_code': '100'}
        candidate = {**row, 'candidates': [comparison]}
        compatibility = {'rule_indicator_name': target, 'unit': 'compatible', 'frequency': 'unknown', 'reason': '源标题限定与总体行，频率未明'}
        initial = {'name_status': 'correct', 'explanation': '离线模拟初审遗漏', 'issues': [], 'candidate_reviews': [
            {**row, 'decision': 'different_indicator', 'target_indicator_name': '', 'reason': '离线模拟错误否定', 'compatibility': compatibility}]}
        issue = {**row, 'reason': '离线夹具：源标题限定了该总量行', 'target_indicator_name': target}
        corrected = {'name_status': 'incorrect', 'explanation': '源标题需继承', 'issues': [issue], 'candidate_reviews': [
            {**issue, 'decision': 'same_indicator', 'compatibility': compatibility}]}
        sent = []
        def transport(request, timeout):
            sent.append(json.loads(request.data))
            return answer(message={'content': json.dumps(initial if len(sent) == 1 else corrected)})
        context = {'parsed_data': [row], 'GZKJG': [comparison], 'near_name_candidates': [candidate],
                   'workbook': {'sheets': [{'name': 's', 'rows': [['规模以上工业增加值'], ['同比'], ['工业增加值', 3.5]]}]}}
        result = self.backend(transport).review_names(context, self.folder)
        self.assertEqual(result['name_status'], 'incorrect')
        self.assertEqual(result['issues'], [issue])
        self.assertEqual(len(sent), 2)
        self.assertFalse((self.folder / 'parser.py').exists())

    def test_negative_name_audit_can_confirm_distinct_incompatible_measure(self):
        row = {'indicator_name': '投资', 'source': {'sheet': 's', 'cell': 'B2'}}
        target = '投资面积'
        rule = {'rule_indicator_name': target, 'rule_indicator_code': '100'}
        review = {'name_status': 'correct', 'explanation': '金额和面积口径不同', 'issues': [], 'candidate_reviews': [
            {**row, 'decision': 'different_indicator', 'target_indicator_name': '', 'reason': '金额与面积不同',
             'compatibility': {'rule_indicator_name': target, 'unit': 'incompatible', 'frequency': 'unknown', 'reason': '单位维度不同'}}]}
        calls = []
        def transport(request, timeout):
            calls.append(request)
            return answer(message={'content': json.dumps(review)})
        context = {'parsed_data': [row], 'GZKJG': [rule], 'near_name_candidates': [{**row, 'candidates': [rule]}]}
        result = self.backend(transport).review_names(context, self.folder)
        self.assertEqual(result['name_status'], 'correct')
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['candidate_reviews'][0]['decision'], 'different_indicator')

    def test_repeated_invalid_review_stops_without_regenerating_code(self):
        (self.folder / 'source.csv').write_text('name,value,unit,date\nGDP,0,unit,2026-06-30\nGDP2,1,unit,2026-06-30\n', encoding='utf-8')
        (self.folder / 'SKILL.md').write_text('按列读取', encoding='utf-8')
        sent = []
        def transport(request, timeout):
            if '[source_preflight_v1]' in json.loads(request.data)['messages'][0]['content']:
                return answer(message={'content':json.dumps({'decision':'has_relevant_data','reason':'真实夹具有GDP数值',
                    'related_indicator_names':['GDP'],'evidence':['GDP | 0 | unit']})})
            sent.append(request)
            return answer() if len(sent) == 1 else answer(message={'content': '{}'})
        job = {'source_path': 'source.csv', 'skill_path': 'SKILL.md', 'output_dir': 'runs', 'GZKJG': [
            {'rule_indicator_name': 'GDP', 'rule_indicator_code': '1'}, {'rule_indicator_name': 'GDP2', 'rule_indicator_code': '2'}]}
        result = run_job(job, self.folder, self.backend(transport), TestExecutor())
        self.assertEqual(result['failure_kind'], 'NAME_REVIEW_PROTOCOL_ERROR')
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(len(sent), 3)
        self.assertFalse((Path(result['output_dir']) / 'node-output.json').exists())

    def test_url_normalization(self):
        self.assertEqual(completion_url("https://api.deepseek.com"), "https://api.deepseek.com/chat/completions")
        self.assertEqual(completion_url("https://example.invalid/v1/chat/completions/"), "https://example.invalid/v1/chat/completions")
        with self.assertRaises(ValueError):
            completion_url("https://user:secret@example.invalid")

    def test_configured_dc_is_never_substituted(self):
        with patch('excel_match_agent.deepseek.load_environment'), patch.dict(os.environ, {"DEEPSEEK_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_MODEL": "dc", "DEEPSEEK_API_KEY_ENV": ""}):
            backend = DeepSeekBackend.from_job({})
        self.assertEqual(backend.model, "dc")
        self.assertEqual(backend.api_key_env, "DEEPSEEK_API_KEY")

    def test_gateway_profile_does_not_send_the_official_key(self):
        env = {'DEEPSEEK_BASE_URL': 'https://llm.example.invalid/v1', 'DEEPSEEK_MODEL': 'bl-deepseek-v4.1-flash',
               'DEEPSEEK_API_KEY_ENV': 'CUSTOM_LLM_API_TOKEN', 'DEEPSEEK_API_KEY': 'official-test-key', 'CUSTOM_LLM_API_TOKEN': ''}
        with patch('excel_match_agent.deepseek.load_environment'), patch.dict(os.environ, env):
            backend = DeepSeekBackend.from_job({})
            self.assertEqual(backend.url, 'https://llm.example.invalid/v1/chat/completions')
            self.assertEqual(backend.api_key_env, 'CUSTOM_LLM_API_TOKEN')
            with self.assertRaisesRegex(BackendUnavailable, 'CUSTOM_LLM_API_TOKEN'):
                backend.headers()
            os.environ['CUSTOM_LLM_API_TOKEN'] = 'internal-test-token'
            self.assertEqual(backend.headers()['Authorization'], 'Bearer internal-test-token')

    def test_probe_reports_unavailable_without_changing_model(self):
        captured = []
        def transport(request, timeout):
            captured.append(request)
            return io.BytesIO(b'{"data":[{"id":"some-other-model"}]}')
        backend = self.backend(transport)
        result = backend.probe_model()
        self.assertFalse(result["model_available"])
        self.assertEqual(backend.model, "dc")
        self.assertEqual(captured[0].get_method(), "GET")
        self.assertTrue(captured[0].full_url.endswith("/v1/models"))

    def test_protocol_through_repair_loop_without_other_model_process(self):
        (self.folder / "source.csv").write_text("name,value,unit,date\nGDP,0,亿元,2026-06-30\nGDP2,1,亿元,2026-06-30\n", encoding="utf-8")
        (self.folder / "SKILL.md").write_text("按列抽取，不改原值", encoding="utf-8")
        sent = []
        def transport(request, timeout):
            if '[source_preflight_v1]' in json.loads(request.data)['messages'][0]['content']:
                return answer(message={'content':json.dumps({'decision':'has_relevant_data','reason':'真实夹具有GDP数值',
                    'related_indicator_names':['GDP'],'evidence':['GDP | 0 | 亿元']})})
            sent.append(json.loads(request.data))
            if len(sent) in {1, 3}:
                return answer(CODE.replace('row[0]', '"wrong"') if len(sent) == 1 else CODE)
            ctx = json.loads(sent[-1]["messages"][1]["content"].split("\n", 1)[1])
            review = {"name_status": "incorrect" if len(sent) == 2 else "correct", "explanation": "依据源表名称核对",
                      "candidate_reviews": [{"indicator_name": c["indicator_name"], "source": c["source"],
                                             "decision": "different_indicator", "target_indicator_name": "",
                                             "compatibility": {"rule_indicator_name": c["candidates"][0]["rule_indicator_name"],
                                                               "unit": "unknown", "frequency": "unknown", "reason": "离线夹具未判定兼容性，wrong本身错误"},
                                             "reason": "测试中的wrong是解析错误而非标准名同义词"}
                                            for c in ctx.get("near_name_candidates", [])],
                      "issues": [{"indicator_name": "wrong", "source": {"sheet": "source", "cell": "B2"},
                                  "reason": "源表名为GDP，不能替换为wrong"}] if len(sent) == 2 else []}
            return answer(message={"content": json.dumps(review)})
        job = {"source_path": "source.csv", "skill_path": "SKILL.md", "output_dir": "runs",
               "GZKJG": [{"rule_indicator_name": "GDP", "rule_indicator_code": "123", "rule_constant_name": "万元"},
                         {"rule_indicator_name": "GDP2", "rule_indicator_code": "124", "rule_constant_name": "亿元"}]}
        with patch("excel_match_agent.app.subprocess.Popen", side_effect=AssertionError("模型调用不得启动其他模型程序")):
            result = run_job(job, self.folder, self.backend(transport), TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(result["model_provider"], "deepseek")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(sent), 4)
        self.assertIn("NAME_CONSTRUCTION_ERROR", sent[2]["messages"][1]["content"])
        self.assertTrue((Path(result["output_dir"]) / "node-output.json").exists())


if __name__ == "__main__":
    unittest.main()
