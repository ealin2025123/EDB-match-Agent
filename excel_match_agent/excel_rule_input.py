"""Excel规则定位；仅向模型传递名称两项业务字段及路由证据。"""
from pathlib import Path
import re


def normalize_page(value):
    return "\\".join(part.strip() for part in re.split(r"[\\/]+", str(value or "").strip()))


def _text(value):
    return '' if value is None else str(value).strip()


def select_duplicates(rows, job):
    """仅使用明确的源标识或相同规则消歧；不猜选冲突模板。"""
    original = rows
    reason = ''
    if len(rows) > 1 and not job.get('rule_row'):
        md5 = _text(job.get('fileMd5')).upper()
        exact = [r for r in rows if md5 and r.get('_source_md5') == md5]
        if exact and any(any(_text(v) for v in r['fields'].values()) for r in exact):
            rows = exact
            reason = 'source_file_md5_match'
        if len(rows) > 1:
            routing = {(normalize_page(r['routing']['路径']),_text(r['routing']['数据源路径代码'])) for r in rows}
            populated = [r for r in rows if any(_text(v) for v in r['fields'].values())]
            signatures = {tuple(_text(r['fields'][k]) for k in ('指标名称示例','指标名称取值逻辑')) for r in populated}
            if len(routing) == 1 and len(signatures) == 1:
                rows = [populated[0]]
                reason = reason or 'equivalent_rules_or_empty_duplicate'
        if len(rows) == 1 and len(original) > 1:
            rows[0]['duplicate_rows'] = [r['row'] for r in original if r['row'] != rows[0]['row']]
            rows[0]['selection_reason'] = reason
    for row in original:
        row.pop('_source_md5',None)
    return rows


def read_rules(path, job, sheet_name=None, discover_header=False):
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book[sheet_name] if sheet_name else book.worksheets[0]
        # 部分导出文件的dimension只有A1，不能据此截断规则行。
        sheet.reset_dimensions()
        headers = None
        selected = []
        for number, values in enumerate(sheet.iter_rows(values_only=True), 1):
            if headers is None:
                names = [str(v).strip() if v is not None else None for v in values]
                if not discover_header or ('路径' in names and '指标名称示例' in names):
                    headers = names
                elif number >= 10:
                    raise ValueError('完整Excel规则表前10行未找到规则表头')
                continue
            data = {h: value for h, value in zip(headers, values) if h}
            if job.get('rule_row'):
                take = number == int(job['rule_row'])
            elif job.get('pageTitle'):
                take = normalize_page(data.get('路径')) == normalize_page(job['pageTitle'])
            else:
                source_name = job.get('source_name') or (Path(job['source_path']).name if job.get('source_path') else '')
                if not source_name:
                    raise ValueError('请提供pagetitle、rule_row或source_name以定位唯一excelrule行')
                take = str(data.get('文件名称', '')) == source_name
            if take:
                selected.append({'row': number, 'fields': {
                    '指标名称示例': data.get('指标名称示例'),
                    '指标名称取值逻辑': data.get('指标名称取值逻辑', data.get('指标取值逻辑'))},
                    'routing': {'路径': data.get('路径'), '数据源路径代码': data.get('数据源路径代码')},
                    '_source_md5':_text(data.get('MD5')).upper()})
        return {'path': str(path), 'sheet': sheet.title, 'rules': select_duplicates(selected,job)}
    finally:
        book.close()


def excel_rule_context(job, resolve, root):
    primary = read_rules(resolve(job['excelrule_path']), job, job.get('rule_sheet'))
    count = len(primary['rules'])
    if count == 1:
        return primary
    # 显式行号不能跨表套用；重复路径也必须先消歧。
    if count or job.get('rule_row') or not job.get('pageTitle'):
        raise ValueError(f'规则行应唯一，实际找到{count}行；请指定rule_row或pageTitle')
    fallback = (resolve(job['excelrule_fallback_path']) if job.get('excelrule_fallback_path') else
                root / 'excel_match_agent' / '完整excelrule表' / 'EDB 代码映射迁移进展.xlsx')
    if not fallback.is_file():
        raise ValueError('旧excelrule未命中，完整Excel规则表不存在；请配置excelrule_fallback_path')
    selected = read_rules(fallback, job, job.get('excelrule_fallback_sheet', 'EXcel文件'), discover_header=True)
    count = len(selected['rules'])
    if count != 1:
        raise ValueError(f'完整Excel规则表路径应唯一，实际找到{count}行；请检查pageTitle及规则表')
    selected['selection'] = 'fallback_after_primary_path_not_found'
    return selected
