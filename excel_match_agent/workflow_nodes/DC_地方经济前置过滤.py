# 放在开始节点之后、规则查询和Agent接入之前。
# 条件节点仅在continue_workflow为true时进入原工作流，false直接结束。
import json

LOCAL_ECONOMY_TABLE = 'usrDFJJYXSJB'


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def main(params):
    params = _object(params)
    candidates = []
    invalid = False

    def collect(container):
        nonlocal invalid
        if 'data_target_table' not in container:
            return
        value = container['data_target_table']
        if isinstance(value, dict):
            value = value.get('value')
        if value is None or isinstance(value, str) and not value.strip():
            return
        if not isinstance(value, str):
            invalid = True
            return
        candidates.append(value.strip())

    # 兼容DC直接映射的字段、fields包装及原始开始事件。
    collect(params)
    collect(_object(params.get('fields')))
    source = _object(params.get('sourceDataInfo'))
    collect(_object(source.get('fields')))
    tables = set(candidates)
    table = next(iter(tables)) if len(tables) == 1 else ''
    if invalid:
        reason = '目标表字段类型无效，跳过；请检查开始节点字段映射。'
    elif len(tables) > 1:
        reason = '输入中的目标表字段互相冲突，跳过；请检查开始节点字段映射。'
    elif not table:
        reason = '缺少目标表，跳过；请传入data_target_table，不根据标签或标题猜测。'
    elif table != LOCAL_ECONOMY_TABLE:
        reason = '目标表不是地方经济表，跳过本工作流。'
    else:
        reason = '目标表为地方经济表，继续原工作流。'
    allowed = not invalid and len(tables) == 1 and table == LOCAL_ECONOMY_TABLE
    return {'continue_workflow':allowed, 'skip_workflow':not allowed,
            'data_target_table':table, 'filter_status':'passed' if allowed else 'skipped',
            'filter_reason':reason}
