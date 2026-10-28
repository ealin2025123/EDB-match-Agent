"""已审核资产缓存：只复用代码，当前源数据仍执行、审核和匹配。"""
import hashlib
import json
from pathlib import Path
import re

VERSION = 1


def page(context):
    from .excel_rule_input import normalize_page
    return normalize_page(context.get('pageTitle') or context.get('rule', {}).get('路径', ''))


def group_folder(folder, context):
    return Path(folder)/hashlib.sha256(page(context).encode()).hexdigest()


def asset_path(folder, context, strategy):
    return group_folder(folder,context)/(key(context,strategy)+'.json')


def library_root(folder):
    folder = Path(folder).resolve()
    return (folder.parent.parent if folder.name == 'code-cache' else folder.parent)/'code'


def filename(context, strategy):
    if strategy not in {'html','excel','prompt'}:
        raise ValueError('代码库解析分支非法')
    name = page(context).replace('\\','_')
    name = re.sub(r'[<>:"|?*\x00-\x1f]', '_', name).rstrip(' .')
    if not name:
        name = '_未指定路径'
    if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        name = '_' + name
    if len(name) > 180:
        name = name[:160] + '_' + hashlib.sha256(page(context).encode()).hexdigest()[:12]
    return name + ('.md' if strategy == 'prompt' else '.py')


def library_path(folder, context, strategy):
    return library_root(folder)/strategy/filename(context,strategy)


def canonical_request(value):
    value = dict(value)
    value.pop('workflow_run_id', None)
    value['GZKJG'] = sorted(value.get('GZKJG', []), key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True))
    return value


def key(context, strategy):
    skill = [x.get('content') if isinstance(x, dict) else x for x in context.get('skill', [])]
    business = []
    for item in context.get('business_rules', []):
        if item.get('rules'):
            business.append({'rules':[{'fields':r['fields'],'routing':r.get('routing')} for r in item['rules']]})
        else:
            business.append({k:v for k,v in item.items() if k not in {'path','selection'}})
    payload = {'version':VERSION,'strategy':strategy,'pageTitle':page(context),
               'skill':skill,'business_rules':business,'rule':context.get('rule'), 'regex':context.get('regex'),
               'rules':canonical_request({'GZKJG':context.get('GZKJG', [])})['GZKJG']}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def current_scope(job, base):
    from .app import ROOT, business_context, skill_context
    from .markdown_agent import strategy_for, task_rules
    from .markdown_inputs import rule_row, regex_rules
    resolve = lambda value: (Path(base)/value).resolve()
    strategy = strategy_for(job)
    if strategy == 'excel':
        return {'pageTitle':job.get('pageTitle',''),'GZKJG':job['GZKJG'],
                'skill':skill_context(resolve(job['skill_path'])),'business_rules':business_context(job, resolve)}
    row = rule_row(job, resolve)
    regex = regex_rules(job, row, resolve) if strategy == 'html' or '登记正则' in str(row.get('指标名称取值逻辑','')) else {'rules':[],'format':'not_needed'}
    name = 'html-rule-parser-codegen' if strategy == 'html' else 'statistical-markdown-prompt-codegen'
    skill = resolve(job['skill_path']) if job.get('skill_path') else ROOT/'skills'/name/'SKILL.md'
    return {'pageTitle':job.get('pageTitle',''),'rule':row,'skill':[x['content'] for x in skill_context(skill)],'GZKJG':task_rules(job, resolve),
            'regex':{'format':regex['format'],'sheet':regex.get('sheet',''),'rules':regex['rules']}}


def lookup(folder, context, strategy, for_storage=False):
    if not folder:
        return None
    path = asset_path(folder,context,strategy)
    if not path.exists():
        path = Path(folder)/(key(context,strategy)+'.json')  # 兼容分组前的缓存。
    try:
        item = json.loads(path.read_text(encoding='utf-8'))
        field = 'prompt_code' if strategy == 'prompt' else 'python_code'
        code = item['response'][field]
        if item['version'] != VERSION or item['key'] != key(context,strategy) or hashlib.sha256(code.encode()).hexdigest() != item['sha256']:
            return None
        if not for_storage:
            published = library_path(folder,context,strategy)
            if (item.get('library_path') != str(published) or not published.is_file()
                    or hashlib.sha256(published.read_bytes()).hexdigest() != item.get('library_sha256')):
                return None
            published_text = published.read_text(encoding='utf-8')
            if (published_text != code if strategy == 'prompt' else code.rstrip() not in published_text):
                return None
        if (not for_storage and context.get('source_sha256') and item.get('source_sha256')
                and context['source_sha256'] != item['source_sha256']
                and re.search(r'\b(?:19|20)\d{2}-\d{2}-\d{2}\b',code)):
            return None  # 新源文件不直接复用含固定日期的旧资产。
        if strategy != 'prompt':
            from .worker import check_code
            check_code(code)
        return item
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store_success(folder, result):
    """调用方须先通过approved_delivery；只索引本次最终通过的资产。"""
    if result.get('status') != 'candidate_pass':
        return False
    strategy = result.get('parse_strategy','excel')
    review = result.get('name_review',{})
    if review.get('name_status' if strategy == 'excel' else 'status') != 'correct':
        return False
    root = Path(result['output_dir']).resolve()
    context = json.loads((root/'input-context.json').read_text(encoding='utf-8'))
    if strategy == 'excel':
        assets = [h for h in result['history'] if h.get('attempt') == result['attempts'] and h.get('code_path')]
        if len(assets) != 1:
            return False
        asset = Path(assets[0]['code_path']).resolve()
    else:
        asset = Path(result['verified_asset']).resolve()
    if not asset.is_relative_to(root) or not asset.is_file():
        return False
    code = asset.read_text(encoding='utf-8')
    if strategy != 'prompt':
        from .worker import check_code
        check_code(code)
    response = {'decision':'generate','explanation':'复用同一路径已审核通过资产；仍用本次源文件执行和审核',
                'prompt_code' if strategy == 'prompt' else 'python_code':code}
    derived = asset.parent/'derived-rules.json'
    if strategy == 'excel' and derived.exists():
        response['derived_rules'] = json.loads(derived.read_text(encoding='utf-8'))
    digest = key(context,strategy)
    published_source = Path(result.get('platform_parser_path') or asset) if strategy != 'prompt' else asset
    published_source = published_source.resolve()
    if not published_source.is_relative_to(root) or not published_source.is_file():
        return False
    published_raw = published_source.read_bytes()
    published_text = published_raw.decode('utf-8').replace('\r\n','\n')
    if (published_text != code if strategy == 'prompt' else code.rstrip() not in published_text):
        return False
    payload = {'version':VERSION,'key':digest,'sha256':hashlib.sha256(code.encode()).hexdigest(),
               'source_run':str(root),'strategy':strategy,'response':response,
               'source_sha256':context.get('source_sha256') or result.get('source_identity',{}).get('sha256') or result.get('source_acquisition',{}).get('sha256'),
               'approved_at':(root/'run-result.json').stat().st_mtime if (root/'run-result.json').exists() else root.stat().st_mtime}
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    existing = lookup(folder,context,strategy,for_storage=True)
    if existing and existing.get('approved_at',0) > payload['approved_at']:
        return True  # 历史任务不得覆盖更新的已通过代码。
    group = group_folder(folder,context)
    group.mkdir(parents=True,exist_ok=True)
    index_path = group/'group.json'
    try:
        index = json.loads(index_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        index = {'pageTitle':page(context),'versions':{},'latest_by_strategy':{}}
    index.setdefault('published_assets',{})
    published = library_path(folder,context,strategy)
    payload.update(library_path=str(published),library_sha256=hashlib.sha256(published_raw).hexdigest())
    current_published = index['published_assets'].get(strategy)
    if not current_published or current_published.get('approved_at',0) <= payload['approved_at']:
        if published.exists():
            actual_hash = hashlib.sha256(published.read_bytes()).hexdigest()
            if current_published:
                if actual_hash != current_published['sha256']:
                    raise ValueError('code目录中的代码已被手工改动，未自动覆盖')
            elif actual_hash != payload['library_sha256']:
                raise ValueError('code目录存在未登记的同名文件，未自动覆盖')
            # 路径替换为下划线后可能重名，不能覆盖其他路径的代码。
            for other_path in folder.glob('*/group.json'):
                if other_path == index_path:
                    continue
                other = json.loads(other_path.read_text(encoding='utf-8'))
                if any(item.get('path') == str(published) for item in other.get('published_assets',{}).values()):
                    raise ValueError('不同路径生成了同名代码文件，未覆盖其他路径')
        for branch in ('html','excel','prompt'):
            (library_root(folder)/branch).mkdir(parents=True,exist_ok=True)
        temporary = published.with_suffix(published.suffix+'.tmp')
        temporary.write_bytes(published_raw)
        temporary.replace(published)
        index['published_assets'][strategy] = {'path':str(published),'sha256':payload['library_sha256'],
                                               'scope_key':digest,'approved_at':payload['approved_at']}
    temporary = group/(digest+'.tmp')
    temporary.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
    temporary.replace(group/(digest+'.json'))
    version = {k:payload[k] for k in ('strategy','sha256','source_run','approved_at')}
    index['versions'][digest] = version
    current = index['latest_by_strategy'].get(strategy)
    if not current or index['versions'].get(current,{}).get('approved_at',0) <= version['approved_at']:
        index['latest_by_strategy'][strategy] = digest
    temporary = group/'group.tmp'
    temporary.write_text(json.dumps(index,ensure_ascii=False),encoding='utf-8')
    temporary.replace(index_path)
    return True
