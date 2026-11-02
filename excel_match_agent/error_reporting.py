"""对外错误说明白名单；不回传模型认证、运行配置或本机异常路径。"""
ERRORS = {
    'LOCAL_ASSET_AMBIGUOUS': ('同一路径找到多个内容不同的已有解析资产。', '请明确保留哪个版本；本次不会调用Agent生成或重建。', 'existing_asset_lookup'),
    'LOCAL_ASSET_EXECUTION_FAILED': ('已有解析资产执行或匹配未通过。', '检查已有资产与当前源文件、GZK是否兼容；查看本机existing-error.json，本次未生成或重建。', 'execute_existing_asset'),
    'GZKJG_EMPTY': ('指标规则为空或无效：GZKJG为空。', '检查规则库查询与GZKJG字段映射后再发起新任务。', 'input_validation'),
    'GZKJG_INVALID': ('指标规则为空或无效：缺少有效指标名称或编码。', '修正规则库中的名称、编码及字段结构。', 'input_validation'),
    'INVALID_GZK': ('指标规则为空或无效：GZKJG格式不符合契约。', '检查GZKJG数组及名称、编码字段。', 'input_validation'),
    'RULE_FIELDS_INVALID': ('指标规则为空或无效：规则编码或字段缺失。', '补齐规则名称与编码后提交新任务。', 'input_validation'),
    'RULE_AMBIGUITY': ('指标规则存在重名或冲突。', '明确同名指标的编码及规则范围。', 'input_validation'),
    'SOURCE_NO_VALID_DATA': ('实际源文件没有有效业务数据。', '核对源文件是否只有导航、标题、表头或空值。', 'preflight'),
    'SOURCE_GZK_NO_RELEVANT_DATA': ('实际源数据与本次指标规则完全不相关。', '核对源标识、pageTitle和本次GZK是否对应。', 'preflight'),
    'PREFLIGHT_PROTOCOL_FAILED': ('DeepSeek内容预审的格式或证据核验失败，未判定源文件无数据。', '核对预审回复及引用原文；不要将此错误解释为源文件无数据。', 'preflight'),
    'EMPTY_EXTRACTION': ('依据真实源文件修复后，ExcelData仍为空。', '检查工作表、表头及取值范围；本任务已停止，不会自动重跑。', 'extraction_validation'),
    'END_DATE_EMPTY': ('解析记录的统计截止日期仍无法确定，禁止空日期通过。', '核对指标表头、真实源标题和文件名、本次title；不得使用publictime代替。', 'date_validation'),
    'SOURCE_DOWNLOAD_FAILED': ('真实源文件下载失败。', '核对源标识、下载服务和网络权限。', 'download'),
    'SOURCE_DOWNLOAD_INVALID': ('下载内容不是有效的预期源文件。', '核对fileMd5对应内容及实际文件类型。', 'download'),
    'NAME_REVIEW_PROTOCOL_ERROR': ('DeepSeek名称审核回复未通过字段或证据契约核验。', '核对具体审核记录及目标名称依据，不能把协议错误当作匹配通过。', 'name_review'),
    'NAME_REVIEW_UNCERTAIN': ('DeepSeek无法确定部分指标的名称或业务口径。', '核对源表标题、父级及GZK口径。', 'name_review'),
    'NAME_CONSTRUCTION_ERROR': ('名称构造经修复仍未通过审核。', '核对名称拼接规则和GZK对应关系。', 'name_review'),
    'INSUFFICIENT_MATCHES': ('修复后仍不足两个不同指标编码匹配。', '核对源范围、GZK及名称规则，不得强配不同口径。', 'match'),
    'MODEL_BACKEND_UNAVAILABLE': ('DeepSeek连接或认证不可用。', '检查服务端模型连接及认证配置；不要在DC粘贴密钥。', 'model'),
    'MODEL_OUTPUT_TRUNCATED': ('DeepSeek回复被截断。', '核对模型回复长度和生成日志。', 'model'),
    'MODEL_CONTEXT_TOO_LARGE': ('模型输入上下文超过限制。', '核对源范围及上下文大小。', 'model'),
    'EXECUTION_BACKEND_UNAVAILABLE': ('本地代码执行环境不可用。', '检查服务Python及运行依赖。', 'execute'),
    'PLATFORM_EXPORT_FAILED': ('平台完整代码导出或真实下载验证未通过。', '检查平台验证记录和下载环境。', 'platform_export'),
    'INPUT_OR_RUNTIME_ERROR': ('任务输入或本地运行环境发生错误。', '检查规则定位、文件读取和Python依赖；原始诊断保留在本机任务记录。', 'input'),
    'ATTEMPTS_EXHAUSTED': ('初次生成加最多两次重建仍未通过。', '检查最后一次执行或审核失败原因。', 'agent'),
    'TOTAL_TIMEOUT': ('Agent整体运行预算耗尽。', '核对耗时阶段及预算设置；本任务不会自动重跑。', 'agent'),
    'USER_CANCELLED': ('用户已停止此任务。', '查询用户指定保留的任务，不要继续等待本任务恢复。', 'queue'),
    'MARKDOWN_MD5_MISSING': ('缺少实际Markdown标识。', '补充markdownMD5，不能用fileMd5替代。', 'acquire_markdown'),
    'MARKDOWN_EMPTY': ('下载的实际Markdown内容为空。', '核对markdownMD5对应文件及转换结果。', 'acquire_markdown'),
    'MARKDOWN_INVALID_CONTENT': ('下载内容不是有效Markdown正文。', '核对是否下载成网页错误页、PDF或其他二进制文件。', 'acquire_markdown'),
    'MARKDOWN_DOWNLOAD_FAILED': ('实际Markdown下载失败。', '核对markdownMD5、下载服务及网络权限。', 'acquire_markdown'),
    'BUSINESS_CONTEXT_REQUIRED': ('DeepSeek确认缺少必要业务依据，已停止生成。', '按具体原因补齐源数据或规则证据，不能编造。', 'generate'),
    'BUSINESS_AMBIGUITY': ('DeepSeek确认存在业务口径或证据歧义。', '核对源文和本次规则中的具体歧义。', 'review'),
    'MODEL_PROTOCOL_FAILED': ('DeepSeek回复未通过格式或证据协议核验。', '核对回复字段及真实源证据。', 'model'),
    'INVALID_INPUT': ('任务输入不符合当前解析分支契约。', '按具体原因核对字段映射及源标识。', 'input_validation'),
    'RULE_BLOCKED': ('规则行已明确标记本任务无法生成。', '核对规则备注中的缺失条件及源文件配置。', 'rule_lookup'),
    'MAX_REBUILDS_EXHAUSTED': ('初次生成加最多两次重建仍未通过。', '核对最后一次执行、名称及证据审核结果。', 'agent'),
}


def public_error(result):
    code = result.get('failure_kind') or 'AGENT_NOT_APPROVED'
    message, suggestion, default_stage = ERRORS.get(code, (f'Agent处理未通过，错误码：{code}。', '核对本任务失败阶段及对应输入，不自动重跑历史任务。', 'agent'))
    if result.get('public_reason'):
        message += ' ' + result['public_reason']
    if code == 'INVALID_INPUT':
        detail = next((n.get('message','') for n in reversed(result.get('notifications',[])) if n.get('code') == code),'')
        if isinstance(detail,str) and detail.startswith(('正则任务缺少数据源路径代码','excelrule 需精确定位唯一规则行',
                '工作簿规则行的路径与开始节点pageTitle不一致','无可用正则','无正则拼接规则')):
            message += ' ' + detail[:1200]
            suggestion = '核对同一路径规则及精确文本路径代码；不要从已丢精度的Excel数值猜测编码，也不要改用其他路径正则。'
    # 已经由协议核验过的业务理由可以直接显示；任意Python异常只留本机。
    if code in {'SOURCE_NO_VALID_DATA', 'SOURCE_GZK_NO_RELEVANT_DATA', 'PREFLIGHT_PROTOCOL_FAILED',
                'EMPTY_EXTRACTION', 'END_DATE_EMPTY', 'GZKJG_EMPTY', 'GZKJG_INVALID',
                'NAME_REVIEW_PROTOCOL_ERROR', 'NAME_REVIEW_UNCERTAIN', 'NAME_CONSTRUCTION_ERROR',
                'BUSINESS_CONTEXT_REQUIRED', 'BUSINESS_AMBIGUITY', 'MODEL_PROTOCOL_FAILED', 'RULE_BLOCKED'}:
        detail = next((n.get('message', '') for n in reversed(result.get('notifications', [])) if n.get('code') == code), '')
        if detail and not any(x in detail.lower() for x in ('bearer ', 'api_key', 'api key', ':\\', 'http://', 'https://', 'sk-')):
            message += ' ' + detail[:1200]
    return {'code': code, 'message': message, 'suggestion': suggestion,
            'stage': result.get('failure_stage') or default_stage, 'requires_human_review': True}
