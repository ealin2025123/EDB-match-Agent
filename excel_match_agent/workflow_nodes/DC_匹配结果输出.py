# 接在Agent接入/查询节点后，仅ready分支执行；不再次执行解析代码或转换单位。
import hashlib
import json
import copy
import base64
import binascii


def main(params):
    if isinstance(params,str):
        params = json.loads(params)
    value = params.get('agent')
    if isinstance(value,str):
        value = json.loads(value)
    if not isinstance(value,list) or len(value)!=1 or not isinstance(value[0],dict):
        raise ValueError('agent须为恰好一个对象的数组')
    item = value[0]
    if item.get('status') != 'ready':
        detail = {key:item.get(key, '') for key in ('status','stage','job_id','error_code','message')}
        detail['suggestion'] = ('继续查询同一job_id，不要重复创建任务' if item.get('status') in {'pending','queued','running'}
                                else '按message中的具体原因处理；已失败或中断的任务不会自动重跑')
        raise ValueError('Agent未通过：' + json.dumps(detail, ensure_ascii=False))
    strategy = item.get('parse_strategy')
    if strategy not in {'excel','html','prompt'}:
        raise ValueError('解析分支非法')
    raw_output = item.get('result_json')
    if isinstance(raw_output,str):
        output = json.loads(raw_output)
    elif isinstance(raw_output,dict):
        output = copy.deepcopy(raw_output)
    else:
        raise ValueError('result_json须为JSON对象或其JSON字符串')
    if not isinstance(output,dict):
        raise ValueError('result_json须为JSON对象或其JSON字符串')
    transfer = output.pop('_asset_transfer',None)
    if transfer is not None and (not isinstance(transfer,dict) or transfer.get('version') != 1
            or transfer.get('job_id') != item.get('job_id')
            or transfer.get('parse_strategy') != strategy
            or transfer.get('sha256') != item.get('code_sha256')):
        raise ValueError('代码传输备份与当前任务/分支/哈希不一致')
    code = item.get('PromptCode') if strategy == 'prompt' else item.get('PythonCode')
    if code in (None,'') and transfer is not None:
        try:
            code = base64.b64decode(transfer['content_b64'],validate=True).decode('utf-8')
        except (KeyError,TypeError,ValueError,UnicodeError,binascii.Error):
            raise ValueError('代码传输备份无效，未跳过哈希校验') from None
    code_field = 'PromptCode' if strategy == 'prompt' else 'PythonCode'
    expected_hash = item.get('code_sha256')
    actual_hash = hashlib.sha256(code.encode('utf-8')).hexdigest() if isinstance(code,str) else ''
    if not isinstance(code,str) or not code or actual_hash != expected_hash:
        # Include only metadata, never the code body. This is visible even on
        # DC installations where intermediate node output cannot be inspected.
        details = {'job_id':item.get('job_id'),'parse_strategy':strategy,
                   'asset_backup_present':transfer is not None,
                   'input_message':str(item.get('message',''))[:100],
                   'code_field':code_field,'code_type':type(code).__name__,
                   'code_chars':len(code) if isinstance(code,str) else None,
                   'expected_sha256':str(expected_hash)[:64], 'actual_sha256':actual_hash,
                   'crlf_count':code.count('\r\n') if isinstance(code,str) else None}
        raise ValueError('交付代码/提示词哈希校验失败：'+json.dumps(details,ensure_ascii=False))
    expected = {'OUTDATA'} if strategy == 'prompt' else {'unitArray','codeOutputResult','OUTDATA'}
    optional = {'excel_data'} if strategy in {'excel','html'} else set()
    if not isinstance(output,dict) or not expected <= set(output) or set(output)-expected-optional or not isinstance(output['OUTDATA'],list):
        raise ValueError('匹配结果与分支JSON契约不符')
    if 'excel_data' in output and (not isinstance(output['excel_data'],list) or any(
            not isinstance(row,dict) or not {'end_date','indicator_data','indicator_unit','indicator_name'} <= set(row)
            for row in output['excel_data'])):
        raise ValueError('excel_data解析明细格式不符')
    if len({str(row.get('indicator_code','')) for row in output['OUTDATA'] if row.get('indicator_code') not in (None,'','empty')})<2:
        raise ValueError('有效匹配不足两个不同编码')
    if strategy != 'prompt' and output['codeOutputResult'] != {'unitArray':output['unitArray'],'OUTDATA':output['OUTDATA']}:
        raise ValueError('表格分支内外匹配结果不一致')
    # 仅DC原MD后处理链需要此适配；Agent/API的Prompt结果仍只有OUTDATA。
    chain = params.get('platform_chain','core')
    if chain not in {'core','md_array','table_audit'}:
        raise ValueError('platform_chain仅支持core、md_array或table_audit')
    if chain == 'table_audit':
        if strategy not in {'excel','html'}:
            raise ValueError('table_audit只用于Excel/HTML接原单位清洗及内审链')
        rules = params.get('GZKJG')
        if rules is None:
            gzk = params.get('GZK') or {}
            if isinstance(gzk,str):
                gzk = json.loads(gzk)
            rules = gzk.get('GZKJG')
        if isinstance(rules,str):
            rules = json.loads(rules)
        if not isinstance(rules,list) or not rules:
            raise ValueError('内审频率适配需要本次GZKJG')
        index = {}
        for rule in rules:
            key = str(rule['rule_indicator_code'])
            frequency = rule.get('indicator_release_frequency')
            if not isinstance(frequency,str) or not frequency.strip():
                raise ValueError('GZK披露频率缺失，不猜测内审频率')
            if key in index and index[key] != frequency:
                raise ValueError('同一指标编码存在不同披露频率')
            index[key] = frequency
        for row in output['OUTDATA']:
            if str(row['indicator_code']) not in index:
                raise ValueError('匹配结果编码不在本次GZK中')
            row['indicator_release_frequency'] = index[str(row['indicator_code'])]
        output['codeOutputResult']['OUTDATA'] = output['OUTDATA']
    if chain == 'md_array':
        if strategy != 'prompt':
            raise ValueError('md_array只用于Prompt接回原MD单位数组清洗链')
        units = {'indicator_unit':[],'KB_unit':[],'convert_ratio':[]}
        seen = set()
        for row in output['OUTDATA']:
            source, target = row.get('indicator_unit'), row.get('KB_unit')
            if not isinstance(source,str) or not isinstance(target,str) or not source or not target:
                raise ValueError('MD单位数组适配缺少行级indicator_unit/KB_unit')
            pair = (source,target)
            if pair not in seen:
                seen.add(pair)
                units['indicator_unit'].append(source);units['KB_unit'].append(target)
                units['convert_ratio'].append(None)
        output['unitArray'] = units
    result = dict(output)
    result['python_code'] = code
    result['python_file'] = (item.get('PythonFile') if strategy != 'prompt' else item.get('PromptFile')) or (transfer or {}).get('filename','')
    return result
