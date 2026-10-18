"""HTML已审核核心的自动平台导出；复用DeepSeek通用适配器并实测MD5入口。"""
import ast
import hashlib
import json
from pathlib import Path

from .platform_export import ASSETS, PlatformExportError, run_platform
from .source_download import DEFAULT_DOWNLOAD_BASE_URL, download_url
from .worker import check_code


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_html_adapter(assets=None):
    assets = Path(assets or ASSETS)
    prefix = (assets / 'html-platform-prefix.py.txt').read_text(encoding='utf-8')
    adapter = (assets / 'html-platform-adapter.py.txt').read_text(encoding='utf-8')
    proof = json.loads((assets / 'html-platform-adapter.provenance.json').read_text(encoding='utf-8'))
    if proof.get('provider') != 'deepseek' or digest(prefix) != proof.get('prefix_sha256') or digest(adapter) != proof.get('adapter_sha256'):
        raise PlatformExportError('HTML平台适配资产来源或哈希校验失败')
    original, patch = proof['original_response'], proof['url_patch_response']
    if (prefix != original['prefix_code'] or original['adapter_code'].count(patch['old_line']) != 1
            or original['adapter_code'].replace(patch['old_line'], patch['new_line'], 1) != adapter):
        raise PlatformExportError('HTML适配资产不能从原始DeepSeek响应重建')
    return prefix, adapter, proof


def configure_endpoint(adapter, base):
    download_url(base, 'validation-only')
    assignments = [n for n in ast.parse(adapter).body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '_DOWNLOAD_BASE_URL' for t in n.targets)]
    if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Constant):
        raise PlatformExportError('HTML适配器下载配置位置不唯一')
    node = assignments[0]
    if node.lineno != node.end_lineno:
        raise PlatformExportError('HTML适配器下载配置须为单行常量')
    lines = adapter.splitlines(keepends=True)
    lines[node.lineno - 1] = '_DOWNLOAD_BASE_URL = ' + repr(base.rstrip('/')) + '\n'
    return ''.join(lines)


def top_symbols(code):
    symbols = set()
    for node in ast.parse(code).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            symbols.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return symbols


def export_html_platform_parser(core_path, expected_rows, params, run_dir, job, rules, timeout=65, runner=None):
    from .markdown_protocol import match_rows, node_output
    runner = runner or run_platform
    folder = Path(run_dir) / 'platform'
    folder.mkdir()
    report_path = folder / 'platform-validation.json'
    def save(value):
        report_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        if job.get('platform_verify_download', True) is not True:
            raise PlatformExportError('HTML平台交付必须完成真实markdownMD5下载验证，不能跳过')
        if not params.get('markdownMD5'):
            raise PlatformExportError('HTML平台验证缺少markdownMD5，禁止回退其他MD5')
        core = Path(core_path).read_text(encoding='utf-8')
        check_code(core)
        prefix, adapter, proof = load_html_adapter()
        # 通用适配器原注释来自最初待核验案例；任务核验状态由框架元数据提供。
        # 仅提取模型生成的原始入口函数，避免沿用其他任务的状态说明。
        prefix_tree = ast.parse(prefix)
        if len(prefix_tree.body) != 1 or not isinstance(prefix_tree.body[0], ast.FunctionDef) or prefix_tree.body[0].name != 'main':
            raise PlatformExportError('HTML平台前置入口结构异常')
        entry = prefix_tree.body[0]
        prefix_code = '\n'.join(prefix.splitlines()[entry.lineno-1:entry.end_lineno]) + '\n'
        scope = 'Markdown回归通过，原网页待核验' if job.get('markdown_only_regression', False) else '原网页与Markdown核验范围见Agent运行报告'
        prefix = '# HTML平台入口：接收markdownMD5并自动下载正文。\n# 验证范围：' + scope + '\n' + prefix_code
        collisions = (top_symbols(core) & top_symbols(adapter)) - {'main'}
        if collisions:
            raise PlatformExportError('HTML适配器与业务核心全局名称冲突: ' + ','.join(sorted(collisions)))
        adapter = configure_endpoint(adapter, job.get('download_base_url', DEFAULT_DOWNLOAD_BASE_URL))
        code = prefix + '\n' + core + '\n' + adapter
        compile(code, 'html-platform-parser.py', 'exec')
        candidate = folder / 'unverified-platform-parser.py'
        candidate.write_text(code, encoding='utf-8')
        platform_params = {key: params[key] for key in ('markdownMD5', 'title', 'publictime') if key in params}
        actual = runner(candidate, platform_params, folder, 'download', timeout)
        minimal = runner(candidate, {'markdownMD5': params['markdownMD5']}, folder, 'minimal-download', timeout)
        for label, response in (('平台参数', actual), ('仅markdownMD5', minimal)):
            if response.get('code', 0) != 0 or response.get('ExcelData') != expected_rows:
                raise PlatformExportError(label + '真实下载解析与已审核核心结果不一致')
        expected_match = node_output(match_rows(expected_rows, rules, job), rules, 'html')
        actual_match = node_output(match_rows(actual['ExcelData'], rules, job), rules, 'html')
        if actual_match != expected_match:
            raise PlatformExportError('HTML平台匹配JSON与Agent审核结果不一致')
        path = folder / 'platform-parser.py'
        candidate.replace(path)
        path.with_suffix('.txt').write_bytes(path.read_bytes())
        report = {'status': 'verified', 'adapter_provider': proof['provider'], 'adapter_model': proof.get('model'),
                  'adapter_source_sha256': proof['adapter_sha256'], 'configured_adapter_sha256': digest(adapter),
                  'prefix_source_sha256': proof['prefix_sha256'], 'entry_code_preserved': True,
                  'core_preserved_verbatim': core in code, 'core_sha256': digest(core),
                  'platform_code_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'real_download_tested': True, 'minimal_md5_only_equal': True,
                  'platform_rows_equal_verified_rows': True, 'matching_output_equal': True,
                  'parsed_count': len(expected_rows), 'distinct_indicators': len({r['indicator_code'] for r in actual_match['OUTDATA']}),
                  'original_webpage_verification_deferred': job.get('markdown_only_regression', False)}
        save(report)
        (folder / 'matching-output.json').write_text(json.dumps(actual_match, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'platform_parser_path': str(path), 'platform_parser_text_path': str(path.with_suffix('.txt')),
                'platform_validation_path': str(report_path), 'platform_download_verified': True,
                'platform_adapter_provider': proof['provider']}
    except Exception as exc:
        save({'status': 'failed', 'message': str(exc), 'formal_output_allowed': False})
        if isinstance(exc, PlatformExportError):
            raise
        raise PlatformExportError('HTML平台导出/验证失败: ' + str(exc)) from exc
