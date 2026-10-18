"""HTML Python / Markdown Prompt 执行分支，共用精确匹配与证据审核闭环。"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

from .app import (ROOT, BackendUnavailable, ModelOutputTruncated, NameReviewProtocolError,
                  ModelContextTooLarge, TotalDeadlineExceeded, LocalPythonExecutor,
                  DockerExecutor, dump, load, normalize_rules, evaluate, skill_context)
from .markdown_inputs import (normalize_task, rule_row, regex_rules, acquire_markdown,
                              page_evidence, exported_value)
from .markdown_protocol import (HTML_GENERATE, PROMPT_GENERATE, REVIEW_PROMPT, json_object,
                                validate_asset, validate_extraction, validate_rows,
                                validate_review, coverage_names, regex_evidence, match_rows, node_output)
from .source_download import SourceDownloadError
from .platform_export import PlatformExportError
from .html_platform_export import export_html_platform_parser


def strategy_for(job):
    value = job.get("parse_strategy")
    if value is not None:
        if value not in {"excel", "html", "prompt"}:
            raise ValueError("parse_strategy须为excel/html/prompt")
        return value
    filetype = str(job.get("fileType") or job.get("upstream_metadata", {}).get("fileType", "")).lower()
    if filetype == "html":
        return "html"
    if filetype in {"pdf", "md", "txt"}:
        return "prompt"
    return "excel"


def request(backend, system, context, folder, validator):
    folder.mkdir(parents=True, exist_ok=True)
    # 相同客户端/认证/限时和原始响应日志；业务内容不经过Codex生成。
    return backend._complete(system, context, folder, {"type": "object"}, validator)


def task_rules(job, resolve):
    if job.get("gzk_file"):
        path = resolve(job["gzk_file"])
        try:
            raw = exported_value(path, "codeOutputResult")
        except ValueError as exc:
            if "缺少字段" not in str(exc):
                raise
            raw = exported_value(path, "GZKJG")
    elif "GZKJG" in job:
        raw = job["GZKJG"]
    else:
        gzk = job.get("GZK", {})
        if "GZKJG" in gzk:
            raw = gzk["GZKJG"]
        else:
            mapping = {"ZBDM":"rule_indicator_code", "ZBMC":"rule_indicator_name", "ZBDW":"rule_constant_name",
                       "unit_ratio":"rule_unit_ratio", "indicator_release_frequency":"indicator_release_frequency"}
            if any(not isinstance(gzk.get(key, []), list) for key in mapping):
                raise ValueError("GZK原始字段须为数组")
            count = max((len(gzk.get(key, [])) for key in mapping), default=0)
            raw = [{field: gzk.get(key, [])[i] if i < len(gzk.get(key, [])) else "" for key, field in mapping.items()} for i in range(count)]
    return normalize_rules(raw)


def restore_generated_asset(origin, context, strategy):
    """校验程序修复后的恢复：复用完整原始模型响应，保留已开始的全部生成预算。"""
    prior = load(origin / "input-context.json")
    for key in ("skill", "rule", "Markdown", "title", "publictime", "GZKJG", "regex", "verification_scope"):
        if prior.get(key) != context.get(key):
            raise ValueError("恢复任务的输入发生变化，不能复用资产: " + key)
    started = sorted((p for p in origin.glob("attempt-*") if (p / "generation/model-request.json").exists()),
                     key=lambda p: int(p.name.split("-")[-1]))
    if not 1 <= len(started) <= 3:
        raise ValueError("恢复来源的生成预算无效")
    for folder in reversed(started):
        generation = folder / "generation"
        response_folder = generation / "protocol-repair" if (generation / "protocol-repair").exists() else generation
        if not (response_folder / "model-response-raw.json").is_file() or not (response_folder / "model-metadata.json").is_file():
            continue
        metadata = load(response_folder / "model-metadata.json")
        if metadata.get("provider") != "deepseek" or metadata.get("finish_reason") != "stop":
            continue
        asset = validate_asset(load(response_folder / "model-response-raw.json")["content"], strategy)
        filename, field = ("parser.py", "python_code") if strategy == "html" else ("prompt.md", "prompt_code")
        if asset["decision"] != "generate" or (folder / filename).read_text(encoding="utf-8") != asset[field]:
            raise ValueError("已保存资产与完整DeepSeek原始响应不一致")
        saved = load(origin / "run-result.json") if (origin / "run-result.json").exists() else {}
        used = max(saved.get("attempts", 0), saved.get("prior_generation_calls", 0),
                   *(int(p.name.split("-")[-1]) for p in started))
        return asset, generation, used
    raise ValueError("没有可恢复的完整DeepSeek资产，不读取截断或未完成响应")


def continue_generation_context(origin, context, strategy):
    """策略修正后的新一轮生成；源数据不变，继承已消耗次数和审核记录。"""
    prior = load(origin / "input-context.json")
    for key in ("rule", "Markdown", "title", "publictime", "GZKJG", "regex", "verification_scope"):
        if prior.get(key) != context.get(key):
            raise ValueError("续跑任务的业务输入发生变化: " + key)
    saved = load(origin / "run-result.json")
    calls = sorted((p for p in origin.glob("attempt-*") if (p / "generation/model-request.json").exists()),
                   key=lambda p: int(p.name.split("-")[-1]))
    used = max([saved.get("attempts", 0), saved.get("prior_generation_calls", 0)] +
               [int(p.name.split("-")[-1]) for p in calls])
    if not calls or not 1 <= used <= 3:
        raise ValueError("续跑来源的生成预算无效")
    context["history"].extend(saved.get("history", []))
    for folder in reversed(calls):
        generation = folder / "generation"
        response_folder = generation / "protocol-repair" if (generation / "protocol-repair").exists() else generation
        response = response_folder / "model-response-raw.json"
        metadata = response_folder / "model-metadata.json"
        if not response.is_file() or not metadata.is_file():
            continue
        info = load(metadata)
        if info.get("provider") != "deepseek" or info.get("finish_reason") != "stop":
            continue
        asset = validate_asset(load(response)["content"], strategy)
        context["previous_asset"] = asset["python_code" if strategy == "html" else "prompt_code"]
        context["previous_generation_explanation"] = asset["explanation"]
        break
    context["continuation_policy"] = (
        "前次因明确不同口径误判为需人工而停止。现按修正策略自主排除不同指标继续生成；"
        "保留源文事实，不强制匹配GZK，不重置生成预算。")
    return used


def checked_review(backend, context, folder, rows, markdown, names):
    validator = lambda content: validate_review(content, rows, markdown, names)
    try:
        return request(backend, REVIEW_PROMPT, context, folder, validator)
    except ValueError as exc:
        if isinstance(exc, (ModelOutputTruncated, ModelContextTooLarge)):
            raise
        feedback = {"validation_error": str(exc)}
        invalid = folder / "model-response-invalid.json"
        if invalid.exists():
            feedback = load(invalid)
        # 一次审核协议补问不增加业务资产重建次数。
        try:
            return request(backend, REVIEW_PROMPT,
                           {**context, "review_protocol_feedback": feedback},
                           folder / "protocol-repair", validator)
        except ValueError as second:
            raise NameReviewProtocolError("审核协议补问仍失败，未据无效判断重建资产: " + str(second)) from second


def checked_generation(backend, system, context, folder, strategy, saved_content=None):
    """生成格式补问最多一次；不猜选重复字段，不把格式错误当业务重建。"""
    validator = lambda content: validate_asset(content, strategy)
    try:
        if saved_content is not None:
            return validator(saved_content)
        return request(backend, system, context, folder, validator)
    except ValueError as exc:
        if isinstance(exc, (ModelOutputTruncated, ModelContextTooLarge)):
            raise
        raw_path = folder / "model-response-raw.json"
        if saved_content is None and not raw_path.is_file():
            raise
        content = saved_content if saved_content is not None else load(raw_path)["content"]
        feedback = {"validation_error": str(exc), "invalid_response": content,
                    "instruction": "仅修复JSON协议；每个字段只出现一次，返回完整资产。不得因格式错误改变业务口径、放宽匹配或省略代码。若重复决策有冲突，请依据完整源证据重新明确唯一决策，不按首值或末值覆盖。"}
        dump(folder / "protocol-repair-request.json", feedback)
        try:
            return request(backend, system + "\n这是唯一一次生成协议补问。严格输出单一JSON对象，每个字段仅出现一次，无Markdown代码围栏。",
                           {**context, "generation_protocol_feedback": feedback},
                           folder / "protocol-repair", validator)
        except ValueError as second:
            if isinstance(second, (ModelOutputTruncated, ModelContextTooLarge)):
                raise
            raise NameReviewProtocolError("生成协议补问仍失败，未执行无效代码，未消耗业务重建次数: " + str(second)) from second


def restore_generation_for_repair(origin, context):
    prior = load(origin / "input-context.json")
    for key in ("skill", "rule", "Markdown", "title", "publictime", "GZKJG", "regex", "verification_scope"):
        if prior.get(key) != context.get(key):
            raise ValueError("格式补问恢复的输入发生变化: " + key)
    folders = sorted((p for p in origin.glob("attempt-*") if (p / "generation/model-request.json").is_file()),
                     key=lambda p: int(p.name.split("-")[-1]))
    if not folders:
        raise ValueError("没有可恢复的生成请求")
    folder = folders[-1] / "generation"
    info = load(folder / "model-metadata.json")
    if (info.get("provider") != "deepseek" or info.get("finish_reason") != "stop"
            or not (folder / "model-response-invalid.json").is_file()
            or (folder / "protocol-repair").exists()):
        raise ValueError("只恢复完整、格式无效且尚未补问的DeepSeek响应")
    saved = load(origin / "run-result.json")
    used = max(saved.get("prior_generation_calls", 0), saved.get("attempts", 0),
               *(int(p.name.split("-")[-1]) for p in folders))
    if not 1 <= used <= 3:
        raise ValueError("格式补问恢复的生成预算无效")
    context["history"].extend(saved.get("history", []))
    return folder, used


def run_markdown_job(job, job_base, backend=None, executor=None, output_root=None,
                     execution_backend=None, _run_dir=None, _started=None, _deadline=None):
    started = _started if _started is not None else time.monotonic()
    base = Path(job_base)
    resolve = lambda path: (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    run = Path(_run_dir) if _run_dir else Path(output_root or resolve(job.get("output_dir", "runs"))) / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True, exist_ok=True)
    strategy = strategy_for(job)
    result = {"run_id": run.name, "output_dir": str(run), "status": "needs_review", "production_ready": False,
              "parse_strategy": strategy, "attempts": 0, "max_rebuilds": 2, "history": [], "notifications": [],
              "stage_timings": [], "minimum_matched_indicators": 2, "decision_basis": "source_evidence_and_name_correctness"}
    budget = job.get("total_timeout")
    deadline = _deadline if _deadline is not None else started + budget if budget else float("inf")

    def timed(stage, callback):
        if time.monotonic() >= deadline:
            raise TotalDeadlineExceeded("整体预算耗尽")
        print(json.dumps({"run_id": run.name, "stage": stage}, ensure_ascii=False), file=sys.stderr, flush=True)
        began = time.monotonic()
        if backend is not None and hasattr(backend, "timeout"):
            backend.timeout = min(job.get("model_timeout", 900), max(0.01, deadline - began))
        try:
            value = callback()
            if time.monotonic() >= deadline:
                raise TotalDeadlineExceeded("整体预算耗尽")
            return value
        finally:
            result["stage_timings"].append({"stage": stage, "seconds": round(time.monotonic() - began, 3)})

    def finish(kind="", message=""):
        result.update(failure_kind=kind, requires_human_review=bool(kind),
                      rebuilds=max(0, result["attempts"] - 1), elapsed_seconds=round(time.monotonic() - started, 3))
        if kind:
            result["status"] = ("markdown_verified_pending_webpage" if kind == "WEBPAGE_VERIFICATION_PENDING"
                                else "needs_review")
            result["production_ready"] = False
            result["notifications"].append({"code": kind, "message": message, "severity": "error"})
            result["human_review_file"] = str(run / "human-review.json")
            dump(run / "human-review.json", {"message": message, **result})
            for name in ("node-output.json", "node-output.txt"):
                (run / name).unlink(missing_ok=True)
        dump(run / "run-result.json", result)
        return result

    try:
        job = normalize_task(job, resolve)
        regression_only = job.get("markdown_only_regression", False)
        if not isinstance(regression_only, bool):
            raise ValueError("markdown_only_regression须为布尔值")
        if regression_only and (strategy != "html" or not isinstance(job.get("webpage_defer_authorization"), str)
                                or not job["webpage_defer_authorization"].strip()):
            raise ValueError("仅Markdown回归须为HTML任务，且记录用户暂缓网页核验的明确授权")
        result["webpage_verification_deferred"] = regression_only
        html_export = job.get('export_platform_parser', True) if strategy == 'html' else False
        if not isinstance(html_export, bool):
            raise ValueError('export_platform_parser须为布尔值')
        result['html_platform_export_enabled'] = html_export
        rules = task_rules(job, resolve)
        preflight = evaluate([], rules)
        if preflight["failure_kind"] in {"GZKJG_EMPTY", "GZKJG_INVALID", "RULE_AMBIGUITY"}:
            return finish(preflight["failure_kind"], "GZK为空、无效或名称存在歧义，停止生成")
        attempts = job.get("max_attempts", 3)
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise ValueError("首次生成加最多两次重建，max_attempts须为1..3")
        row = rule_row(job, resolve)
        for key in ("备注", "是否通过初步测试"):
            if any(term in str(row.get(key, "")) for term in ("无正则表", "无正则拼接", "MD5错误且文件错误", "文件错误且MD5错误", "文件错误且md5错误")):
                return finish("RULE_BLOCKED", "规则行明确标记无法生成: " + str(row[key]))
        regex_needed = strategy == "html" or "登记正则" in str(row.get("指标名称取值逻辑", ""))
        regex = regex_rules(job, row, resolve) if regex_needed else {"rules": [], "format": "not_needed"}
        dump(run / "business-rules.json", {"rule": row, "regex": regex})
        markdown, acquired = timed("acquire_markdown", lambda: acquire_markdown(job, resolve, run))
        result["source_acquisition"] = acquired
        if regression_only:
            page = {"status": "pending_verification", "url": row.get("source_url", ""),
                    "authorization": job["webpage_defer_authorization"],
                    "note": "用户授权只根据本次真实Markdown回归；尚无原网页内容证据"}
        else:
            page = timed("verify_original_webpage", lambda: page_evidence(job, row, resolve)) if strategy == "html" else None
        if page:
            dump(run / "webpage-evidence.json", page)
        skill_name = "html-rule-parser-codegen" if strategy == "html" else "statistical-markdown-prompt-codegen"
        skill = resolve(job["skill_path"]) if job.get("skill_path") else ROOT / "skills" / skill_name / "SKILL.md"
        # 路径属于本机审计信息，不进入业务提示词；Skill本身说明保留。
        regex_for_model = {"format": regex["format"], "sheet": regex.get("sheet", ""), "rules": regex["rules"]}
        context = {"skill": [item["content"] for item in skill_context(skill)], "rule": row,
                   "Markdown": markdown, "title": job.get("title", ""), "publictime": job.get("publictime", ""),
                   "GZK": {**job.get("GZK", {}), "GZKJG": rules}, "GZKJG": rules,
                   "regex": regex_for_model, "original_webpage": page,
                   "history": result["history"], "coverage_names": coverage_names(rules, regex)}
        if regression_only:
            context["verification_scope"] = (
                "用户明确授权本次只用真实Markdown生成并回归HTML解析器，原网页访问失败留待核验，不作为正式通过。"
                "此授权覆盖Skill的网页先核验顺序；不要仅因尚无网页而拒绝生成或判uncertain。"
                "规则文件名称属于历史示例，不用其示例年份覆盖本次Markdown自身统计期。"
                "仍须严格核验Markdown中的名称、数值、期间、正则和遗漏；真实业务口径歧义仍应报告。"
                "不得声明网页已核验，运行时不得回退原HTML或其他MD5。")
        dump(run / "input-context.json", context)
        if backend is None:
            from .deepseek import DeepSeekBackend
            backend = DeepSeekBackend.from_job(job)
        result["model_provider"] = "deepseek" if backend.__class__.__name__ == "DeepSeekBackend" else "injected_test_backend"
        result["model"] = getattr(backend, "model", "test")
        if strategy == "html" and executor is None:
            mode = execution_backend or job.get("execution_backend", "local_python")
            if mode == "docker":
                from .app import locate_docker
                executor = DockerExecutor(locate_docker(), job.get("parser_timeout", 30))
            elif mode == "local_python":
                executor = LocalPythonExecutor(job.get("parser_timeout", 30))
            else:
                raise ValueError("不支持的执行后端")
        result["execution_backend"] = "deepseek_prompt" if strategy == "prompt" else "docker" if isinstance(executor, DockerExecutor) else "local_python"
        result["os_sandbox"] = isinstance(executor, DockerExecutor)
        replay, replay_source, first_attempt, repair_source = None, None, 1, None
        if sum(bool(job.get(key)) for key in ("continue_from_run", "replay_generated_from_run", "repair_generation_from_run")) > 1:
            raise ValueError("新生成续跑、原资产复跑、生成格式恢复只能指定一项")
        if job.get("repair_generation_from_run"):
            origin = resolve(job["repair_generation_from_run"])
            repair_source, first_attempt = restore_generation_for_repair(origin, context)
            result.update(resumed_from_run=str(origin), prior_generation_calls=first_attempt,
                          recovery_note="完整DeepSeek响应因格式无效而恢复；仅补问一次，不重置生成预算")
            if first_attempt > attempts:
                raise ValueError("已使用生成次数超过本任务预算")
            dump(run / "input-context.json", context)
        if job.get("continue_from_run"):
            origin = resolve(job["continue_from_run"])
            used = continue_generation_context(origin, context, strategy)
            result.update(resumed_from_run=str(origin), prior_generation_calls=used, attempts=used,
                          recovery_note="源数据一致；策略修正后调用DeepSeek续建，继承全部已用生成次数")
            first_attempt = used + 1
            dump(run / "input-context.json", context)
            if first_attempt > attempts:
                return finish("MAX_REBUILDS_EXHAUSTED", "原任务已用完首次生成加两次重建预算，交人工处理")
        if job.get("replay_generated_from_run"):
            origin = resolve(job["replay_generated_from_run"])
            replay, replay_source, first_attempt = restore_generated_asset(origin, context, strategy)
            result.update(resumed_from_run=str(origin), prior_generation_calls=first_attempt,
                          recovery_note="仅修正Agent校验器，复用完整原始DeepSeek资产；中止的生成请求也计入原有预算")
            if first_attempt > attempts:
                raise ValueError("已使用生成次数超过本任务预算")
        for attempt in range(first_attempt, attempts + 1):
            result["attempts"] = attempt
            folder = run / ("attempt-" + str(attempt))
            folder.mkdir()
            if replay is not None:
                asset = replay
                replay = None
                generation = folder / "generation"
                shutil.copytree(replay_source, generation)
                dump(generation / "recovery-provenance.json", {"source": str(replay_source),
                     "raw_response_edited": False, "input_context_validated": True, "new_generation_call": False})
            else:
                saved_content = None
                if repair_source is not None:
                    shutil.copytree(repair_source, folder / "generation")
                    saved_content = load(repair_source / "model-response-raw.json")["content"]
                    dump(folder / "generation/recovery-provenance.json", {
                        "source": str(repair_source), "raw_response_edited": False,
                        "input_context_validated": True, "new_business_generation_call": False})
                    repair_source = None
                asset = timed("generate_" + strategy, lambda: checked_generation(
                    backend, HTML_GENERATE if strategy == "html" else PROMPT_GENERATE,
                    context, folder / "generation", strategy, saved_content))
            if asset["decision"] == "needs_review":
                return finish("BUSINESS_AMBIGUITY", asset["explanation"])
            field = "python_code" if strategy == "html" else "prompt_code"
            code = asset[field]
            asset_path = folder / ("parser.py" if strategy == "html" else "prompt.md")
            asset_path.write_text(code, encoding="utf-8")
            record = {"attempt": attempt, "asset": str(asset_path), "asset_sha256": hashlib.sha256(code.encode()).hexdigest()}
            try:
                if strategy == "html":
                    from .worker import check_code
                    check_code(code)
                    if re.search(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]|\b[0-9a-fA-F]{32}\b", code):
                        raise ValueError("HTML资产不能嵌入本机路径或样例MD5")
                    rows = timed("execute_python", lambda: executor.execute(asset_path,
                        {"Markdown": markdown, "title": job.get("title", ""), "publictime": job.get("publictime", "")}, folder))
                else:
                    extracted = timed("execute_prompt", lambda: request(backend, code,
                        {"Markdown": markdown, "title": job.get("title", ""),
                         "GZK": context["GZK"], "publictime": job.get("publictime", "")},
                        folder / "extraction", validate_extraction))
                    rows = extracted["out_data"]
                validate_rows(rows, strategy)
                dump(folder / "parsed-data.json", rows)
                evaluation = match_rows(rows, rules, job)
                dump(folder / "matching.json", evaluation)
                result["last_evaluation"] = evaluation
                review_context = {key: value for key, value in context.items() if key not in {"skill", "history"}}
                review_context.update(parsed_data=rows, regex_matches=regex_evidence(regex, markdown))
                review = timed("review_names_and_evidence", lambda: checked_review(
                    backend, review_context, folder / "review", rows, markdown, context["coverage_names"]))
                record.update(review=review, matched_indicator_count=evaluation["metrics"]["matched_indicator_count"], parsed_count=len(rows))
                result["history"].append(record)
                dump(folder / "review.json", review)
                if review["status"] in {"uncertain", "upstream_blocked"}:
                    return finish("BUSINESS_AMBIGUITY" if review["status"] == "uncertain" else "UPSTREAM_MARKDOWN_BLOCKED", review["explanation"])
                if (review["status"] == "correct" and record["matched_indicator_count"] >= 2
                        and not evaluation.get("errors") and not evaluation.get("rule_conflicts")
                        and evaluation.get("failure_kind") in {None, "NAME_MISMATCH"}):
                    output = node_output(evaluation, rules, strategy)
                    if html_export:
                        delivery = timed('html_platform_export', lambda: export_html_platform_parser(
                            asset_path, rows, {'markdownMD5': job['markdownMD5'], 'title': job.get('title', ''),
                                              'publictime': job.get('publictime', '')},
                            run, job, rules, timeout=min(job.get('platform_test_timeout', 65), max(.01, deadline-time.monotonic()))))
                        result.update(delivery)
                        result['delivery_mode'] = 'platform_markdownmd5_parser'
                    elif strategy == 'html':
                        result['delivery_mode'] = 'internal_core_only'
                    if regression_only:
                        dump(run / "regression-node-output.json", output)
                        (run / "regression-node-output.txt").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
                        result.update(regression_output=output, matched_indicator_count=record["matched_indicator_count"],
                                      parsed_count=len(rows), verified_asset=str(asset_path), name_review=review,
                                      verification_level="Markdown回归通过，原网页待核验", markdown_regression_passed=True,
                                      regression_output_file=str(run / "regression-node-output.json"))
                        return finish("WEBPAGE_VERIFICATION_PENDING", "Markdown生成、执行、名称匹配与证据审核通过；按用户授权保留原网页待核验，不输出正式成功节点值")
                    dump(run / "node-output.json", output)
                    (run / "node-output.txt").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
                    result.update(output, status="candidate_pass", production_ready=True, output_keys=list(output),
                                  matched_indicator_count=record["matched_indicator_count"], parsed_count=len(rows),
                                  verified_asset=str(asset_path), name_review=review,
                                  verification_level="网页与Markdown均验证" if strategy == "html" else "Markdown模型提取与证据审核均验证")
                    return finish()
                record["rebuild_reason"] = "审核发现构造/遗漏问题" if review["status"] != "correct" else "有效匹配不足两个不同指标"
            except (ModelOutputTruncated, ModelContextTooLarge, NameReviewProtocolError, BackendUnavailable, TotalDeadlineExceeded):
                raise
            except PlatformExportError as exc:
                return finish('PLATFORM_EXPORT_FAILED', str(exc))
            except (ValueError, RuntimeError, TimeoutError) as exc:
                record["execution_error"] = str(exc)
                result["history"].append(record)
            context["previous_asset"] = code
            dump(folder / "attempt-result.json", record)
        return finish("MAX_REBUILDS_EXHAUSTED", "首次生成加两次重建仍未通过，交人工处理")
    except BackendUnavailable as exc:
        return finish("MODEL_BACKEND_UNAVAILABLE", str(exc))
    except SourceDownloadError as exc:
        return finish(exc.code, str(exc))
    except TotalDeadlineExceeded as exc:
        return finish("TOTAL_TIMEOUT", str(exc))
    except (ModelOutputTruncated, ModelContextTooLarge, NameReviewProtocolError) as exc:
        return finish("MODEL_PROTOCOL_FAILED", str(exc))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return finish("INVALID_INPUT", str(exc))
