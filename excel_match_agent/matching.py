"""Excel agent 的本地评估/重试原型；不执行外部生成的 Python。"""
from __future__ import annotations

import argparse
import calendar
import copy
import json
import math
import re
from pathlib import Path


class NeedsReviewError(Exception):
    """生成器确认业务规则缺失/歧义时抛出，禁止靠重写名称继续尝试。"""


def text(value):
    return "" if value is None else str(value).strip()


def normalize_rules(rules):
    """兼容上游原字段和既有 rule_* 字段；不修改名称内容、不推断缺失字段。"""
    if isinstance(rules, str):
        try:
            rules = json.loads(rules)
        except json.JSONDecodeError as exc:
            raise ValueError("GZKJG 不是有效 JSON") from exc
    if isinstance(rules, dict) and "GZKJG" in rules:
        rules = rules["GZKJG"]
        if isinstance(rules, str):
            try:
                rules = json.loads(rules)
            except json.JSONDecodeError as exc:
                raise ValueError("GZKJG 不是有效 JSON") from exc
    if not isinstance(rules, list):
        raise ValueError("GZKJG 必须是列表")
    aliases = {"indicator_code": "rule_indicator_code", "indicator_name": "rule_indicator_name",
               "constant_name": "rule_constant_name"}
    normalized = []
    for row in rules:
        if not isinstance(row, dict):
            raise ValueError("GZKJG 每条必须是对象")
        result = copy.deepcopy(row)
        for source, target in aliases.items():
            if source in row and target in row and text(row[source]) != text(row[target]):
                raise ValueError(f"GZKJG 字段冲突：{source}/{target}")
            if source in row and target not in row:
                result[target] = row[source]
        code = result.get("rule_indicator_code")
        if code is not None and (isinstance(code, bool) or not isinstance(code, (str, int))):
            raise ValueError("rule_indicator_code 必须是字符串或整数，不能用浮点数或布尔值")
        if "rule_indicator_code" in result:
            result["rule_indicator_code"] = text(code)
        normalized.append(result)
    return normalized


def valid_date(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text(value)):
        return False
    year, month, day = map(int, value.split("-"))
    return 1 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]


def evaluate(rows, rules, publictime="", expected_keys=None, threshold=0.8, matching_only=False):
    """名称仅 strip 后精确匹配。expected_keys 是人工/规则确认的本报表预期键。"""
    if not isinstance(rows, list):
        raise ValueError("ExcelData 必须是列表")
    rules = normalize_rules(rules)
    if not 0 <= threshold <= 1:
        raise ValueError("threshold 必须在 0 到 1 之间")
    if expected_keys is not None and not expected_keys:
        raise ValueError("expected_keys 不能是空列表；未知时请传 None")
    index, conflicts = {}, []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("GZKJG 每条必须是对象")
        name = text(rule.get("rule_indicator_name"))
        if not name:
            continue
        if name in index:
            conflicts.append(name)
        else:
            index[name] = rule
    output, unmatched, errors, keys, unit_differences, rule_field_errors = [], [], [], set(), [], []
    matched = 0
    for number, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append({"row": number, "reason": "记录不是对象"})
            continue
        name = text(row.get("indicator_name"))
        value = row.get("indicator_data")
        unit, end = text(row.get("indicator_unit")), text(row.get("end_date"))
        key = (name, end)
        problems = []
        if not name or not unit or not valid_date(end):
            problems.append("名称/单位缺失或 end_date 非有效 YYYY-MM-DD")
        if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)) or not text(value):
            problems.append("数值缺失或类型非法")
        else:
            try:
                if not math.isfinite(float(value)):
                    problems.append("数值不是有限数")
            except (TypeError, ValueError, OverflowError):
                problems.append("数值不可解析；缺失符号应单独报告")
        if key in keys:
            problems.append("重复的名称与期间，需要检查维度/表头")
        keys.add(key)
        source = row.get("source")
        if not isinstance(source, dict) or not text(source.get("sheet")) or not text(source.get("cell")):
            problems.append("缺少来源 sheet/cell")
        if problems:
            errors.append({"row": number, "name": name, "reasons": problems})
        rule = index.get(name)
        if rule is None:
            unmatched.append({"row": number, "indicator_name": name, "source": source})
            continue
        matched += 1
        code = text(rule.get("rule_indicator_code"))
        if not code or (not matching_only and not text(rule.get("rule_constant_name"))):
            errors.append({"row": number, "reason": "规则编码或标准单位缺失"})
            rule_field_errors.append(number)
            continue
        if problems and not matching_only:
            continue
        if not matching_only and unit != text(rule.get("rule_constant_name")):
            unit_differences.append({"indicator_name": name, "indicator_code": code,
                                     "indicator_unit": unit, "KB_unit": text(rule.get("rule_constant_name")),
                                     "source": source, "conversion_applied": False})
        output.append({
            "indicator_code": code, "indicator_name": name,
            "indicator_data": value, "indicator_unit": unit,
            "KB_unit": text(rule.get("rule_constant_name")),
            "convert_ratio": text(rule.get("convert_ratio")),
            "unit_ratio": text(rule.get("rule_unit_ratio")),
            "publictime": publictime, "end_date": end,
            "remark": row.get("remark", ""), "PLPL": rule.get("PLPL", ""), "SJPLLB": "",
        })
    expected = {tuple(k) for k in expected_keys} if expected_keys is not None else None
    missing = sorted(expected - keys) if expected is not None else []
    coverage = len(expected & keys) / len(expected) if expected is not None else None
    rate = matched / len(rows) if rows else 0.0
    output_count = len(output)
    output_rate = output_count / len(rows) if rows else 0.0
    # 有来源只是可追溯，不代表核对过来源值；本原型不会自动放行到生产。
    passed = (bool(rows) and rate >= threshold and not conflicts and not rule_field_errors) if matching_only else (
        bool(rows) and output_rate >= threshold and not errors and not conflicts and coverage in (None, 1.0))
    retryable = False
    if not rules:
        failure_kind, message = "GZKJG_EMPTY", "规则库查询结果 GZKJG 为0条，请检查上游查询/API；停止解析修正。"
    elif not index:
        failure_kind, message = "GZKJG_INVALID", "GZKJG 没有有效指标名称，请检查上游字段映射。"
    elif conflicts:
        failure_kind, message = "RULE_AMBIGUITY", "规则库存在重名指标，需要明确规则范围。"
    elif rule_field_errors:
        failure_kind, message = "RULE_FIELDS_INVALID", "规则编码缺失或规则字段不满足当前接口。"
    elif not rows:
        failure_kind, message = "PARSE_EMPTY", "解析结果 ExcelData 为0条，需要检查文件读取和表格定位。"
        retryable = True
    elif errors and not matching_only:
        failure_kind, message = "DATA_QUALITY", "解析字段或规则字段异常，需查看具体诊断。"
    elif passed:
        failure_kind, message = None, "名称匹配及当前质量检查达到门槛。"
    elif unmatched:
        failure_kind, message = "NAME_MISMATCH", "ExcelData 与有效 OUTDATA 差距超出门槛或预期名称缺失，需核对名称构造证据。"
        retryable = True
    else:
        failure_kind, message = "COVERAGE_GAP", "本报表预期指标未完整提取，需要核对表格范围。"
    notifications = [] if passed else [{"code": failure_kind, "message": message, "severity": "error"}]
    if passed and unmatched:
        notifications.append({"code": "PARTIAL_MATCH", "severity": "warning",
                              "message": f"仍有 {len(unmatched)} 条未匹配，已保留明细。"})
    if unit_differences:
        notifications.append({"code": "UNIT_CONVERSION_REQUIRED", "severity": "warning",
                              "message": f"{len(unit_differences)} 条采集单位与库表单位不同，尚未换算，不能直接写库。"})
    units = {
        "indicator_unit": list(dict.fromkeys(r["indicator_unit"] for r in output if r["indicator_unit"])) or ["-"],
        "KB_unit": list(dict.fromkeys(r["KB_unit"] for r in output if r["KB_unit"])) or ["-"],
    }
    if not output:
        output = [{k: "" for k in (
            "indicator_code", "indicator_name", "indicator_data", "indicator_unit", "KB_unit",
            "convert_ratio", "unit_ratio", "publictime", "end_date", "remark", "PLPL", "SJPLLB") }]
        output[0].update(indicator_code="empty", publictime=publictime)
    return {
        "status": "candidate_pass" if passed else "needs_review",
        "failure_kind": failure_kind, "retryable": retryable,
        "notifications": notifications,
        "production_ready": False,
        "metrics": {"parsed_count": len(rows), "matched_count": matched, "match_rate": rate,
                    "valid_outdata_count": output_count, "output_rate": output_rate,
                    "dropped_count": len(rows) - output_count,
                    "drop_rate": 1 - output_rate if rows else 0.0,
                    "rule_count": len(rules), "match_threshold": threshold, "expected_coverage": coverage},
        "decision_basis": "indicator_name_exact_match" if matching_only else "strict_quality_checks",
        "errors": errors, "unmatched": unmatched, "rule_conflicts": sorted(set(conflicts)),
        "unit_differences": unit_differences,
        "missing_expected_keys": missing,
        "review_reasons": ["尚未核对原始单元格、单位语义和期间语义"] +
                          (["缺少本报表预期指标范围，无法证明无漏提"] if expected is None else []),
        "OUTDATA": output, "unitArray": units,
    }


def run_loop(params, generate, execute, max_attempts=3):
    """generate(context)->候选；execute(candidate, params)->ExcelData。

    生产 execute 必须接入隔离运行器；此处只定义接口，不提供裸 exec。
    max_attempts 包含首次尝试。每次模型只收到输入副本和诊断，不能修改评估规则。
    """
    if not 1 <= max_attempts <= 3:
        raise ValueError("本原型 max_attempts 允许 1..3")
    original = copy.deepcopy(params)
    history = []
    rules = original.get("GZKJG")
    try:
        rules = normalize_rules(rules)
    except ValueError as exc:
        return {"status": "needs_review", "failure_kind": "GZKJG_INVALID", "attempts": 0,
                "history": [], "notifications": [{"code": "GZKJG_INVALID", "severity": "error",
                                                     "message": str(exc)}]}
    original["GZKJG"] = copy.deepcopy(rules)
    preflight = evaluate([], rules)
    if preflight["failure_kind"] in {"GZKJG_EMPTY", "GZKJG_INVALID", "RULE_AMBIGUITY"}:
        return {"status": "needs_review", "failure_kind": preflight["failure_kind"], "attempts": 0,
                "history": [], "result": preflight, "notifications": preflight["notifications"],
                **{key: preflight[key] for key in ("OUTDATA", "unitArray", "metrics")}}
    events = []
    for attempt in range(1, max_attempts + 1):
        candidate = None
        try:
            candidate = generate({"params": copy.deepcopy(original), "history": copy.deepcopy(history), "attempt": attempt})
            rows = execute(candidate, copy.deepcopy(original))
            result = evaluate(rows, rules, original.get("publictime", ""), original.get("expected_keys"),
                              original.get("match_threshold", 0.8))
        except NeedsReviewError as exc:
            result = {"status": "needs_review", "failure_kind": "BUSINESS_CONTEXT_REQUIRED",
                      "retryable": False, "error": str(exc)[:2000], "notifications": [
                          {"code": "BUSINESS_CONTEXT_REQUIRED", "severity": "error", "message": str(exc)[:2000]}]}
        except Exception as exc:
            result = {"status": "execution_error", "failure_kind": "GENERATION_OR_EXECUTION_ERROR",
                      "retryable": True, "error_type": type(exc).__name__, "error": str(exc)[:2000],
                      "notifications": [{"code": "GENERATION_OR_EXECUTION_ERROR", "severity": "error",
                                         "message": str(exc)[:2000]}]}
        history.append({"attempt": attempt, "candidate": candidate, "result": result})
        events.extend(dict(event, attempt=attempt) for event in result.get("notifications", []))
        if result["status"] == "candidate_pass" or not result.get("retryable"):
            break
    exhausted = len(history) == max_attempts and result.get("retryable", False)
    if exhausted:
        events.append({"code": "ATTEMPTS_EXHAUSTED", "severity": "error",
                       "message": f"已尝试 {max_attempts} 次，仍未达标，需要人工检查。"})
    return {"status": result["status"], "failure_kind": result.get("failure_kind"),
            "attempts": len(history), "stop_reason": "attempts_exhausted" if exhausted else result["status"],
            "result": result, "history": history, "notifications": events,
            **{key: result[key] for key in ("OUTDATA", "unitArray", "metrics") if key in result}}


def demo():
    # 纯合成数据：证明诊断反馈/修正协议，不冒充真实模型生成与真实 Excel 解析。
    params = {"GZKJG": [{"rule_indicator_name": "地区生产总值累计值", "rule_indicator_code": "DEMO_GDP",
                        "rule_constant_name": "亿元"}],
              "expected_keys": [["地区生产总值累计值", "2026-06-30"]]}
    def generate(context):
        return {"version": context["attempt"], "name": "生产总值" if not context["history"] else "地区生产总值累计值"}
    def execute(candidate, _params):
        return [{"indicator_name": candidate["name"], "indicator_data": "123.45", "indicator_unit": "亿元",
                 "end_date": "2026-06-30", "source": {"sheet": "合成示例", "cell": "C4"}}]
    return run_loop(params, generate, execute)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--input", type=Path, help="包含 ExcelData、GZKJG、可选 expected_keys 的 JSON")
    cli.add_argument("--output", required=True, type=Path)
    args = cli.parse_args()
    if args.input:
        data = json.loads(args.input.read_text(encoding="utf-8-sig"))
        result = evaluate(data["ExcelData"], data["GZKJG"], data.get("publictime", ""), data.get("expected_keys"))
    else:
        result = demo()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "attempts") if k in result}, ensure_ascii=False))
