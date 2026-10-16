"""显式恢复审核协议故障，沿用真实生成记录及原重建次数，不自动重开预算。"""
import copy
import hashlib
import json
from pathlib import Path

from .name_alignment import build_review_candidates, required_alignments
from .name_review import validate_review
from .name_reference import comparison_rules


def restore_incomplete_review(previous, context, source_hash, folder):
    """只恢复已完整生成并执行、随后审核截断的轮次，重新审核而不重置生成预算。"""
    from .app import load, dump
    from .worker import check_code
    from .deepseek import validate_decision
    previous = Path(previous)
    old = load(previous / 'run-result.json')
    before = load(previous / 'input-context.json')
    serialized = json.loads(json.dumps(context, ensure_ascii=False, default=str))
    if old.get('failure_kind') != 'MODEL_OUTPUT_TRUNCATED':
        raise ValueError('审核续跑仅适用于已执行代码后审核响应截断的任务')
    count = old.get('attempts')
    if type(count) is not int or not 1 <= count <= 3:
        raise ValueError('原任务生成轮次无效')
    for key in ('skill', 'business_rules', 'GZKJG', 'source_name', 'pageTitle', 'fileMd5',
                'workbook', 'required_source_cells', 'publictime', 'upstream_metadata'):
        if before.get(key) != serialized.get(key):
            raise ValueError('恢复输入与原任务不一致：' + key)
    if old.get('source_identity', {}).get('sha256') != source_hash:
        raise ValueError('恢复源文件内容与原任务不一致')
    attempt = previous / f'attempt-{count}'
    metadata = load(attempt / 'name-review/model-metadata.json')
    if metadata.get('finish_reason') != 'length' or (attempt / 'name-review/name-review.json').exists():
        raise ValueError('没有可续跑的审核截断证据')
    history = copy.deepcopy([r for r in old['history'] if 'evaluation' in r and 'python_code' in r])
    if [r['attempt'] for r in history] != list(range(1, count + 1)):
        raise ValueError('原任务生成历史不连续')
    code = (attempt / 'parser.py').read_text(encoding='utf-8')
    response = validate_decision(load(attempt / 'model-response-raw.json')['content'], require_derivation=True)
    if code != history[-1]['python_code'] or response['python_code'] != code:
        raise ValueError('原解析源码与DeepSeek响应及历史不一致')
    if history[-1]['evaluation'].get('status') != 'pending_name_review':
        raise ValueError('原解析轮次未到待审核阶段')
    check_code(code)
    # 本轮会按正常执行路径重新执行、查数值、查覆盖范围，再调用真实DeepSeek审核。
    folder.mkdir()
    dump(folder / 'provenance.json', {'previous_run': str(previous), 'prior_attempts': count,
        'remaining_rebuilds': 3 - count, 'source_sha256': source_hash,
        'code_origin': str(attempt / 'parser.py'), 'generation_response_origin': str(attempt / 'model-response-raw.json'),
        'generation_metadata': load(attempt / 'model-metadata.json'),
        'semantic_response_edited': False, 'review_restarted': True})
    return history[:-1], count, response, old.get('elapsed_seconds', 0) + old.get('prior_elapsed_seconds', 0)


def restore_protocol_run(previous, context, source_hash, executor, params, folder, negative_audit=False):
    from .app import load, dump, audit_sources, audit_required_sources, match_evaluation
    from .worker import check_code
    previous = Path(previous)
    old = load(previous / 'run-result.json')
    before = load(previous / 'input-context.json')
    # Excel 的 iter_rows 返回 tuple，保存为 JSON 后为 list；按实际发送的 JSON 比较。
    serialized_context = json.loads(json.dumps(context, ensure_ascii=False, default=str))
    protocol_failure = old.get('failure_kind') == 'NAME_REVIEW_PROTOCOL_ERROR'
    negative_recheck = negative_audit and old.get('failure_kind') == 'NAME_RECHECK_FOUND_ERROR'
    if not protocol_failure and not negative_recheck:
        raise ValueError('恢复仅支持审核协议故障；不能借恢复绕过名称修复上限')
    count = old.get('attempts')
    if type(count) is not int or not 1 <= count < 3:
        raise ValueError('原任务没有剩余重建次数，交人工处理')
    for key in ('skill', 'business_rules', 'GZKJG', 'source_name', 'pageTitle', 'fileMd5', 'workbook', 'required_source_cells'):
        if before.get(key) != serialized_context.get(key):
            raise ValueError('恢复输入与原任务不一致：' + key)
    for key, default in (('publictime', ''), ('upstream_metadata', {})):
        if before.get(key, default) != serialized_context.get(key, default):
            raise ValueError('恢复输入与原任务不一致：' + key)
    if old.get('source_identity', {}).get('sha256') != source_hash:
        raise ValueError('恢复源文件内容与原任务不一致')
    history = copy.deepcopy(old.get('history', []))
    if not history or history[-1].get('attempt') != count or history[-1].get('python_code') is None:
        raise ValueError('原生成历史不完整，不能恢复')
    attempt = previous / f'attempt-{count}'
    code = (attempt / 'parser.py').read_text(encoding='utf-8')
    if code != history[-1]['python_code']:
        raise ValueError('原解析代码与已保存生成历史不一致')
    check_code(code)
    folder.mkdir()
    replay_code = folder / 'parser.py'
    replay_code.write_text(code, encoding='utf-8')
    rows = executor.execute(replay_code, params, folder)
    audit_sources(rows, context['workbook'])
    audit_required_sources(rows, context['required_source_cells'])
    if rows != load(attempt / 'ExcelData.json')['ExcelData']:
        raise ValueError('原解析代码重跑与保存结果不一致')
    reference = comparison_rules(context['business_rules'], context['GZKJG'])
    candidates = build_review_candidates(rows, reference, context['GZKJG'], context['workbook'])['review_candidates']
    if json.loads(json.dumps(candidates, ensure_ascii=False, default=str)) != load(attempt / 'name-precheck.json')['review_candidates']:
        raise ValueError('恢复时名称预判依据与原任务不一致')
    review_origin = (previous / 'semantic-recheck/negative-name-audit/combined-review.json' if negative_recheck
                     else attempt / 'name-review/model-response-invalid.json')
    raw = json.dumps(load(review_origin), ensure_ascii=False) if negative_recheck else load(review_origin)['content']
    review = validate_review(raw, rows, context['workbook'], context['name_comparison_rules'], candidates,
                             required_alignments(history[:-1], context['GZKJG']), gzk_rules=context['GZKJG'])
    if review['name_status'] != 'incorrect':
        raise ValueError('恢复只续接已验证的DeepSeek修复建议，不额外重建已正确代码')
    evaluation = match_evaluation(rows, context['GZKJG'], {'publictime': params.get('publictime', '')})
    evaluation.update(name_review=review, near_name_candidates=candidates, status='needs_review',
                      failure_kind='NAME_CONSTRUCTION_ERROR', retryable=True)
    history[-1]['evaluation'] = evaluation
    dump(folder / 'recovered-review.json', review)
    dump(folder / 'provenance.json', {'previous_run': str(previous), 'prior_attempts': count,
        'remaining_rebuilds': 3 - count, 'source_sha256': source_hash,
        'original_review_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
        'review_origin': str(review_origin),
        'code_origin': str(attempt / 'parser.py'), 'code_reexecuted': True,
        'semantic_response_edited': False})
    return history, count, old.get('elapsed_seconds', 0) + old.get('prior_elapsed_seconds', 0)
