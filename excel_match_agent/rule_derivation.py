"""用户允许由DeepSeek从源表推导的四项规则；不改写原excelrule工作簿。"""
FIELDS = ("指标单位取值逻辑", "指标单位位置", "截止日期取值逻辑", "截止日期位置")
SCHEMA = {"type": "object", "additionalProperties": False, "required": list(FIELDS),
          "properties": {name: {"type": "string"} for name in FIELDS}}
POLICY = {"mode": "derive_from_source", "fields": list(FIELDS),
          "existing_excelrule_fields": "excluded_except_indicator_example_and_name_logic",
          "evidence_required": True, "modify_original_rule_workbook": False}


def validate_derived_rules(value):
    if (not isinstance(value, dict) or set(value) != set(FIELDS)
            or any(not isinstance(v, str) or not v.strip() for v in value.values())):
        raise ValueError("DeepSeek须给出四项推导规则及源依据；无法确定时应明确needs_review，不能虚构规则")
    return value
