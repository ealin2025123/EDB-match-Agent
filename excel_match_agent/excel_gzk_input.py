"""Excel规则库文件读取：兼容原JSON及平台规则库查询转输出TXT。"""
import json
from pathlib import Path
import re


def load_excel_gzk_file(path):
    text = Path(path).read_text(encoding='utf-8-sig')
    try:
        # 保留既有纯JSON数组、GZKJG对象及JSON字符串的行为。
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    records = []
    for marker in re.finditer(r'(?m)^[ \t]*(codeOutputResult|GZKJG)[：:][ \t]*', text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[marker.end():].lstrip())
            if isinstance(value, dict):
                if 'GZKJG' not in value:
                    raise ValueError('codeOutputResult缺少GZKJG')
                value = value['GZKJG']
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, list):
                raise ValueError('GZKJG须为数组')
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError('Excel gzk_file平台TXT字段解析失败：' + marker[1] + '；' + str(exc)) from None
        records.append(value)
    if not records:
        raise ValueError('Excel gzk_file不是有效JSON，也未找到平台TXT中的codeOutputResult/GZKJG输出字段')
    if any(value != records[0] for value in records[1:]):
        raise ValueError('Excel gzk_file平台TXT中的重复GZKJG输出内容不一致，请检查文件')
    # 只读取数据字段；不执行scriptCode或其他文本，也不拼接重复展示的列表。
    return records[0]
