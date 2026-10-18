# 不通过 Codex 独立测试

1. 在 VS Code 打开项目根目录，选择 .venv 解释器，在根目录打开 PowerShell 终端。
2. 将 .env.example 复制为 .env，填写模型地址、准确模型 ID 和 Key。job 不写真实 Key。
3. 复制对应 examples/excel-job.json、html-job.json 或 prompt-job.json 为同目录下 *.private.json。
4. 准备开始节点参数、对应 GZK 输出文件和规则表。HTML/Prompt 模板采用 workbook 模式：填写 excelrule_path、rule_sheet 和从1开始的 rule_row，首行为表头。使用此模式不必再填 rule 对象。
5. HTML/正则任务填写名称规则库路径，HTML 规则行应有原网页真实超链接或明确 source_url。普通表格 Prompt 不用正则库。
6. 修改 download_base_url 为自己的下载服务。Excel 使用 fileMd5，HTML/Prompt 使用 markdownMD5；不能混用。

```powershell
& ".\.venv\Scripts\python.exe" -m excel_match_agent run --job ".\examples\html.private.json" --diagnostics
```

每条命令单独输入。出现 `>>` 通常表示引号或括号未闭合，按 Ctrl+C 后重新完整输入。不要把两个 Set-Location 粘成一行。

## 确认完成

运行时 stderr 显示阶段日志；等待进程返回 PowerShell 提示符。最后诊断中查看 output_dir、status、failure_kind、matched_indicator_count（HTML/Prompt）或匹配明细。正式成功为 candidate_pass，不根据“文件存在”认定审核通过。

打开 output_dir 对应目录。也可以按 runs 下文件夹修改时间倒序找最新任务；同时跑多个任务时，以各自终端的 output_dir 为准。

```powershell
Get-ChildItem .\runs -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 5 Name,LastWriteTime
```

- Excel/HTML：platform/platform-parser.py 或同内容 .txt 是完整平台代码，不能用 attempt-N/parser.py 代替。
- Prompt：查看 run-result.json 的 verified_asset，取该轮 prompt.md。工作流模型节点完整读取提示词，正文来自 markdownMD5，不把 MD5 字符串当正文。
- node-output.json 是最终匹配 JSON。HTML/Excel 和 Prompt 的根字段不同，按各自分支接入。
- human-review.json、notifications、failure_kind 用于失败排查。原网页待核验只有回归结果，不是正式成功。

重复运行会新建目录并重新调用模型，可能产生费用；历史目录不会自动覆盖为最新结果。默认没有整体120秒限制，elapsed_seconds 和 stage_timings 记录实际耗时。

## 协议恢复

普通新任务不填写恢复字段。HTML/Prompt 可显式使用下面三者之一，均验证输入并保留已用次数：

| 配置 | 用途 |
| --- | --- |
| replay_generated_from_run | 程序校验器修复后，复用完整合法 DeepSeek 资产重新执行审核 |
| continue_from_run | 策略修正后继承历史，从剩余次数调用 DeepSeek 新建下一轮资产 |
| repair_generation_from_run | 恢复完整但格式无效且尚未补问的响应，补问一次，不增加业务生成次数 |

生成格式补问失败为 MODEL_PROTOCOL_FAILED，不执行无效代码，不输出成功结果。响应截断、上下文过大、认证和连接错误不走格式补问。生成补问日志保留原始响应和修正响应，不手改模型原始判断。
