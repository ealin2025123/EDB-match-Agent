"""执行后的工程门禁；业务期间的推导和代码修复仍由DeepSeek完成。"""
DATE_REPAIR = ('逐条按指标自身/表头统计期间、实际源标题及真实文件名、本次title的顺序查找统计期间；'
               '仅高优先级证据缺失时使用下一项，冲突保留更具体证据。禁止用publictime或发布日期代替。'
               '根据真实证据重新生成完整代码，保留全部有效源数值；无法确定时明确说明，不能编造或删除缺日期记录。')


class OutputCheckError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def check_extraction(rows):
    if isinstance(rows, list) and not rows:
        raise OutputCheckError('EMPTY_EXTRACTION', '源文件预审未判定无业务数据，但解析结果ExcelData为空。请依据真实源文件和本轮错误核对读取范围、工作表、表头和数值定位，重新生成完整代码。')
    if not isinstance(rows, list):
        raise ValueError('解析结果必须为ExcelData数组')
    missing = [i + 1 for i, row in enumerate(rows) if isinstance(row, dict) and
               (row.get('end_date') is None or str(row.get('end_date', '')).strip().lower() in {'', 'empty'})]
    if missing:
        raise OutputCheckError('END_DATE_EMPTY', f'{len(missing)}条解析记录的end_date为空或empty（记录序号：{missing[:10]}）。' + DATE_REPAIR)
