"""显式用DeepSeek复核已保存的名称否定判断，不修改既有节点输出。"""
import json
from pathlib import Path
import sys

from .app import load, dump
from .deepseek import DeepSeekBackend
from .name_alignment import required_alignments


def invalidate_after_audit(folder, result):
    if result['name_status'] != 'incorrect':
        return
    saved = load(folder / 'run-result.json')
    out = folder / 'semantic-recheck'
    for name in ('node-output.json', 'node-output.txt'):
        path = folder / name
        if path.exists():
            path.replace(out / ('before-audit-' + name))
    saved.update(status='needs_review', failure_kind='NAME_RECHECK_FOUND_ERROR', requires_human_review=True,
                 negative_name_audit=result)
    saved.setdefault('notifications', []).append({'code': 'NAME_RECHECK_FOUND_ERROR', 'severity': 'error',
        'message': 'DeepSeek补充复核发现名称问题，原成功输出已撤回保存为审计记录；续接必须沿用原重建上限。'})
    dump(folder / 'run-result.json', saved)


def audit(folder):
    saved = load(folder / 'run-result.json')
    if saved['status'] != 'candidate_pass':
        raise ValueError('仅支持已完成运行的补充名称复核')
    context = load(folder / 'input-context.json')
    attempt = folder / f"attempt-{saved['attempts']}"
    context.update(parsed_data=load(attempt / 'ExcelData.json')['ExcelData'],
                   parser_code=(attempt / 'parser.py').read_text(encoding='utf-8'),
                   matching=saved['last_evaluation'], history=saved['history'],
                   required_name_alignments=required_alignments(saved['history'], context['GZKJG']),
                   near_name_candidates=load(attempt / 'name-precheck.json')['review_candidates'])
    backend = DeepSeekBackend.from_job(load(folder / 'job-request.json')['job'])
    out = folder / 'semantic-recheck'
    out.mkdir()
    result = backend.audit_negative_names(saved['name_review'], context, out)
    dump(out / 'final-review.json', result)
    invalidate_after_audit(folder, result)
    print(json.dumps({'name_status': result['name_status'], 'issues': result['issues']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    audit(Path(sys.argv[1]).resolve())
