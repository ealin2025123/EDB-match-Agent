"""用户指定的已确认资产库：命中后只执行与匹配，不生成、不重建。"""
import hashlib
import json
from pathlib import Path
import re
import time
import uuid


def locate(root, page, strategy):
    root = Path(root).resolve()
    name = re.sub(r'[\\/]+', '_', str(page).strip())
    if not name or any(c in name for c in '<>:"|?*\x00'):
        return None
    suffix = '.md' if strategy == 'prompt' else '.py'
    folder = root / strategy
    if folder.exists() and not folder.resolve().is_relative_to(root):
        raise ValueError('已有资产分类目录超出指定根目录')
    paths = sorted(p.resolve() for p in folder.rglob('*' + suffix)
                   if p.name == name + suffix and p.is_file() and p.resolve().is_relative_to(root)) if folder.is_dir() else []
    # 历史资产的存放分类与上游fileType可能不同；Python入口契约相同。
    # 分类未命中时，在用户指定的整个根目录查找，而不是进入规则生成。
    if not paths:
        paths = sorted(p.resolve() for p in root.rglob('*' + suffix)
                       if p.name == name + suffix and p.is_file() and p.resolve().is_relative_to(root))
    if not paths:
        return None
    contents = {hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    if len(contents) != 1:
        raise ValueError('同一路径存在内容不同的已生成资产，请明确保留一个版本')
    return paths[0]


def matching_output(rows, rules, job, strategy):
    from .markdown_protocol import match_rows, node_output
    from .output_format import workflow_output
    from .output_checks import check_extraction
    check_extraction(rows)
    evaluation = match_rows(rows, rules, job)
    if evaluation.get('errors') or evaluation.get('rule_conflicts'):
        raise ValueError('已有代码的输出与当前规则不兼容，已停止；未调用生成或重建')
    if evaluation['metrics']['matched_indicator_count'] < 2:
        raise ValueError('已有代码有效匹配不足两个不同编码，已停止；未调用生成或重建')
    output = workflow_output(evaluation) if strategy == 'excel' else node_output(evaluation, rules, strategy)
    return output


def run_if_present(job, job_base, backend=None, runner=None, output_root=None):
    from .app import ROOT, dump, normalize_rules
    from .markdown_agent import strategy_for
    root = job.get('existing_parser_root', ROOT/'EDBPythonCode-master (3)'/'EDBPythonCode-master')
    if job.get('prefer_existing_parsers', True) is False:
        return None
    strategy = strategy_for(job)
    began = time.monotonic()
    try:
        asset = locate(root, job.get('pageTitle', ''), strategy)
    except ValueError:
        return {'status':'needs_review','parse_strategy':strategy,'failure_kind':'LOCAL_ASSET_AMBIGUOUS',
                'failure_stage':'existing_asset_lookup','execution_mode':'trusted_library',
                'notifications':[{'code':'LOCAL_ASSET_AMBIGUOUS','message':'同一路径存在不同内容的资产，请明确版本；未调用生成。'}]}
    if asset is None:
        return None
    resolve = lambda v: (Path(job_base)/v).resolve()
    folder = Path(output_root or resolve(job.get('output_dir', 'runs')))/('existing-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    result = {'status':'needs_review','production_ready':False,'parse_strategy':strategy,
              'execution_mode':'trusted_library','output_dir':str(folder),'attempts':1,'rebuilds':0,
              'cache_hit':True,'cache_kind':'trusted_library','generation_calls':0,'notifications':[]}
    stage = 'input_validation'
    try:
        raw_rules = job.get('GZKJG') or (job.get('GZK') or {}).get('GZKJG')
        if not raw_rules and job.get('gzk_file'):
            from .markdown_agent import task_rules
            rules = task_rules(job, resolve)
        else:
            rules = normalize_rules(raw_rules)
        if not rules or any(not str(r.get('rule_indicator_name','')).strip() or not str(r.get('rule_indicator_code','')).strip() for r in rules):
            raise ValueError('指标规则为空或无效')
        # 保存逐字节快照；原始资产保持不变，任务中交付的即实际执行版本。
        copied = folder/asset.name
        copied.write_bytes(asset.read_bytes())
        digest = hashlib.sha256(copied.read_bytes()).hexdigest()
        params = {k:job.get(k,'') for k in ('fileMd5','markdownMD5','title','publictime','pageTitle','fileType')}
        params.update(GZKJG=rules,GZK={'GZKJG':rules},filemd5=params['fileMd5'],markdownMd5=params['markdownMD5'])
        stage = 'execute_existing_asset'
        if strategy == 'prompt':
            from .markdown_inputs import acquire_markdown
            from .markdown_agent import request
            from .markdown_protocol import validate_extraction
            from .deepseek import DeepSeekBackend
            markdown, acquisition = acquire_markdown(job, resolve, folder)
            dump(folder/'source-acquisition.json', acquisition)
            extracted = request(backend or DeepSeekBackend.from_job(job), copied.read_text(encoding='utf-8-sig'),
                                {**params,'Markdown':markdown,'GZK':{'GZKJG':rules}},
                                folder/'extraction', validate_extraction)
            rows = extracted['out_data']
        else:
            from .platform_export import run_platform
            extracted = (runner or run_platform)(copied, params, folder, 'existing-parser', job.get('platform_test_timeout',65))
            rows = extracted['ExcelData']
        stage = 'matching_existing_asset'
        output = matching_output(rows, rules, job, strategy)
        dump(folder/'existing-evidence.json', {'asset_sha256':digest,'source_asset':str(asset),'pageTitle':job.get('pageTitle'),
             'rows':rows,'rules':rules,
             'job':{k:job.get(k) for k in ('publictime',)},'approval_basis':'user_confirmed_library',
             'generated':False,'rebuilt':False})
        dump(folder/'node-output.json', output)
        result.update(status='candidate_pass',production_ready=True,**output,
                      verified_asset=str(copied),asset_sha256=digest,
                      matched_indicator_count=len({str(r['indicator_code']) for r in output['OUTDATA']}))
    except Exception as exc:
        from .app import dump
        from .output_checks import OutputCheckError
        code = exc.code if isinstance(exc, OutputCheckError) else 'LOCAL_ASSET_EXECUTION_FAILED'
        dump(folder/'existing-error.json',{'error_type':type(exc).__name__,'message':str(exc)})
        result.update(failure_kind=code,failure_stage=stage,
                      public_reason='已有解析资产执行或匹配未通过；未生成代码、未重建，请检查本机 existing-error.json。')
    result['elapsed_seconds'] = round(time.monotonic()-began,3)
    dump(folder/'run-result.json',result)
    return result


def delivery(result):
    """独立的用户确认资产交付契约，不伪装成DeepSeek独立审核通过。"""
    if result.get('status') != 'candidate_pass':
        from .error_reporting import public_error
        error = public_error(result)
        return {'status':'failed','parse_strategy':result['parse_strategy'],'stage':error['stage'],'error':error}
    folder = Path(result['output_dir']).resolve()
    asset = Path(result['verified_asset']).resolve()
    if not asset.is_relative_to(folder) or not asset.is_file():
        raise ValueError('已有资产交付路径无效')
    evidence = json.loads((folder/'existing-evidence.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    if digest != evidence['asset_sha256'] or digest != result['asset_sha256']:
        raise ValueError('已有资产在执行后发生变化')
    strategy = result['parse_strategy']
    output = matching_output(evidence['rows'],evidence['rules'],evidence['job'],strategy)
    if output != json.loads((folder/'node-output.json').read_text(encoding='utf-8')):
        raise ValueError('已有资产匹配结果发生变化')
    if strategy in {'excel','html'}:
        fields = ('indicator_name','indicator_data','indicator_unit','end_date')
        output['excel_data'] = [{k:r[k] for k in fields} for r in evidence['rows']]
    return {'status':'ready','parse_strategy':strategy,'result':output,
            'cache_hit':True,'cache_kind':'trusted_library','approval_basis':'user_confirmed_library',
            'asset':{'type':'prompt' if strategy=='prompt' else 'python','filename':asset.name,
                     'content':asset.read_bytes().decode('utf-8-sig'),'sha256':hashlib.sha256(asset.read_bytes().decode('utf-8-sig').encode('utf-8')).hexdigest()},
            'metrics':{'matched_indicator_count':result['matched_indicator_count'],'attempts':1,'rebuilds':0,'generation_calls':0,
                       'elapsed_seconds':result['elapsed_seconds']}}
