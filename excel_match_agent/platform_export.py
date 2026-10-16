"""把经审核的DeepSeek核心与有来源记录的通用适配层组合，并实测平台入口。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .worker import check_code
from .source_download import DEFAULT_DOWNLOAD_BASE_URL, download_url

ASSETS = Path(__file__).with_name('assets')


class PlatformExportError(ValueError):
    pass


def load_adapter():
    adapter = (ASSETS / 'platform-adapter.py.txt').read_text(encoding='utf-8')
    proof = json.loads((ASSETS / 'platform-adapter.provenance.json').read_text(encoding='utf-8'))
    if proof.get('provider') != 'deepseek' or hashlib.sha256(adapter.encode('utf-8')).hexdigest() != proof.get('sha256'):
        raise PlatformExportError('平台适配层来源或哈希校验失败')
    compile(adapter, 'platform-adapter', 'exec')
    return adapter, proof


def run_platform(code_path, params, folder, label, timeout):
    input_path, output_path = folder / (label + '-input.json'), folder / (label + '-output.json')
    input_path.write_text(json.dumps(params, ensure_ascii=False, default=str), encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
    try:
        process = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(Path(__file__).with_name('platform_worker.py')),
            str(code_path), str(input_path), str(output_path)], capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise PlatformExportError('平台入口下载/解析测试超时') from None
    if process.returncode or not output_path.exists():
        raise PlatformExportError('平台入口测试进程失败，未导出成功结果')
    result = json.loads(output_path.read_text(encoding='utf-8'))
    if not isinstance(result, dict) or result.get('code', 0) != 0 or not isinstance(result.get('ExcelData'), list) or not result['ExcelData']:
        message = result.get('msg', '缺少非空ExcelData') if isinstance(result, dict) else '返回结构错误'
        raise PlatformExportError('平台入口失败：' + str(message)[:1000])
    return result


def export_platform_parser(core_path, expected_rows, params, run_dir, job, timeout=65, runner=None):
    from .app import match_evaluation
    from .output_format import workflow_output
    runner = runner or run_platform
    core_path, run_dir = Path(core_path), Path(run_dir)
    folder = run_dir / 'platform'
    folder.mkdir()
    core = core_path.read_text(encoding='utf-8')
    check_code(core)
    adapter, proof = load_adapter()
    base_url = job.get('download_base_url', DEFAULT_DOWNLOAD_BASE_URL)
    download_url(base_url, params.get('fileMd5') or 'validation')
    marker = json.dumps(DEFAULT_DOWNLOAD_BASE_URL.rstrip('/') + '/')
    if adapter.count(marker) != 1:
        raise PlatformExportError('平台下载地址模板不是唯一配置位置')
    adapter = adapter.replace(marker, json.dumps(base_url.rstrip('/') + '/'), 1)
    code = core + '\n\n# 平台下载读取入口：DeepSeek生成的通用适配层，原业务源码保持不变。\n' + adapter + '\n'
    compile(code, 'platform-parser.py', 'exec')
    path = folder / 'platform-parser.py'
    path.write_text(code, encoding='utf-8')
    (folder / 'platform-parser.txt').write_bytes(path.read_bytes())
    snapshot = runner(path, params, folder, 'snapshot', timeout)
    if snapshot['ExcelData'] != expected_rows:
        raise PlatformExportError('平台版注入源表重跑与已审核核心结果不一致')
    live = bool(job.get('platform_verify_download', True))
    platform_params = {**job.get('upstream_metadata', {}), **{k: v for k, v in params.items()
                       if k in {'fileMd5', 'pageTitle', 'publictime'}}}
    platform_params.pop('workbook', None)
    # 没有实际文件名输入；验证平台能依靠下载元数据及真实源表确定期间。
    for key in ('source_name', 'fileName', 'filename'):
        platform_params.pop(key, None)
    if live:
        if not platform_params.get('fileMd5'):
            raise PlatformExportError('平台真实下载测试需要fileMd5')
        actual = runner(path, platform_params, folder, 'download', timeout)
        if actual['ExcelData'] != expected_rows:
            raise PlatformExportError('平台真实下载解析与Agent已审核记录不一致，需检查读取结构或期间来源')
    else:
        actual = snapshot
    rules = json.loads((run_dir / 'input-context.json').read_text(encoding='utf-8'))['GZKJG']
    expected_match = workflow_output(match_evaluation(expected_rows, rules, job))
    actual_match = workflow_output(match_evaluation(actual['ExcelData'], rules, job))
    if actual_match != expected_match:
        raise PlatformExportError('平台实际解析数据的匹配输出与Agent结果不一致')
    report = {'status': 'verified' if live else 'snapshot_verified', 'core_preserved_verbatim': code.startswith(core),
        'core_path': str(core_path), 'core_sha256': hashlib.sha256(core.encode('utf-8')).hexdigest(),
        'adapter_provider': proof['provider'], 'adapter_model': proof.get('model'), 'adapter_template_sha256': proof['sha256'], 'adapter_sha256': hashlib.sha256(adapter.encode('utf-8')).hexdigest(),
        'platform_code_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'snapshot_equal': True, 'real_download_tested': live, 'platform_rows_equal_verified_rows': True,
        'matching_output_equal': True, 'parsed_count': len(expected_rows),
        'matched_records': len(actual_match['OUTDATA']),
        'distinct_indicators': len({r['indicator_code'] for r in actual_match['OUTDATA']}),
        'end_dates': sorted({r['end_date'] for r in expected_rows})}
    (folder / 'platform-validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / 'matching-output.json').write_text(json.dumps(actual_match, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'platform_parser_path': str(path), 'platform_parser_text_path': str(folder / 'platform-parser.txt'),
            'platform_validation_path': str(folder / 'platform-validation.json'), 'platform_download_verified': live}
