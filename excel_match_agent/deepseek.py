"""DeepSeek Chat Completions生成后端，直接通过HTTP调用用户接口。"""
import json
import copy
from pathlib import Path
import shutil
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .app import BackendUnavailable, ModelOutputTruncated, NameReviewProtocolError, ModelContextTooLarge, MAX_CONTEXT_BYTES, MAX_OUTPUT_BYTES, PROMPT, SCHEMA, dump
from .environment import load_environment


def model_context(context):
    """只移除重复诊断；源表、规则、当前数据、候选及真实反馈完整保留。"""
    result = dict(context)
    if isinstance(context.get("matching"), dict):
        result["matching"] = {k: v for k, v in context["matching"].items() if k != "near_name_candidates"}
    if isinstance(context.get("history"), list):
        history = []
        for original in context["history"]:
            item = dict(original)
            if isinstance(item.get("evaluation"), dict):
                item["evaluation"] = {k: v for k, v in item["evaluation"].items()
                                      if k not in {"near_name_candidates", "OUTDATA", "unitArray", "unmatched"}}
            if context.get("parser_code") and item.get("python_code") == context["parser_code"]:
                item.pop("python_code")
            history.append(item)
        result["history"] = history
    return result


def review_context(context):
    """审核保留完整源表和名称依据；不重复发送生成用Skill、源码与匹配输出。"""
    keys = {'parsed_data', 'workbook', 'business_rules', 'GZKJG', 'name_comparison_rules',
            'name_review_basis', 'required_name_alignments', 'near_name_candidates',
            'source_name', 'pageTitle', 'fileMd5', 'publictime', 'upstream_metadata',
            'review_protocol_feedback', 'review_scope', 'other_parsed_records'}
    result = copy.deepcopy({k: v for k, v in context.items() if k in keys})
    sheets = {s['name']: s['rows'] for s in result.get('workbook', {}).get('sheets', [])}
    references = {r['rule_indicator_name']: r.get('name_reference', [])
                  for r in result.get('name_comparison_rules', [])}
    for candidate in result.get('near_name_candidates', []):
        evidence = candidate.get('source_evidence', {})
        nearby = evidence.get('nearby_rows')
        sheet = sheets.get(evidence.get('sheet'))
        if nearby and sheet is not None and all(
                type(r.get('row')) is int and 1 <= r['row'] <= len(sheet)
                and r.get('cells') == sheet[r['row'] - 1] for r in nearby):
            evidence['workbook_row_numbers'] = [r['row'] for r in nearby]
            evidence.pop('nearby_rows')
        for key in ('candidates', 'concatenation_candidates', 'gzk_candidates'):
            for rule in candidate.get(key, []):
                name = rule.get('rule_indicator_name')
                if 'name_reference' in rule and name in references and rule['name_reference'] == references[name]:
                    rule.pop('name_reference')
                    rule['name_reference_lookup'] = name
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # 不把业务数据和Authorization转发给重定向目标。
        return None


def completion_url(base_url):
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("DeepSeek base_url须为无用户名、密码、查询参数的HTTP(S)地址")
    base_url = base_url.rstrip("/")
    return base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"


def validate_decision(content, require_derivation=False):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("DeepSeek返回空content；不能拿reasoning_content代替解析代码")
    content = content.strip()
    match = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", content, flags=re.DOTALL)
    if match:
        content = match[1]
    response = json.loads(content)
    if (not isinstance(response, dict) or not {"decision", "python_code", "explanation"} <= set(response)
            or set(response) - {"decision", "python_code", "explanation", "derived_rules"}):
        raise ValueError("DeepSeek决策须包含decision、python_code、explanation及生成时的derived_rules")
    if response["decision"] not in {"generate", "needs_review"} or not all(isinstance(response[k], str) for k in ("python_code", "explanation")):
        raise ValueError("DeepSeek决策字段类型或取值不符")
    if response["decision"] == "generate" and not response["python_code"].strip():
        raise ValueError("DeepSeek选择generate但没有生成Python代码")
    if response["decision"] == "generate":
        fence = re.fullmatch(r"```(?:python|py)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*", response["python_code"].strip(), flags=re.DOTALL | re.IGNORECASE)
        if fence:
            # 只拆除完整Markdown外壳，业务源码仍全部来自DeepSeek，后续照常编译和检查。
            response["python_code"] = fence[1]
    if response["decision"] == "generate" and (require_derivation or "derived_rules" in response):
        from .rule_derivation import validate_derived_rules
        validate_derived_rules(response.get("derived_rules"))
    return response


class DeepSeekBackend:
    def __init__(self, base_url, model, api_key_env="DEEPSEEK_API_KEY", timeout=240, max_tokens=8192,
                 json_mode=True, require_api_key=True, extra_body=None, transport=None, stream=False):
        self.url = completion_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise ValueError("必须配置你自己的DeepSeek模型名")
        if not isinstance(api_key_env, str) or not api_key_env.strip():
            raise ValueError("api_key_env必须是环境变量名")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
            raise ValueError("max_tokens必须是正整数")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("model_timeout必须为正数")
        if not isinstance(json_mode, bool) or not isinstance(require_api_key, bool):
            raise ValueError("json_mode和require_api_key必须是布尔值")
        if not isinstance(stream, bool):
            raise ValueError("stream必须是布尔值")
        self.stream = stream
        self.model, self.api_key_env, self.timeout = model, api_key_env, timeout
        self.max_tokens, self.json_mode, self.require_api_key = max_tokens, json_mode, require_api_key
        self.extra_body = extra_body or {}
        if not isinstance(self.extra_body, dict) or set(self.extra_body) & {"model", "messages", "stream", "max_tokens", "response_format"}:
            raise ValueError("extra_body不能覆盖model/messages/stream/max_tokens/response_format")
        self.transport = transport or urllib.request.build_opener(NoRedirect()).open

    @classmethod
    def from_job(cls, job):
        load_environment()
        config = job.get("deepseek", {})
        if not isinstance(config, dict):
            raise ValueError("deepseek配置必须为对象")
        base_url = config.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL")
        model = config.get("model") or os.environ.get("DEEPSEEK_MODEL")
        if not base_url or not model:
            raise BackendUnavailable("请配置DeepSeek base_url和model，或设置DEEPSEEK_BASE_URL、DEEPSEEK_MODEL；不会回退到其他模型")
        return cls(base_url, model, config.get("api_key_env") or os.environ.get("DEEPSEEK_API_KEY_ENV") or "DEEPSEEK_API_KEY",
                   job.get("model_timeout", 240), config.get("max_tokens", 8192), config.get("json_mode", True),
                   config.get("require_api_key", True), config.get("extra_body"), stream=config.get("stream", False))

    def generate(self, context, folder):
        system = PROMPT + "\n请返回JSON对象，generate包含完整python_code及derived_rules，needs_review解释缺失证据。输出schema：" + json.dumps(SCHEMA, ensure_ascii=False)
        from .output_checks import DATE_REPAIR
        system += '\n执行后空提取或缺日期将进入同一生成预算重建，output_repair为必须修复的问题。' + DATE_REPAIR
        return self._complete(system, context, folder, SCHEMA,
                              lambda content: validate_decision(content, require_derivation=bool(context.get("rule_derivation_policy"))))

    def review_names(self, context, folder):
        # 每批只审核指定的真实记录，完整工作表始终提供作口径证据。
        # 分批降低单次回复被token上限截断的风险，不改变业务判断或重建计数。
        rows = context.get('parsed_data', [])
        if len(rows) > 12:
            from .name_review import validate_review
            identity = lambda r: (r['indicator_name'], r['source']['sheet'], r['source']['cell'])
            reviews = []
            for offset in range(0, len(rows), 12):
                subset = rows[offset:offset + 12]
                cells = {(r['source']['sheet'], r['source']['cell']) for r in subset}
                names = {identity(r) for r in subset}
                child = folder / ('batch-' + str(offset // 12 + 1))
                child.mkdir()
                scoped = {**context, 'parsed_data': subset,
                          'near_name_candidates': [c for c in context.get('near_name_candidates', []) if identity(c) in names],
                          'required_name_alignments': [r for r in context.get('required_name_alignments', [])
                              if (r['source']['sheet'], r['source']['cell']) in cells],
                          'review_scope': '仅审核本批parsed_data记录；完整workbook用于核对其标题、父级和口径。其他记录由其他批审核，不重复输出。'}
                scoped['other_parsed_records'] = [{k: r[k] for k in ('indicator_name', 'source')}
                                                 for r in rows if identity(r) not in names]
                if context.get('_review_cache_dir'):
                    scoped['_review_cache_dir'] = str(Path(context['_review_cache_dir']) / child.name)
                reviews.append(self.review_names(scoped, child))
            status = ('incorrect' if any(r['name_status'] == 'incorrect' for r in reviews) else
                      'uncertain' if any(r['name_status'] == 'uncertain' for r in reviews) else 'correct')
            combined = {'name_status': status, 'explanation': '\n'.join(r['explanation'] for r in reviews),
                        'issues': [i for r in reviews for i in r['issues']],
                        'candidate_reviews': [i for r in reviews for i in r.get('candidate_reviews', [])]}
            validated = validate_review(json.dumps(combined, ensure_ascii=False), rows, context.get('workbook'),
                context.get('name_comparison_rules', context['GZKJG']), context.get('near_name_candidates'),
                context.get('required_name_alignments'), gzk_rules=context['GZKJG'])
            dump(folder / 'combined-review.json', validated)
            return validated
        review = self._review_request(context, folder)
        return self.audit_negative_names(review, context, folder) if review['name_status'] == 'correct' else review

    def audit_negative_names(self, review, context, folder):
        from .name_alignment import negative_review_candidates
        from .name_review import validate_review
        focus = negative_review_candidates(review, context.get('near_name_candidates', []), context['GZKJG'])
        if not focus:
            return review
        audit = folder / 'negative-name-audit'
        audit.mkdir()
        prompt = ('这是名称否定判断的独立复核：只对给定near_name_candidates逐项重新核对源标题、父级和汇总/分项身份。'
                  '名称包含关系只用于召回，不能直接判同义或直接改名。尤其不能仅凭主表/子表分处、原始简称未出现在GZK、'
                  '或者存在其他分项，就断言不是同一指标。独立子表的总量行可能继承子表标题中的范围限定；'
                  '要从源表找出其究竟是该标题的总体还是明确的不同分项。相同数值仅辅助，不单独证明同义。'
                  '仍结合单位和真实频率排除不兼容，缺少证据填unknown，不猜测频率。'
                  '若原different_indicator判断有误且需对齐GZK，输出incorrect、真实issue和same_indicator；'
                  '否则给出具体口径区别，不能以字面未匹配代替依据。不要生成代码。')
        follow = {**context, 'near_name_candidates': focus}
        if context.get('_review_cache_dir'):
            follow['_review_cache_dir'] = str(Path(context['_review_cache_dir']) / 'negative-name-audit')
        result = self._review_request(follow, audit, prompt)
        key = lambda item: (item['indicator_name'], item['source']['sheet'], item['source']['cell'])
        merged = {key(r): r for r in review.get('candidate_reviews', [])}
        merged.update({key(r): r for r in result.get('candidate_reviews', [])})
        combined = {'name_status': result['name_status'],
                    'explanation': review['explanation'] + '\n名称否定复核：' + result['explanation'],
                    'issues': result['issues'], 'candidate_reviews': list(merged.values())}
        validated = validate_review(json.dumps(combined, ensure_ascii=False), context['parsed_data'], context.get('workbook'),
            context.get('name_comparison_rules', context['GZKJG']), context.get('near_name_candidates'),
            context.get('required_name_alignments'), gzk_rules=context['GZKJG'])
        dump(audit / 'combined-review.json', validated)
        return validated

    def _review_request(self, context, folder, extra_prompt=''):
        from .name_review import PROMPT as REVIEW_PROMPT, SCHEMA as REVIEW_SCHEMA, validate_review
        system = (REVIEW_PROMPT + '\n' + extra_prompt +
                  '\n去重引用说明：候选name_reference_lookup指向name_comparison_rules中同名项的完整name_reference；'
                  'workbook_row_numbers指向workbook同一sheet的一基行号，所有原单元格仍在完整workbook。'
                  '存在review_scope时只输出该批parsed_data记录的审核，其他批负责其余记录。'
                  'other_parsed_records是其他批次已经提取的真实名称与坐标，不是遗漏项；不要为这些记录输出issue或candidate_review。'
                  '每个实际名称和source坐标只返回一条candidate_review；该记录有多个相似候选时综合比较后给一条结论，不能按候选数量重复记录。'
                  '\n输出schema：' + json.dumps(REVIEW_SCHEMA, ensure_ascii=False))
        def validator(content):
            review = validate_review(content, context['parsed_data'], context.get('workbook'),
                context.get('name_comparison_rules', context.get('GZKJG')), context.get('near_name_candidates'),
                context.get('required_name_alignments'), gzk_rules=context.get('GZKJG'))
            if context.get('review_scope'):
                cells = {(r['source']['sheet'], r['source']['cell']) for r in context['parsed_data']}
                if any((r['source']['sheet'], r['source']['cell']) not in cells
                       for r in review['issues'] + review.get('candidate_reviews', [])):
                    raise ValueError('分批审核只能引用本批parsed_data坐标；其他批次记录不算遗漏，不得输出其issue或candidate_review')
            return review
        cache = Path(context['_review_cache_dir']) if context.get('_review_cache_dir') else None
        for origin in ([cache, cache / 'response-repair-1'] if cache else []):
            if not (origin / 'model-response-raw.json').exists():
                continue
            # 以前因程序校验窗口过窄拒绝的完整原始回复，也只在当前契约全部通过后复用。
            # 截断/缺结束证据的回复绝不恢复，且不修改模型文字或业务决策。
            if not (origin / 'model-response.json').exists():
                metadata = origin / 'model-metadata.json'
                if not metadata.exists() or json.loads(metadata.read_text(encoding='utf-8')).get('finish_reason') != 'stop':
                    continue
            raw = json.loads((origin / 'model-response-raw.json').read_text(encoding='utf-8'))['content']
            try:
                cached = validator(raw)
            except ValueError:
                continue
            for name in ('model-request.json', 'model-metadata.json', 'model-response-raw.json', 'response.schema.json'):
                if (origin / name).exists():
                    shutil.copyfile(origin / name, folder / name)
            dump(folder / 'model-response.json', cached)
            dump(folder / 'cache-provenance.json', {'origin': str(origin), 'raw_response_edited': False,
                                                   'validated_against_current_inputs': True})
            return cached
        try:
            return self._complete(system, review_context(context), folder, REVIEW_SCHEMA, validator)
        except ValueError as exc:
            if isinstance(exc, ModelOutputTruncated) or not (folder / "model-response-invalid.json").exists():
                raise
            # 只修正审核契约，不凭无效回复触发业务代码重建。最多补问一次。
            feedback = json.loads((folder / "model-response-invalid.json").read_text(encoding="utf-8"))
            repair = folder / "response-repair-1"
            repair.mkdir()
            try:
                return self._complete(system, review_context({**context, "review_protocol_feedback": feedback}), repair, REVIEW_SCHEMA, validator)
            except ValueError as second:
                if isinstance(second, (ModelOutputTruncated, ModelContextTooLarge)):
                    raise
                raise NameReviewProtocolError("DeepSeek审核响应补问后仍未通过契约校验，停止导出，未把该格式问题当作名称构造错误重建代码：" + str(second)) from second

    def check_source(self, context, folder, job):
        from .source_preflight import SYSTEM, validate_decision as validate_source
        timeout = job.get('preflight_timeout',120)
        if type(timeout) not in (int,float) or timeout <= 0:
            raise ValueError('preflight_timeout须为正数秒数')
        previous = self.timeout
        self.timeout = min(previous,timeout)
        try:
            return self._complete(SYSTEM,context,folder,{'type':'object'},
                                  lambda text:validate_source(text,context['source_text'],context['GZKJG']))
        finally:
            self.timeout = previous

    def _complete(self, system, context, folder, schema, validator):
        user = "本轮业务上下文与历史诊断：\n" + json.dumps(model_context(context), ensure_ascii=False, default=str)
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "stream": self.stream, "max_tokens": self.max_tokens, **self.extra_body}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise ModelContextTooLarge("DeepSeek请求去重后仍超过1.2MB，需缩小输入范围，不能静默截断")
        dump(folder / "response.schema.json", schema)
        # 此文件不含Authorization或API Key。环境变量值不加入业务提示词。
        dump(folder / "model-request.json", body)
        headers = self.headers()
        if self.stream:
            headers['Accept'] = 'text/event-stream'
        request = urllib.request.Request(self.url, data=encoded, headers=headers, method="POST")
        payload = self.request_sse(request) if self.stream else self.request_json(request)
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("DeepSeek接口响应不符合Chat Completions格式") from None
        dump(folder / "model-metadata.json", {"provider": "deepseek", "model": payload.get("model", self.model),
                                              "finish_reason": choice.get("finish_reason"), "usage": payload.get("usage")})
        if choice.get("finish_reason") == "length":
            raise ModelOutputTruncated("DeepSeek生成被token上限截断；已停止相同配置重试，请增加deepseek.max_tokens或调整deepseek.extra_body.reasoning_effort")
        dump(folder / "model-response-raw.json", {"content": content})
        try:
            decision = validator(content)
        except ValueError as exc:
            dump(folder / "model-response-invalid.json", {"content": content, "validation_error": str(exc)})
            raise
        dump(folder / "model-response.json", decision)
        return decision

    def headers(self):
        key = os.environ.get(self.api_key_env, "")
        if self.require_api_key and not key:
            raise BackendUnavailable(f"未配置DeepSeek认证环境变量 {self.api_key_env}；没有调用任何其他模型")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    def request_json(self, request):
        try:
            with self.transport(request, timeout=self.timeout) as response:
                raw = response.read(MAX_OUTPUT_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # 不保存上游原始报错体，避免网关回显认证信息。
            raise BackendUnavailable(f"DeepSeek接口返回HTTP {exc.code}；请检查接口、模型名、权限或额度") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BackendUnavailable("DeepSeek接口连接失败或超时；请检查地址、网络和服务状态") from None
        if len(raw) > MAX_OUTPUT_BYTES:
            raise ValueError("DeepSeek响应超过8MB限制")
        return json.loads(raw)

    def request_sse(self, request):
        """流式接收完整content；思考片段不作代码，收到结束信号后才允许校验/执行。"""
        started = time.monotonic()
        chunks, event = [], []
        state = {'model': self.model, 'finish': None, 'usage': {}}
        total = 0

        def consume():
            if not event:
                return False
            raw = b'\n'.join(event)
            event.clear()
            if raw.strip() == b'[DONE]':
                return True
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                raise BackendUnavailable('DeepSeek流式响应不是有效JSON事件，未执行不完整代码') from None
            if not isinstance(item, dict) or item.get('error'):
                raise BackendUnavailable('DeepSeek流式接口返回错误，未记录可能包含认证信息的原始错误体')
            state['model'] = item.get('model') or state['model']
            if isinstance(item.get('usage'), dict):
                state['usage'] = item['usage']
            for choice in item.get('choices', []):
                if choice.get('index', 0) != 0:
                    continue
                content = choice.get('delta', {}).get('content')
                if content is not None:
                    if not isinstance(content, str):
                        raise BackendUnavailable('DeepSeek流式content类型异常，未执行代码')
                    chunks.append(content)
                if choice.get('finish_reason'):
                    state['finish'] = choice['finish_reason']
            return False

        try:
            with self.transport(request, timeout=self.timeout) as response:
                while True:
                    if time.monotonic() - started >= self.timeout:
                        raise BackendUnavailable('DeepSeek流式调用超过model_timeout，未执行不完整代码')
                    line = response.readline(MAX_OUTPUT_BYTES - total + 1)
                    if not line:
                        consume()
                        break
                    total += len(line)
                    if total > MAX_OUTPUT_BYTES:
                        raise ModelOutputTruncated('DeepSeek流式响应超过8MB限制，未执行不完整代码')
                    if not line.strip():
                        if consume():
                            break
                    elif line.startswith(b'data:'):
                        event.append(line[5:].strip())
        except urllib.error.HTTPError as exc:
            raise BackendUnavailable(f'DeepSeek流式接口返回HTTP {exc.code}；请检查接口或网关状态') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BackendUnavailable('DeepSeek流式连接失败、中断或超时，未执行不完整代码') from None
        if not state['finish']:
            raise BackendUnavailable('DeepSeek流式响应缺少finish_reason，未执行不完整代码')
        return {'model': state['model'], 'usage': state['usage'], 'choices': [{
            'message': {'content': ''.join(chunks)}, 'finish_reason': state['finish']}]}

    def probe_model(self):
        url = self.url.removesuffix("/chat/completions") + "/models"
        payload = self.request_json(urllib.request.Request(url, headers=self.headers(), method="GET"))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("模型列表响应格式不正确")
        ids = [item["id"] for item in payload["data"] if isinstance(item, dict) and isinstance(item.get("id"), str)]
        return {"configured_model": self.model, "model_available": self.model in ids, "available_model_ids": ids}
