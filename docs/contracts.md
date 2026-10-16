# 输入输出约定

## GZKJG

```json
[
  {
    "rule_indicator_name": "示例指标甲累计",
    "rule_indicator_code": "DEMO001",
    "rule_constant_name": "万元",
    "rule_unit_ratio": "",
    "indicator_release_frequency": "月"
  }
]
```

兼容旧字段 indicator_name、indicator_code、constant_name。编码最终为字符串。GZK 为空或存在无法消歧的规则时停止，不虚构编码。示例中只有一个指标时，不满足至少两个不同编码的交付条件；完整可运行的合成示例包含两个。

## 内部解析与平台解析

内部解析核心接收 workbook.sheets；每个 sheet 有 name、rows、merged_ranges、number_formats。每条记录至少包含 indicator_name、indicator_data、indicator_unit、end_date，以及 source.sheet/cell。内部执行器禁止代码自己下载文件或读取本机路径。

平台完整文件的最终 main 接收 fileMd5 和可选开始节点参数。通用适配层读取实际源文件，构造相同的 workbook 后调用原核心。成功返回：

```json
{"code":0,"msg":"success","ExcelData":[{"indicator_name":"示例指标甲累计","indicator_data":0,"indicator_unit":"万元","end_date":"2026-06-30","source":{"sheet":"Sheet1","cell":"B2"}}]}
```

平台函数不在运行时调用模型。模型在 Agent 的生成、审核和修复阶段调用；完整平台函数是该阶段的交付物。适配层的公共分发版本使用可配置的下载地址占位值，不包含开发者内网地址。

## 最终匹配节点 JSON

根对象依次为 unitArray、codeOutputResult、OUTDATA。codeOutputResult 内含相同内容的 unitArray 和 OUTDATA，不表示重复的匹配数量。

每条 OUTDATA 字段依次为：

```text
end_date, indicator_data, SJPLLB, indicator_code, publictime,
indicator_unit, convert_ratio, indicator_name, unit_ratio,
KB_unit, remark, PLPL
```

indicator_data 是整数时保留 JSON 数字，是小数时输出字符串，已有字符串保持其有效数值写法。其他字段为字符串。截止日期按实际统计期间推导；publictime 只作发布时间，缺失则为空。单位和数值不在本节点换算。

## 失败与恢复

失败输出包含 status=error、error_code、requires_human_review、attempts、rebuilds、max_rebuilds、human_review_file 和通知。成功节点 JSON 不使用 empty 占位记录替代。

名称问题或不足两个不同匹配编码才触发业务重建，最多两次。认证、连接、响应截断、审核协议和平台导出故障均报告其具体阶段，不自动重开业务生成预算。

历史审核协议故障可通过 resume_from_run 显式恢复，程序检查同一输入和代码并保留原计数。审核截断可配 resume_incomplete_review；已完成审核批次可配 resume_review_cache。仅重新校验未经编辑的完整模型回复，不能拿截断内容或思考正文执行。普通新任务不要填写恢复项。
