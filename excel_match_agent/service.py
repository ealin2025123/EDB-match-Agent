"""DC HTTP接入：持久受理、幂等查询、独立Agent进程和通过审核的资产交付。"""
import argparse
import base64
from contextlib import closing
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .app import ROOT, normalize_rules
from .code_cache import canonical_request, store_success, current_scope, key as cache_key, lookup as lookup_code_cache, filename as code_filename

DEFAULT_CONFIG = Path(__file__).parent / 'service-runtime/config.json'
MAX_REQUEST_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 16_000_000
TERMINAL = {'ready', 'failed', 'interrupted'}
TYPES = {'xls':'excel','xlsx':'excel','csv':'excel','html':'html','htm':'html','shtml':'html',
         'pdf':'prompt','md':'prompt','markdown':'prompt','txt':'prompt',
         'doc':'prompt','docx':'prompt','png':'prompt','jpg':'prompt','jpeg':'prompt'}

CACHE_VALID = 'NOT EXISTS (SELECT 1 FROM task_cache_invalidations i WHERE i.task_id=tasks.id)'
CACHE_INVALIDATION_SCHEMA = ('CREATE TABLE IF NOT EXISTS task_cache_invalidations '
    '(task_id TEXT PRIMARY KEY, invalidated_at REAL NOT NULL, reason TEXT NOT NULL, previous_workflow_id TEXT)')


def invalidate_job_cache(database, task_id):
    """只解除终止任务的幂等复用，不删除任务或触发执行。"""
    with closing(sqlite3.connect(database, timeout=15)) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(CACHE_INVALIDATION_SCHEMA)
        row = connection.execute('SELECT state,workflow_id FROM tasks WHERE id=?',(task_id,)).fetchone()
        if row is None or row[0] not in {'failed','interrupted'}:
            raise ValueError('仅允许清除已失败或中断任务的复用缓存，不影响排队或运行任务')
        connection.execute('INSERT OR IGNORE INTO task_cache_invalidations VALUES (?,?,?,?)',
                           (task_id,time.time(),'user_requested_cache_clear',row[1]))
        # workflow_id是幂等索引；原始运行ID仍保存在payload及清除审计中。
        if row[1]:
            connection.execute('UPDATE tasks SET workflow_id=NULL WHERE id=?',(task_id,))
    return {'job_id':task_id,'cache_cleared':True,'history_preserved':True,'new_job_started':False}


class APIError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def transport_reply(value):
    """在HTTP传输层备份已审核资产，兼容未备份PythonCode的旧DC接入脚本。"""
    if not isinstance(value, dict) or value.get('status') != 'ready':
        return value
    asset, result = value.get('asset'), value.get('result')
    if not isinstance(asset, dict) or not isinstance(result, dict):
        raise APIError(500, 'DELIVERY_INVALID', '已完成任务缺少交付资产或结果，请检查任务记录')
    content = asset.get('content')
    if (not isinstance(content, str) or not content
            or hashlib.sha256(content.encode('utf-8')).hexdigest() != asset.get('sha256')):
        raise APIError(500, 'DELIVERY_INVALID', '已完成任务的交付资产哈希校验失败，请检查任务记录')
    transfer = {'version':1, 'job_id':str(value.get('job_id','')),
                'parse_strategy':value.get('parse_strategy'), 'sha256':asset['sha256'],
                'filename':asset.get('filename',''),
                'content_b64':base64.b64encode(content.encode('utf-8')).decode('ascii')}
    # 不写入数据库或node-output.json；接出校验并移除传输字段后恢复业务契约。
    return {**value, 'result':{**result, '_asset_transfer':transfer}}


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('重复JSON字段')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('非有限JSON数值')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def normalize_request(value):
    if not isinstance(value, dict):
        raise APIError(422, 'INVALID_REQUEST', '请求体必须是JSON对象')
    value = dict(value)
    for old, new in (('pagetitle','pageTitle'),('filemd5','fileMd5'),('markdownMd5','markdownMD5')):
        if old in value:
            if new in value and value[new] != value[old]:
                raise APIError(422, 'ALIAS_CONFLICT', '同一字段别名内容冲突')
            value[new] = value.pop(old)
    allowed = {'pageTitle','fileMd5','markdownMD5','fileType','title','publictime','GZKJG',
               'GZK','workflow_run_id','profile','rule_row'}
    if set(value) - allowed:
        raise APIError(422, 'UNKNOWN_FIELD', '仅接受业务入参，不接受代码、配置路径或模型认证设置')
    for key in ('pageTitle','fileMd5','markdownMD5','fileType','title','publictime','workflow_run_id','profile'):
        item = value.get(key, '')
        if item is None:
            item = ''
        if not isinstance(item, str) or len(item) > (4096 if key in {'title','pageTitle'} else 256):
            raise APIError(422, 'INVALID_FIELD', '字符串字段类型或长度非法: ' + key)
        value[key] = item.strip()
    if not value['pageTitle']:
        raise APIError(422, 'MISSING_PAGE_TITLE', '缺少pageTitle')
    filetype = value['fileType'].lower().lstrip('.')
    if filetype not in TYPES:
        raise APIError(422, 'UNSUPPORTED_FILE_TYPE', 'fileType缺失或不受支持')
    value['fileType'], value['parse_strategy'] = filetype, TYPES[filetype]
    key = 'fileMd5' if value['parse_strategy'] == 'excel' else 'markdownMD5'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value[key]):
        raise APIError(422, 'MISSING_SOURCE_ID', '缺少或非法源标识: ' + key)
    raw = value.get('GZKJG')
    if raw is None and isinstance(value.get('GZK'), dict):
        raw = value['GZK'].get('GZKJG')
    if isinstance(raw, str):
        try:
            raw = strict_json(raw)
        except ValueError:
            raise APIError(422, 'INVALID_GZK', '指标规则为空或无效：GZKJG字符串不是有效JSON') from None
    if raw is None or raw == []:
        raise APIError(422, 'GZKJG_EMPTY', '指标规则为空或无效：GZKJG为0条或缺失，停止生成，请检查pageTitle对应的规则库')
    if not isinstance(raw, list) or len(raw) > 10000:
        raise APIError(422, 'INVALID_GZK', '指标规则为空或无效：GZKJG须为数组且不超过10000条')
    try:
        rules = normalize_rules(raw)
    except (TypeError,ValueError,KeyError,AttributeError):
        raise APIError(422, 'INVALID_GZK', '指标规则为空或无效：GZKJG记录结构非法') from None
    if len(rules) != len(raw) or any(not isinstance(r.get('rule_indicator_name'),str)
            or not r['rule_indicator_name'].strip() or not r.get('rule_indicator_code') for r in rules):
        raise APIError(422, 'INVALID_GZK', '指标规则为空或无效：规则必须包含有效指标名称与编码')
    value.pop('GZK', None)
    value['GZKJG'] = rules
    value['profile'] = value['profile'] or value['parse_strategy']
    if 'rule_row' in value and (type(value['rule_row']) is not int or not 2 <= value['rule_row'] <= 1048576):
        raise APIError(422, 'INVALID_RULE_ROW', 'rule_row须为从2开始的工作簿行号')
    return value


def init_config(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    profiles = {}
    for strategy, filename in (('excel','excel-task-1.json'),('html','html-task-2.json'),('prompt','prompt-task-3.json')):
        folder = Path(__file__).parent / 'examples'
        selected = folder / filename
        if not selected.is_file():
            selected = ROOT / 'examples' / (strategy + '-job.json')
        profiles[strategy] = str(selected.resolve())
    config = {'host':'127.0.0.1','port':8767,'api_token':secrets.token_urlsafe(32),
              'runtime_dir':str(path.parent.resolve()),'profiles':profiles,
              'dedupe_seconds':900,'max_pending_jobs':100}
    # x模式防止已有令牌和本机配置被覆盖；不输出配置或令牌。
    with path.open('x', encoding='utf-8') as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
    return path.resolve()


def read_config(path):
    path = Path(path).resolve()
    config = strict_json(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict) or not isinstance(config.get('api_token'), str) or len(config['api_token']) < 24:
        raise ValueError('独立服务令牌缺失或过短；不使用模型API Key作服务令牌')
    if not isinstance(config.get('profiles'), dict) or not config['profiles']:
        raise ValueError('缺少服务器端任务配置profiles')
    config['runtime_dir'] = str((path.parent / config.get('runtime_dir', '.')).resolve())
    config['profiles'] = {k:str((path.parent / v).resolve()) for k,v in config['profiles'].items()}
    port = config.get('port', 8767)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('服务端口非法')
    return config


def prepare_job(request, profile, task_dir):
    profile = Path(profile).resolve()
    job = strict_json(profile.read_text(encoding='utf-8-sig'))
    if not isinstance(job, dict):
        raise ValueError('服务器任务配置必须是对象')
    from .markdown_agent import strategy_for
    if strategy_for(job) != request['parse_strategy']:
        raise APIError(422,'PROFILE_TYPE_MISMATCH','任务模板与本次文件类型不属于同一解析分支')
    # 示例profile含单任务规则/精度修复。只允许同一路径复用，防止新任务套用旧规则。
    bound_page = (job.get('rule') or {}).get('路径') or job.get('pageTitle') or job.get('pagetitle')
    if bound_page != request['pageTitle'] or ('rule_row' in request and request['rule_row'] != job.get('rule_row')):
        for key in ('rule','rule_row','path_code','rule_overrides','rule_override_provenance'):
            job.pop(key,None)
        if request['parse_strategy'] in {'html','prompt'}:
            job['rule_source'] = 'workbook'
    # 不能带入旧任务的MD5、元数据、输入文件或续跑标识。
    for key in ('pagetitle','filemd5','start_file','gzk_file','GZK','source_path','markdown_path',
                'upstream_metadata','resume_from_run','replay_generated_from_run','continue_from_run',
                'repair_generation_from_run','resume_review_cache','resume_incomplete_review'):
        job.pop(key, None)
    for key in ('pageTitle','fileMd5','markdownMD5','fileType','title','publictime','GZKJG','parse_strategy'):
        job[key] = request[key]
    if 'rule_row' in request:
        job['rule_row'] = request['rule_row']
    job.update(output_dir=str((task_dir/'runs').resolve()), max_attempts=3,
               code_cache_dir=str((task_dir.parent/'code-cache').resolve()))
    if request['parse_strategy'] in {'excel','html'}:
        job.update(export_platform_parser=True, platform_verify_download=True)
    return job, profile.parent


def approved_delivery(result):
    strategy = result.get('parse_strategy', 'excel')
    # 既有Excel版本成功时仍保留历史production_ready=false；以成功状态、
    # 独立名称审核和真实平台验证为准，不把该旧展示字段当业务失败。
    if result.get('status') != 'candidate_pass' or (strategy != 'excel' and result.get('production_ready') is not True):
        code = result.get('failure_kind') or 'AGENT_NOT_APPROVED'
        from .error_reporting import public_error
        error = public_error(result)
        return {'status':'failed', 'stage':error['stage'], 'parse_strategy':strategy, 'error':error}
    root = Path(result['output_dir']).resolve()
    review = result.get('name_review',{})
    if (review.get('name_status') != 'correct' if strategy == 'excel' else review.get('status') != 'correct'):
        raise ValueError('缺少本次独立审核通过结果')
    asset = Path(result.get('verified_asset', '')) if strategy == 'prompt' else Path(result.get('platform_parser_path', ''))
    asset = asset.resolve()
    if not asset.is_relative_to(root) or not asset.is_file():
        raise ValueError('通过资产不在本次输出目录内')
    if strategy != 'prompt' and result.get('platform_download_verified') is not True:
        # Excel导出器以平台报告记录真实下载；HTML另有顶层布尔字段。
        report = strict_json((root/'platform/platform-validation.json').read_text(encoding='utf-8'))
        if report.get('status') != 'verified' or report.get('real_download_tested') is not True:
            raise ValueError('完整平台代码未经真实下载验证')
    output = strict_json((root/'node-output.json').read_text(encoding='utf-8'))
    keys = {'OUTDATA'} if strategy == 'prompt' else {'unitArray','codeOutputResult','OUTDATA'}
    if set(output) != keys or not isinstance(output['OUTDATA'], list):
        raise ValueError('最终JSON与分支契约不一致')
    if output != {key:result.get(key) for key in keys}:
        raise ValueError('节点JSON与已审核Agent结果不一致')
    from .output_checks import check_extraction, OutputCheckError
    try:
        check_extraction(output['OUTDATA'])
    except OutputCheckError as exc:
        from .error_reporting import public_error
        error = public_error({'failure_kind':exc.code})
        return {'status':'failed','parse_strategy':strategy,'stage':error['stage'],'error':error}
    matched = len({str(row['indicator_code']) for row in output['OUTDATA'] if str(row.get('indicator_code','')) not in {'','empty'}})
    if matched < 2:
        raise ValueError('通过结果不足两个不同编码')
    raw = asset.read_bytes()
    code = raw.decode('utf-8')
    context_path = root/'input-context.json'
    display_name = code_filename(strict_json(context_path.read_text(encoding='utf-8')),strategy) if context_path.is_file() else asset.name
    if strategy != 'prompt':
        report = strict_json((root/'platform/platform-validation.json').read_text(encoding='utf-8'))
        if report.get('status') != 'verified' or report.get('real_download_tested') is not True:
            raise ValueError('完整平台代码未通过真实下载')
        if hashlib.sha256(raw).hexdigest() != report.get('platform_code_sha256'):
            raise ValueError('交付代码与平台验证时的哈希不一致')
    else:
        approved = [h.get('asset_sha256') for h in result.get('history',[]) if h.get('asset') == str(asset)]
        if not approved or hashlib.sha256(code.replace('\r\n','\n').encode()).hexdigest() != approved[-1]:
            raise ValueError('提示词与审核资产不一致')
    delivery = {'status':'ready','parse_strategy':strategy,'result':output,
                'cache_hit':bool(result.get('cache_hit',False)), 'cache_kind':result.get('cache_kind',''),
                'asset':{'type':'prompt' if strategy == 'prompt' else 'python','filename':display_name,
                         'content':code,'sha256':hashlib.sha256(raw).hexdigest()},
                'metrics':{'matched_indicator_count':matched,'attempts':result.get('attempts',0),
                           'rebuilds':result.get('rebuilds',0),'elapsed_seconds':result.get('elapsed_seconds',0)}}
    if strategy in {'excel','html'}:
        # Deliver the actual final reviewed parser output, not matched rows or
        # data from an earlier attempt. Keep local evidence paths server-side.
        attempt = result.get('attempts')
        history = result.get('history') or []
        path_field = 'code_path' if strategy == 'excel' else 'asset'
        final = [h for h in history if h.get('attempt') == attempt and h.get(path_field)]
        if len(final) != 1:
            raise ValueError('缺少最终通过轮次的解析明细来源')
        parser = Path(final[0][path_field]).resolve()
        if not parser.is_relative_to(root) or not parser.is_file():
            raise ValueError('解析明细来源不在本次输出目录内')
        if strategy == 'html':
            if (parser != Path(result.get('verified_asset','')).resolve()
                    or hashlib.sha256(parser.read_text(encoding='utf-8').encode('utf-8')).hexdigest()
                    != final[0].get('asset_sha256')):
                raise ValueError('HTML解析明细来源与最终审核资产不一致')
            rows = strict_json((parser.parent/'parsed-data.json').read_text(encoding='utf-8'))
        else:
            parsed = strict_json((parser.parent/'ExcelData.json').read_text(encoding='utf-8'))
            rows = parsed.get('ExcelData') if isinstance(parsed,dict) else None
        fields = ('end_date','indicator_data','indicator_unit','indicator_name')
        if not isinstance(rows,list) or any(not isinstance(row,dict) or any(k not in row for k in fields) for row in rows):
            raise ValueError('最终解析明细格式无效')
        try:
            check_extraction(rows)
        except OutputCheckError as exc:
            from .error_reporting import public_error
            error = public_error({'failure_kind':exc.code})
            return {'status':'failed','parse_strategy':strategy,'stage':error['stage'],'error':error}
        output['excel_data'] = [{k:row[k] for k in fields} for row in rows]
    if len(json_text(delivery).encode('utf-8')) > MAX_RESPONSE_BYTES:
        raise ValueError('匹配结果和代码超过接口响应大小上限')
    return delivery


class TaskService:
    def __init__(self, config, runner=None):
        self.config = config
        self.root = Path(config['runtime_dir']).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_file = (self.root/'service.lock').open('a+b')
        if os.fstat(self.lock_file.fileno()).st_size == 0:
            self.lock_file.write(b'0')
            self.lock_file.flush()
        self.lock_file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise RuntimeError('同一运行目录已有服务，不能重复启动') from None
        self.condition = threading.Condition(threading.RLock())
        self.db = sqlite3.connect(self.root/'tasks.sqlite3', check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, workflow_id TEXT UNIQUE, created REAL NOT NULL, updated REAL NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, response TEXT)')
        self.db.execute(CACHE_INVALIDATION_SCHEMA)
        self.db.execute("UPDATE tasks SET state='interrupted', response=?, updated=? WHERE state='running'",
                        (json_text({'status':'interrupted','error':{'code':'SERVICE_RESTARTED','message':'服务重启中断了进行中任务；未自动重建','requires_human_review':True}}), time.time()))
        self.db.commit()
        self.runner = runner or self._run_agent
        self.stop = threading.Event()
        self.process = None
        self.thread = threading.Thread(target=self._loop, name='match-agent-worker', daemon=True)
        self.thread.start()

    def submit(self, payload):
        request = normalize_request(payload)
        profile = self.config['profiles'].get(request['profile'])
        if not profile or not Path(profile).is_file():
            raise APIError(422, 'UNKNOWN_PROFILE', '服务器未配置对应任务模板')
        workflow = request['workflow_run_id'] or None
        fingerprint = hashlib.sha256(json_text(canonical_request(request)).encode()).hexdigest()
        now = time.time()
        with self.condition:
            existing = self.db.execute('SELECT * FROM tasks WHERE workflow_id=? AND '+CACHE_VALID, (workflow,)).fetchone() if workflow else None
            if existing and canonical_request(strict_json(existing['payload'])) != canonical_request(request):
                raise APIError(409, 'IDEMPOTENCY_CONFLICT', '同一workflow_run_id已绑定不同输入')
            if existing is None and not workflow:
                existing = self.db.execute("SELECT * FROM tasks WHERE "+CACHE_VALID+" AND fingerprint=? AND (state IN ('queued','running') OR created>=?) ORDER BY created DESC LIMIT 1",
                                           (fingerprint, now-self.config.get('dedupe_seconds',900))).fetchone()
                if existing is None:
                    existing = next((r for r in self.db.execute("SELECT * FROM tasks WHERE "+CACHE_VALID+" AND (state IN ('queued','running') OR created>=?) ORDER BY created DESC",
                                    (now-self.config.get('dedupe_seconds',900),)).fetchall()
                                     if canonical_request(strict_json(r['payload'])) == canonical_request(request)), None)
            if existing:
                saved = self.root/existing['id']/'agent-result.json'
                if existing['state'] != 'ready' or workflow or not saved.is_file():
                    return self._snapshot(existing), True
                prior = strict_json(saved.read_text(encoding='utf-8'))
                context_path = Path(prior.get('output_dir',''))/'input-context.json'
                if not context_path.is_file():
                    return self._snapshot(existing), True  # 兼容旧版无上下文的幂等记录。
                try:
                    check_job, check_base = prepare_job(request, profile, self.root/'cache-scope-check')
                    old_context = strict_json(context_path.read_text(encoding='utf-8'))
                    check_context = current_scope(check_job, check_base)
                    if (cache_key(check_context,request['parse_strategy']) == cache_key(old_context,request['parse_strategy'])
                            and lookup_code_cache(self.root/'code-cache',check_context,request['parse_strategy'])):
                        approved = approved_delivery(prior)
                        if approved.get('status') == 'ready':
                            return {**self._snapshot(existing),'cache_hit':True,'cache_kind':'result'}, True
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            # 同一业务请求的成功结果长期复用；失败结果仍遵循原来的15分钟幂等窗口。
            folder = self.root/str(uuid.uuid4())
            job, base = prepare_job(request, profile, folder)
            scope = None
            for previous in self.db.execute("SELECT * FROM tasks WHERE state='ready' AND "+CACHE_VALID+" ORDER BY created DESC").fetchall():
                old_request = strict_json(previous['payload'])
                if (old_request.get('pageTitle') != request['pageTitle'] or old_request.get('parse_strategy') != request['parse_strategy']):
                    continue
                saved = self.root/previous['id']/'agent-result.json'
                try:
                    old_result = strict_json(saved.read_text(encoding='utf-8'))
                    approved = approved_delivery(old_result)
                    if approved.get('status') != 'ready':
                        continue
                    store_success(self.root/'code-cache', old_result)
                    if workflow and previous['workflow_id'] != workflow:
                        continue
                    if canonical_request(old_request) != canonical_request(request):
                        continue
                    if scope is None:
                        scope = current_scope(job, base)
                    old_context = strict_json((Path(old_result['output_dir'])/'input-context.json').read_text(encoding='utf-8'))
                    if (cache_key(scope,request['parse_strategy']) == cache_key(old_context,request['parse_strategy'])
                            and lookup_code_cache(self.root/'code-cache',scope,request['parse_strategy'])):
                        return {**self._snapshot(previous),'cache_hit':True,'cache_kind':'result'}, True
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            pending = self.db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('queued','running')").fetchone()[0]
            if pending >= self.config.get('max_pending_jobs',100):
                raise APIError(429, 'QUEUE_FULL', '任务队列已满，请稍后重试')
            task_id = folder.name
            # 在受理前验证配置能形成任务；客户端不得传入路径或模型配置。
            folder.mkdir(exist_ok=False)
            (folder/'request.json').write_text(json_text({'job':job,'job_base':str(base)}),encoding='utf-8')
            self.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,NULL)',
                            (task_id, fingerprint, workflow, now, now, 'queued', json_text(request)))
            self.db.commit()
            row = self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            self.condition.notify_all()
            return self._snapshot(row), False

    def query(self, task_id, wait_seconds=0):
        if not re.fullmatch(r'[a-f0-9-]{36}',task_id):
            raise APIError(404, 'TASK_NOT_FOUND', '未找到任务')
        if not isinstance(wait_seconds,(float,int)) or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 25:
            raise APIError(422, 'INVALID_WAIT', 'wait_seconds须为0至25秒')
        deadline = time.monotonic()+wait_seconds
        with self.condition:
            while True:
                row = self.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
                if not row:
                    raise APIError(404, 'TASK_NOT_FOUND', '未找到任务')
                if row['state'] in TERMINAL or time.monotonic() >= deadline or self.stop.is_set():
                    return self._snapshot(row)
                self.condition.wait(max(.001, deadline-time.monotonic()))

    def _snapshot(self, row):
        response = strict_json(row['response']) if row['response'] else {'status':row['state']}
        invalidated = hasattr(self,'db') and self.db.execute('SELECT 1 FROM task_cache_invalidations WHERE task_id=?',(row['id'],)).fetchone()
        # 只更新查询时的错误展示；不修改失败记录、不重新执行或调用模型。
        if response.get('status') == 'failed':
            saved = self.root/row['id']/'agent-result.json'
            if saved.is_file():
                previous = strict_json(saved.read_text(encoding='utf-8'))
                if previous.get('failure_kind'):
                    response = approved_delivery(previous)
        # Older completed tasks can expose the missing details without another
        # model call. Revalidate their saved approved assets before enriching.
        if (response.get('status') == 'ready' and response.get('parse_strategy') in {'excel','html'}
                and 'excel_data' not in (response.get('result') or {})):
            saved = self.root/row['id']/'agent-result.json'
            if saved.is_file():
                response = approved_delivery(strict_json(saved.read_text(encoding='utf-8')))
        if response.get('status') == 'ready' and isinstance(response.get('asset'),dict):
            request = strict_json(row['payload'])
            response['asset']['filename'] = code_filename({'pageTitle':request['pageTitle']},response['parse_strategy'])
        if invalidated:
            response.update(cache_invalidated=True,resubmit_required=True)
        return {**response,'job_id':row['id'],'created_at':row['created'],'updated_at':row['updated'],
                'elapsed_seconds':round(row['updated']-row['created'] if row['state'] in TERMINAL else time.time()-row['created'],3)}

    def _loop(self):
        while not self.stop.is_set():
            with self.condition:
                row = self.db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
                if row is None:
                    self.condition.wait(.5)
                    continue
                self.db.execute("UPDATE tasks SET state='running',updated=? WHERE id=?",(time.time(),row['id']))
                self.db.commit()
                self.condition.notify_all()
            try:
                request = strict_json(row['payload'])
                folder = self.root/row['id']
                folder.mkdir(exist_ok=True)
                # 接收时已保存模板快照，不允许排队期间模板变更改变同一任务。
                frozen = folder/'request.json'
                result = self.runner(frozen, folder/'agent-result.json')
                response = approved_delivery(result)
                if response.get('status') == 'ready':
                    try:
                        if not store_success(self.root/'code-cache', result):
                            response['cache_warning'] = '本次结果已通过，但代码库文件未保存；请检查资产来源和发布记录。'
                    except (OSError, ValueError, KeyError, TypeError):
                        response['cache_warning'] = '本次结果已通过，但code目录写入失败或同名文件已被改动，未自动覆盖；本次通过代码保留在任务记录中。'
            except Exception as exc:
                # 仅保存到本机私有任务目录，HTTP不回显本地路径或异常原文。
                diagnostic = self.root/row['id']/'service-error.json'
                diagnostic.parent.mkdir(exist_ok=True)
                diagnostic.write_text(json_text({'error_type':type(exc).__name__,
                                                 'message':str(exc)[:1000]}),encoding='utf-8')
                response = {'status':'interrupted' if self.stop.is_set() else 'failed',
                            'error':{'code':'SERVICE_STOPPED' if self.stop.is_set() else 'AGENT_EXECUTION_FAILED',
                                     'message':'Agent执行或交付校验失败；查看本机任务记录，不自动重建',
                                     'requires_human_review':True}}
            with self.condition:
                self.db.execute('UPDATE tasks SET state=?,response=?,updated=? WHERE id=?',
                                (response['status'],json_text(response),time.time(),row['id']))
                self.db.commit()
                self.condition.notify_all()

    def _run_agent(self, request_path, result_path):
        flags = {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session':True}
        with (request_path.parent/'worker.log').open('wb') as log:
            self.process = subprocess.Popen([sys.executable,'-X','utf8','-m','excel_match_agent.service_worker',str(request_path),str(result_path)],
                                            cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,**flags)
            while self.process.poll() is None:
                if self.stop.wait(.1):
                    from .app import terminate_tree
                    terminate_tree(self.process)
                    break
            code = self.process.wait(timeout=10)
        self.process = None
        if code or not result_path.is_file():
            raise RuntimeError('Agent工作进程失败')
        return strict_json(result_path.read_text(encoding='utf-8'))

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=20)
        if self.thread.is_alive():
            raise RuntimeError('工作进程未能停止，未关闭任务数据库')
        self.db.close()
        self.lock_file.close()


def create_server(service, host='127.0.0.1', port=8767):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'MatchAgent/0.3'

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass  # 不把Bearer、请求体或业务路径写入HTTP访问日志。

        def send_json(self, status, value):
            raw = json_text(transport_reply(value)).encode('utf-8')
            if len(raw) > MAX_RESPONSE_BYTES:
                raise APIError(500, 'DELIVERY_TOO_LARGE', '交付结果及代码备份超过16MB，请检查任务记录')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):
                pass  # 断线不取消任务、不重建。
            self._debug_log(status, value)

        def _debug_log(self, status, value):
            # 临时诊断：只记方法/路径/状态码/错误码，绝不写请求体、令牌或业务地址。
            try:
                code = (value.get('error') or {}).get('code', '') if isinstance(value, dict) else ''
                line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.command} {self.path.split('?')[0]} -> {status} {code}\n"
                log_path = Path(__file__).parent / 'service-runtime' / 'access-debug.log'
                with open(log_path, 'a', encoding='utf-8') as fh:
                    fh.write(line)
            except Exception:
                pass

        def authorize(self):
            expected = 'Bearer '+service.config['api_token']
            if not hmac.compare_digest(self.headers.get('Authorization','').encode(),expected.encode()):
                raise APIError(401,'UNAUTHORIZED','独立服务令牌无效')

        def body(self):
            if self.headers.get('Transfer-Encoding'):
                raise APIError(400,'INVALID_BODY','不支持分块请求体')
            try:
                size = int(self.headers.get('Content-Length','0'))
            except ValueError:
                raise APIError(400,'INVALID_BODY','请求长度非法') from None
            if not 0 < size <= MAX_REQUEST_BYTES:
                raise APIError(413,'BODY_TOO_LARGE','请求为空或超过2MB')
            if self.headers.get_content_type() != 'application/json':
                raise APIError(415,'JSON_REQUIRED','Content-Type须为application/json')
            return strict_json(self.rfile.read(size).decode('utf-8-sig'))

        def dispatch(self, method):
            try:
                url = urlsplit(self.path)
                if method == 'GET' and url.path == '/health':
                    return self.send_json(200,{'status':'ok','service':'excel-match-agent','contract_version':1})
                self.authorize()
                if method == 'POST' and url.path == '/v1/match/jobs':
                    value, reused = service.submit(self.body())
                    return self.send_json(200 if reused else 202,{**value,'reused':reused})
                match = re.fullmatch(r'/v1/match/jobs/([a-f0-9-]{36})',url.path)
                if method == 'GET' and match:
                    values = parse_qs(url.query)
                    if set(values)-{'wait_seconds'} or len(values.get('wait_seconds',[]))>1:
                        raise APIError(422,'INVALID_WAIT','查询参数非法')
                    wait = float(values.get('wait_seconds',['0'])[0])
                    return self.send_json(200, service.query(match[1], wait))
                raise APIError(404,'NOT_FOUND','未找到接口')
            except APIError as exc:
                from .error_reporting import public_error
                error = public_error({'failure_kind':exc.code})
                error['message'] = str(exc)
                self.send_json(exc.status,{'status':'failed','stage':error['stage'],'error':error})
            except (ValueError,UnicodeError,TypeError,KeyError):
                self.send_json(422,{'error':{'code':'INVALID_REQUEST','message':'请求或服务器任务模板不符合契约'}})
            except Exception:
                self.send_json(500,{'error':{'code':'SERVICE_ERROR','message':'服务处理失败，未回显内部配置'}})

        def do_POST(self):
            self.dispatch('POST')

        def do_GET(self):
            self.dispatch('GET')

    server = ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('init-config','check-config','serve'))
    parser.add_argument('--config',type=Path,default=DEFAULT_CONFIG)
    args = parser.parse_args()
    if args.command == 'init-config':
        try:
            path = init_config(args.config)
        except FileExistsError:
            print('配置已存在，未覆盖令牌或其他配置')
        else:
            print('已生成独立服务配置（令牌不回显）：'+str(path))
        return
    config = read_config(args.config)
    if args.command == 'check-config':
        missing = [name for name,path in config['profiles'].items() if not Path(path).is_file()]
        print(json_text({'status':'invalid' if missing else 'ok','missing_profiles':missing,
                         'token_configured':True,'host':config.get('host','127.0.0.1'),'port':config.get('port',8767)}))
        return
    service = TaskService(config)
    try:
        server = create_server(service,config.get('host','127.0.0.1'),config.get('port',8767))
    except Exception:
        service.close()
        raise
    print(json_text({'status':'listening','host':server.server_address[0],'port':server.server_address[1]}),flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
