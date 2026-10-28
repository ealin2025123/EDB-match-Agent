# 临时替换DC接入代码做网络诊断；不提交业务任务，不需要令牌。
# 正常return agent数组，元素只含status、stage、error_code、message，兼容现有DC父变量映射。
# 仅供临时排查；不要作为正式业务接入代码。
import json
import socket
import time
import urllib.error
import urllib.request

AGENT_HOST = 'YOUR_AGENT_HOST'
AGENT_PORT = 8767


def main(params):
    started = time.monotonic()
    stage = 'tcp_connect'
    code = 'HEALTH_PROBE_OK'
    message = ''
    try:
        with socket.create_connection((AGENT_HOST,AGENT_PORT),timeout=10) as connection:
            local_ip = connection.getsockname()[0]
        stage = 'http_health'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://'+AGENT_HOST+':'+str(AGENT_PORT)+'/health',timeout=10) as response:
            try:
                health = json.loads(response.read(8192).decode('utf-8'))
            except (ValueError,UnicodeError):
                raise ValueError('health_not_json') from None
            if response.status != 200 or not isinstance(health,dict) or health.get('service') != 'excel-match-agent' or health.get('status') != 'ok':
                raise ValueError('unexpected_health_response')
        message = 'TCP及HTTP健康检查通过；容器本地源IP='+local_ip+'（未必是NAT后的出站IP）。'
    except Exception as exc:
        reason = exc.reason if isinstance(exc,urllib.error.URLError) else exc
        if isinstance(exc,urllib.error.HTTPError):
            code = 'HEALTH_HTTP_'+str(exc.code)
            message = '已连接到HTTP服务，但健康接口返回此状态；核对目标服务、路径及网关。'
        elif isinstance(reason,TimeoutError):
            code = 'TCP_CONNECT_TIMEOUT' if stage=='tcp_connect' else 'HTTP_HEALTH_TIMEOUT'
            message = 'DC到Agent的TCP连接超时，检查路由/ACL/防火墙。' if stage=='tcp_connect' else 'TCP已连通，但健康接口读取超时。'
        elif isinstance(reason,ConnectionRefusedError):
            code = 'CONNECTION_REFUSED'
            message = '连接被拒绝，检查服务是否启动及目标IP/监听端口。'
        elif isinstance(reason,socket.gaierror):
            code = 'DNS_FAILED'
            message = 'DC无法解析目标服务域名。'
        elif isinstance(exc,ValueError):
            code = 'HEALTH_RESPONSE_INVALID'
            message = '收到响应，但不是预期的Agent健康JSON，核对是否为网关页或其他服务。'
        else:
            code = 'PROBE_FAILED_'+type(reason).__name__
            message = '网络探测失败；请结合执行时间检查部署机防火墙日志。'
    elapsed = round(time.monotonic()-started,3)
    return {'agent':[{'status':'success' if code=='HEALTH_PROBE_OK' else 'failed',
            'stage':stage,'error_code':'' if code=='HEALTH_PROBE_OK' else code,
            'message':message+' 耗时'+str(elapsed)+'秒；目标'+AGENT_HOST+':'+str(AGENT_PORT)+'；未提交业务任务。'}]}
