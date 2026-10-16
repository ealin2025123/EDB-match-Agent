"""与用户提供的表格数据匹配节点输出格式一致。"""
import json
import math

FIELDS = ("end_date", "indicator_data", "SJPLLB", "indicator_code", "publictime", "indicator_unit",
          "convert_ratio", "indicator_name", "unit_ratio", "KB_unit", "remark", "PLPL")


def workflow_value(value):
    """按工作流样本保留整数/原字符串，小数使用字符串，避免全部强转字符串。"""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("indicator_data须为数字或字符串")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("indicator_data不能为NaN或Infinity")
        return str(value)
    return value


def workflow_output(data):
    rows = []
    for row in data["OUTDATA"]:
        if any(field not in row for field in FIELDS):
            raise ValueError("OUTDATA缺少节点输出字段")
        rows.append({field: workflow_value(row[field]) if field == "indicator_data"
                     else "" if row[field] is None else str(row[field]) for field in FIELDS})
    units = {key: list(data["unitArray"][key]) for key in ("indicator_unit", "KB_unit")}
    return {"unitArray": units, "codeOutputResult": {"unitArray": units, "OUTDATA": rows}, "OUTDATA": rows}


def export_output(folder, data):
    output = workflow_output(data)
    encode = lambda value: json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    (folder / "node-output.json").write_text(encode(output), encoding="utf-8")
    (folder / "node-output.txt").write_text(
        "输出:\nunitArray：" + encode(output["unitArray"]) +
        "\ncodeOutputResult：" + encode(output["codeOutputResult"]) +
        "\nOUTDATA：" + encode(output["OUTDATA"]) + "\n", encoding="utf-8")
    return output
