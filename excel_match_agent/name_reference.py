"""读取已存在的完整拼接名称和规则行证据；业务名称判断交给DeepSeek。"""
from pathlib import Path
import re

DEFAULT_WORKBOOK_NAME = "指标名称拼接逻辑_分路径代码.xlsx"


def attach_name_reference(business, job, resolve, root):
    """按excelrule路径代码读取拼接表；显式context_files可覆盖默认表。"""
    for item in business:
        if "rows" in item and (item.get("role") == "name_reference" or Path(item["path"]).name == DEFAULT_WORKBOOK_NAME):
            item.update(role="name_reference", final_name_column=item.get("final_name_column", "K"))
    if any(item.get("role") == "name_reference" for item in business):
        return
    rule = next((item["rules"][0].get("routing", item["rules"][0]["fields"]) for item in business if item.get("rules")), {})
    code = rule.get("数据源路径代码")
    path = resolve(job["name_reference_path"]) if job.get("name_reference_path") else root / "excelname拼接规则" / DEFAULT_WORKBOOK_NAME
    if code is None or not path.exists():
        return
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = str(code).strip()
        if sheet not in book.sheetnames:
            business.append({"path": str(path), "sheet": sheet, "role": "name_reference_missing",
                             "reason": "拼接表没有本次路径代码工作表，不使用其他路径的名称代替"})
            return
        business.append({"path": str(path), "sheet": sheet, "role": "name_reference",
                         "final_name_column": job.get("final_name_column", "K"), "rows": list(book[sheet].values)})
    finally:
        book.close()


def comparison_rules(business, gzk):
    """K列为现有拼接库的完整名列；其他布局通过final_name_column显式指定。"""
    reference = {}
    gzk_names = {r["rule_indicator_name"].strip(): r for r in gzk if isinstance(r.get("rule_indicator_name"), str)}
    for table in business:
        if table.get("role") != "name_reference":
            continue
        column = table["final_name_column"]
        if not isinstance(column, str) or not re.fullmatch(r"[A-Z]+", column):
            raise ValueError("final_name_column须为完整名称所在的Excel列字母，例如K")
        number = 0
        for letter in column:
            number = number * 26 + ord(letter) - 64
        for index, row in enumerate(table["rows"], 1):
            value = row[number - 1] if len(row) >= number else None
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                raise ValueError("拼接表完整指标名称必须为字符串")
            name = value.strip()
            if not name or index == 1 and name in {"完整指标名称", "最终指标名称", "指标名称", "拼接后指标名称"}:
                continue
            evidence = {"path": table["path"], "sheet": table["sheet"], "cell": f"{column}{index}",
                        "components": list(row[:number - 1])}
            if name not in reference:
                # 编码和单位/频率仅为辅助信息；完整名称取自拼接表，不从GZK替换。
                reference[name] = {**gzk_names.get(name, {}), "rule_indicator_name": name, "name_reference": []}
            reference[name]["name_reference"].append(evidence)
    return list(reference.values())
