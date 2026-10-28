# 复制完整文件到DC代码节点；仅配置自己的服务地址和独立服务令牌。
# 模型Key只保留在Agent服务器端，不填到这里。
import json
import base64
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SERVICE_BASE_URL = 'http://YOUR_AGENT_HOST:8767'
SERVICE_TOKEN = 'REPLACE_WITH_SERVICE_TOKEN'
NODE_WAIT_SECONDS = 100
# retry：使用DC节点异常重试；return：使用显式条件分支/后续查询节点。
PENDING_MODE = 'retry'


class InterfaceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _valid_job_id(value):
    if not isinstance(value,str) or len(value)!=36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _check_reply(reply, expected_job_id=None):
    if not isinstance(reply,dict):
        raise InterfaceError('SERVICE_RESPONSE_NOT_OBJECT','Agent应返回任务JSON对象；请核对/v1接口地址')
    if 'job_id' not in reply:
        raise InterfaceError('SERVICE_RESPONSE_JOB_ID_MISSING','收到JSON但缺少job_id；可能接到了旧接口或其他服务，不能视为任务已受理')
    if not _valid_job_id(reply['job_id']):
        raise InterfaceError('SERVICE_RESPONSE_JOB_ID_INVALID','Agent返回的job_id不是标准任务UUID')
    if expected_job_id is not None and reply['job_id'] != expected_job_id:
        raise InterfaceError('SERVICE_RESPONSE_JOB_ID_MISMATCH','查询返回的任务编号与本次job_id不一致，停止交付')
    if reply.get('status') not in {'queued','running','ready','failed','interrupted'}:
        raise InterfaceError('SERVICE_RESPONSE_STATUS_INVALID','任务状态不符合/v1/match/jobs契约')
    if any(reply.get(key) is not None and not isinstance(reply[key],dict) for key in ('asset','error','metrics')):
        raise InterfaceError('SERVICE_RESPONSE_CONTRACT_MISMATCH','asset/error/metrics字段类型不符合/v1任务契约')
    if reply['status']=='ready' and (reply.get('parse_strategy') not in {'excel','html','prompt'}
            or not isinstance(reply.get('result'),dict) or not isinstance(reply.get('asset'),dict)):
        raise InterfaceError('SERVICE_RESPONSE_CONTRACT_MISMATCH','ready响应缺少parse_strategy/result/asset；请勿混用/v3契约')
    return reply


def _failure(code,message,job_id,stage,pending=False):
    return {'status':'pending' if pending and job_id else 'failed','job_id':job_id or '',
            'error':{'code':code,'message':message,'stage':stage}}


def _network_failure(exc,job_id,stage):
    reason = exc.reason if isinstance(exc,urllib.error.URLError) else exc
    if isinstance(reason,socket.gaierror):
        code,message = 'SERVICE_DNS_FAILED','DC无法解析Agent服务域名，请检查DNS及地址'
    elif isinstance(reason,TimeoutError) or (isinstance(reason,str) and 'timed out' in reason.lower()):
        code,message = 'SERVICE_TIMEOUT','DC调用Agent时连接或读取超时；请在DC内测试/health并检查网络/防火墙，此错误尚不能归因DeepSeek'
    elif isinstance(reason,ConnectionRefusedError) or getattr(reason,'errno',None) in {61,111,10061}:
        code,message = 'SERVICE_CONNECTION_REFUSED','Agent目标端口拒绝连接，请检查服务是否启动、地址和监听端口'
    else:
        code,message = 'SERVICE_NETWORK_ERROR','DC与Agent通信失败或中断，请检查路由、端口及网络策略'
    if job_id:
        message += '；已有job_id，继续查询同一任务'
    else:
        message += '；客户端未取得任务编号，不代表服务端一定未受理，请保持本次输入/运行ID一致再重试'
    return _failure(code,message,job_id,stage,pending=True)


def _record(reply):
    asset = reply.get('asset') or {}
    error = reply.get('error') or {}
    kind = asset.get('type')
    result = reply.get('result')
    if reply.get('status') == 'ready' and isinstance(result,dict) and isinstance(asset.get('content'),str):
        result = dict(result)
        result['_asset_transfer'] = {'version':1,'job_id':str(reply.get('job_id','')),
            'parse_strategy':reply.get('parse_strategy'),'sha256':asset.get('sha256'),
            'filename':asset.get('filename'),
            'content_b64':base64.b64encode(asset['content'].encode('utf-8')).decode('ascii')}
    message = str(error.get('message',''))
    suggestion = error.get('suggestion')
    if not suggestion and reply.get('status') in {'failed','interrupted'}:
        suggestion = '按上述错误原因核对输入或运行环境；本任务不会自动恢复或重跑。'
    if suggestion:
        message += ' 处理建议：' + str(suggestion)
    return {'status':str(reply.get('status','failed')),'stage':str(error.get('stage') or reply.get('stage','match_agent')),
            'job_id':str(reply.get('job_id','')),'parse_strategy':str(reply.get('parse_strategy','')),
            'PythonFile':asset.get('filename','') if kind == 'python' else '',
            'PythonCode':asset.get('content','') if kind == 'python' else '',
            'PromptFile':asset.get('filename','') if kind == 'prompt' else '',
            'PromptCode':asset.get('content','') if kind == 'prompt' else '',
            'code_sha256':str(asset.get('sha256','')),
            'cache_hit':'true' if reply.get('cache_hit',False) else 'false',
            'warning_summary':str(reply.get('cache_warning','')),
            'result_json':json.dumps(result,ensure_ascii=False,allow_nan=False) if result is not None else '',
            'error_code':str(error.get('code','')),
            'message':message or ('asset_transfer=b64-v1' if '_asset_transfer' in (result or {}) else ''),
            'elapsed_seconds':str(reply.get('elapsed_seconds','')),
            'matched_indicator_count':str((reply.get('metrics') or {}).get('matched_indicator_count',''))}


def _http(method, path, payload=None, timeout=15):
    raw = json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8') if payload is not None else None
    request = urllib.request.Request(SERVICE_BASE_URL.rstrip('/')+path,data=raw,method=method,
                                    headers={'Authorization':'Bearer '+SERVICE_TOKEN,'Content-Type':'application/json'})
    # 不把请求或Bearer转发到重定向目的地，不依赖代理。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(request,timeout=timeout) as response:
        body = response.read(16_000_001)
        if len(body)>16_000_000:
            raise InterfaceError('SERVICE_RESPONSE_TOO_LARGE','Agent响应超过16MB限制')
        try:
            return json.loads(body.decode('utf-8-sig'))
        except (ValueError,UnicodeError):
            raise InterfaceError('SERVICE_RESPONSE_NOT_JSON','服务返回非JSON内容，可能是网关/登录页或错误地址；未回显响应正文') from None


def main(params):
    if isinstance(params,str):
        params = json.loads(params)
    if not isinstance(params,dict):
        raise ValueError('params必须为对象')
    if SERVICE_TOKEN == 'REPLACE_WITH_SERVICE_TOKEN' or 'YOUR_AGENT_HOST' in SERVICE_BASE_URL:
        raise ValueError('请先填写Agent服务基址和独立服务令牌；不要填写模型Key')
    base = urllib.parse.urlsplit(SERVICE_BASE_URL)
    if base.scheme not in {'http','https'} or not base.hostname or base.username or base.password or base.query or base.fragment or base.path not in {'','/'}:
        raise ValueError('SERVICE_BASE_URL只能填http(s)://主机:端口，不能包含API路径、认证信息或查询参数')
    started = time.monotonic()
    deadline = started+NODE_WAIT_SECONDS
    job_id = ''
    stage = 'input_validation'
    reply = {'status':'pending','job_id':''}
    try:
        previous = params.get('agent',[])
        if isinstance(previous,str):
            previous = json.loads(previous) if previous.strip() else []
        if previous is None:
            previous = []
        if not isinstance(previous,list) or (previous and (len(previous)!=1 or not isinstance(previous[0],dict))):
            raise InterfaceError('LOCAL_AGENT_FIELD_INVALID','查询入参agent应为一个对象的数组')
        job_id = params.get('job_id') or (previous[0].get('job_id') if previous else '') or ''
        if job_id and not _valid_job_id(job_id):
            raise InterfaceError('LOCAL_JOB_ID_INVALID','传入的job_id不是标准任务UUID')
        reply['job_id'] = job_id
        if not job_id:
            keys = ('pageTitle','fileMd5','markdownMD5','fileType','title','publictime','workflow_run_id','profile','rule_row')
            def clean(value):
                return None if value is None or isinstance(value,str) and not value.strip() else value
            payload = {k:clean(params[k]) for k in keys if clean(params.get(k)) is not None}
            for old,new in (('pagetitle','pageTitle'),('filemd5','fileMd5'),('markdownMd5','markdownMD5')):
                if clean(params.get(old)) is not None and new not in payload:
                    payload[new] = clean(params[old])
            payload['GZKJG'] = clean(params.get('GZKJG'))
            if payload['GZKJG'] is None:
                gzk = params.get('GZK') or {}
                if isinstance(gzk,str):
                    gzk = json.loads(gzk)
                if not isinstance(gzk,dict):
                    raise InterfaceError('LOCAL_GZK_FIELD_INVALID','GZK应为对象或其JSON字符串，或直接传GZKJG')
                payload['GZKJG'] = gzk.get('GZKJG')
            stage = 'submit_job'
            submitted = _http('POST','/v1/match/jobs',payload,timeout=min(15,max(1,deadline-time.monotonic())))
            if isinstance(submitted,dict) and _valid_job_id(submitted.get('job_id')):
                job_id = submitted['job_id']
            reply = _check_reply(submitted)
            job_id = reply['job_id']
        while reply.get('status') not in {'ready','failed','interrupted'}:
            remaining = deadline-time.monotonic()
            if remaining < 2:
                break
            wait = min(25,int(remaining-1))
            stage = 'query_job'
            reply = _check_reply(_http('GET','/v1/match/jobs/'+job_id+'?wait_seconds='+str(wait),timeout=min(remaining,wait+5)),job_id)
    except urllib.error.HTTPError as exc:
        code = 'HTTP_'+str(exc.code)
        message = {401:'Agent服务令牌无效，请核对SERVICE_TOKEN与服务端api_token',
                   404:'未找到/v1接口或任务；请核对基址、路径及job_id，不要使用/v3服务',
                   422:'Agent收到请求但字段校验失败，请核对入参映射'}.get(exc.code,'Agent接口拒绝请求，请核对鉴权、字段及服务器配置')
        try:
            error = json.loads(exc.read(8192).decode('utf-8')).get('error',{})
            # 仅透传本服务的结构化业务错误，不输出原始响应体。
            if isinstance(error.get('code'),str) and error['code'].replace('_','').isalnum():
                code = error['code'][:100]
                if isinstance(error.get('message'),str):
                    message = error['message'][:2000]
                if isinstance(error.get('suggestion'),str):
                    message += ' 处理建议：' + error['suggestion'][:1000]
                if isinstance(error.get('stage'),str):
                    stage = error['stage']
        except (ValueError,AttributeError,UnicodeError):
            pass
        reply = _failure(code,message,job_id,stage)
    except InterfaceError as exc:
        reply = _failure(exc.code,str(exc),job_id,stage)
    except (urllib.error.URLError,OSError) as exc:
        reply = _network_failure(exc,job_id,stage)
    except (ValueError,KeyError,TypeError,AttributeError):
        reply = _failure('LOCAL_INPUT_OR_RESPONSE_INVALID','输入字段类型或接口响应不符合契约；请检查本阶段映射，未回显业务正文或认证信息',job_id,stage)
    if 'elapsed_seconds' not in reply:
        reply['elapsed_seconds'] = round(time.monotonic()-started,3)
    if reply.get('status') not in {'ready','failed','interrupted'} and PENDING_MODE == 'retry':
        # 重试重新POST时由服务幂等复用任务；平台必须保持本次输入/运行ID不变。
        detail = _record(reply)
        raise RuntimeError('DC_AGENT_RETRY:继续查询同一任务，job_id='+str(job_id or '')+'；status='+detail['status']+'；stage='+detail['stage']+'；error_code='+detail['error_code']+'；message='+detail['message']+'；处理建议=继续查询同一job_id，保持输入不变；排队或运行状态不代表业务失败')
    return {'agent':[_record(reply)]}
