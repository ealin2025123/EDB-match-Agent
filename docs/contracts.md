# 输入输出约定

## HTTP / DC 交付契约（0.3.0）

下文的 node-output.json 是 CLI 业务结果。HTTP 服务在此基础上为 Excel/HTML 追加 `result.excel_data`，读取最终审核轮次原始四字段明细；Prompt 保持 `OUTDATA`。

服务按真实 fileType 选择分支，POST /v1/match/jobs 返回 job_id，GET 同一路径查询；queued/running 不能进入接出。HTTP 的 ready 对应内部正式通过，failed/interrupted 保留具体错误、阶段及处理建议。

HTTP 交付的 result._asset_transfer 是 Base64 传输备份，包含任务、分支与 SHA256，统一接出节点校验并移除它。不要把该字段当成业务结果入库。ExcelData 是解析器返回字段，excel_data 是 HTTP/DC 原始明细字段，不能混用大小写。

Prompt 资产为 PromptCode/PromptFile，Excel/HTML 为 PythonCode/PythonFile。统一接出后 OUTDATA 映射 out_data；Prompt 接原 MD 单位清洗链时设 platform_chain="md_array"，它可追加 unitArray。table_audit 只用于 Excel/HTML。

完整说明见 [HTTP 与 DC 接入](http-service.md)、[缓存与质量门禁](cache-and-quality.md)。

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

### HTML 与 Prompt 分支

通过 parse_strategy 显式选择 html 或 prompt。开始节点参数可通过 start_file 读取平台导出 TXT/JSON；输入含 markdownMD5、title、publictime。按下载地址追加 `/markdownMD5` 获取正文，拒绝 Excel/PDF 二进制、原始 HTML 错误页、空正文和 JSON 错误体。不回退到 fileMd5。

HTML 内部 main(params) 接收 Markdown、title、publictime，返回 code/msg/ExcelData。每条严格为四个字符串字段：indicator_name、indicator_data、indicator_unit、end_date。交付平台入口自行按 markdownMD5 下载正文，再调用相同核心。最终 JSON 根字段与 Excel 相同；规范整数字面量输出 JSON 整数，小数及有精度格式的字符串保留。

PromptCode 为完整 system prompt，结合 Markdown、title、GZK.GZKJG 和 publictime 执行。模型提取只返回 out_data，记录严格为五个字符串字段：indicator_code、indicator_name、indicator_data、indicator_unit、end_date。名称与编码必须来自同一 GZK 记录。

Prompt 最终匹配输出只有 OUTDATA；每条依次为 indicator_code、indicator_name、indicator_data、indicator_unit、KB_unit、convert_ratio、unit_ratio、indicator_release_frequency、publictime、end_date、remark、PLPL，全部字符串。不输出 unitArray 或 SJPLLB。

Excel gzk_file 新增兼容平台导出 TXT；HTML/Prompt 保持原有规则库查询转输出读取逻辑，不宣称支持所有平台任意文本。不会执行导出文本中的 scriptCode。

独立审核逐条引用源文并检查全部 GZK/正则名称：同一指标真实遗漏为 missing；明确不同口径或源文不存在为 not_in_source，不因此阻止其他指标；确实无法判断才 uncertain。至少两个不同编码且审核无真实错误即可通过，无匹配率门槛。

HTML/Prompt 正式失败状态为 needs_review，需查看 failure_kind、notifications 及 human-review.json；不沿用下述 Excel 外层错误包装判断 HTML/Prompt 状态。

失败输出包含 status=error、error_code、requires_human_review、attempts、rebuilds、max_rebuilds、human_review_file 和通知。成功节点 JSON 不使用 empty 占位记录替代。

名称问题或不足两个不同匹配编码才触发业务重建，最多两次。认证、连接、响应截断、审核协议和平台导出故障均报告其具体阶段，不自动重开业务生成预算。

历史审核协议故障可通过 resume_from_run 显式恢复，程序检查同一输入和代码并保留原计数。审核截断可配 resume_incomplete_review；已完成审核批次可配 resume_review_cache。仅重新校验未经编辑的完整模型回复，不能拿截断内容或思考正文执行。普通新任务不要填写恢复项。
