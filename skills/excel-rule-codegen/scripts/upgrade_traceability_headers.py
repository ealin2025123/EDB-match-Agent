#!/usr/bin/env python3
"""Upgrade abbreviated parser docstrings from their recorded excelrule row."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from openpyxl import load_workbook


ROW_PATTERN = re.compile(
    r'(?:excelrule第\s*(\d+)\s*行|excelrule行号[：:]\s*(\d+))'
)
TOP_DOCSTRING_PATTERN = re.compile(
    r'\A(#\s*-\*-\s*coding:\s*utf-8\s*-\*-\r?\n)r""".*?"""',
    re.DOTALL,
)


def field(values, index):
    value = values[index] if index < len(values) else None
    return "" if value is None else str(value).strip()


def build_header(row_number, values, global_rule):
    source_file = field(values, 6)
    lines = [
        'r"""',
        '采集数据源：' + field(values, 1),
        '数据来源编码：' + field(values, 0),
        '数据源路径代码：' + field(values, 2),
        '路径：' + field(values, 4),
        '老采集站点名称：' + field(values, 5),
        '文件名称：' + source_file,
        '目标表：' + field(values, 13),
        'excelrule行号：' + str(row_number),
        '源文件：D:\\excel\\org\\' + source_file,
        '指标名称示例：' + field(values, 3),
        '',
        '拆解规则：',
        '1. 指标名称取值逻辑：' + field(values, 7),
        '2. 指标名称标准化处理：' + field(values, 8),
        '3. 指标单位取值逻辑：' + field(values, 9),
        '4. 指标单位位置：' + field(values, 10),
        '5. 截止日期取值逻辑：' + field(values, 11),
        '6. 截止日期位置：' + field(values, 12),
        '7. 备注/Sheet限制：' + field(values, 16),
        '8. 全局规范：' + global_rule,
        '"""',
    ]
    return "\n".join(lines)


def upgrade(rule_workbook, parser_dir, sheet_name=None):
    workbook = load_workbook(rule_workbook, read_only=True, data_only=True)
    sheet = workbook[sheet_name] if sheet_name else workbook.worksheets[0]
    headers = [sheet.cell(1, column).value for column in range(1, sheet.max_column + 1)]
    header_global_rule = field(headers, 19)
    changed = []

    for parser_path in sorted(Path(parser_dir).glob('*.py')):
        source = parser_path.read_text(encoding='utf-8')
        row_match = ROW_PATTERN.search(source[:1000])
        if not row_match:
            continue
        row_number = int(row_match.group(1) or row_match.group(2))
        values = [
            sheet.cell(row_number, column).value
            for column in range(1, sheet.max_column + 1)
        ]
        global_rule = field(values, 19) or header_global_rule
        header = build_header(row_number, values, global_rule)
        updated, count = TOP_DOCSTRING_PATTERN.subn(
            lambda match: match.group(1) + header,
            source,
            count=1,
        )
        if count != 1:
            raise ValueError('无法定位顶部模块说明：' + str(parser_path))
        if updated != source:
            with parser_path.open('w', encoding='utf-8', newline='\n') as handle:
                handle.write(updated)
            changed.append(parser_path)

    workbook.close()
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('rule_workbook', type=Path)
    parser.add_argument('parser_dir', type=Path)
    parser.add_argument('--sheet')
    args = parser.parse_args()
    changed = upgrade(args.rule_workbook, args.parser_dir, args.sheet)
    print('updated=' + str(len(changed)))


if __name__ == '__main__':
    main()
