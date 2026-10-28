# 开发说明

HTTP 调度在 service.py/service_worker.py，缓存逻辑在 code_cache.py，内容预审在 source_preflight.py，结果门禁在 output_checks.py。改动 DC 契约需同步 workflow_nodes 脚本及三分支集成测试，保留 SHA256 校验和传输备份。

安装：`python -m pip install -e ".[xls,dev]"`。

测试：`python -X utf8 -m unittest discover -s excel_match_agent/tests -q`。

隐私检查：`python -X utf8 scripts/check_release.py`。

目录约定：业务解析生成/审核在 deepseek.py、name_review.py；主循环在 app.py；匹配逻辑在 matching.py；平台导出在 platform_export.py；生成规则在 skills/excel-rule-codegen。

HTML/Prompt 主循环在 markdown_agent.py，可信输入和工作簿规则读取在 markdown_inputs.py，生成/提取/审核及输出契约在 markdown_protocol.py，HTML 完整代码导出在 html_platform_export.py。对应 Skills 为 html-rule-parser-codegen 和 statistical-markdown-prompt-codegen。Excel 平台导出仍走原模块，勿混淆两种 MD5。

所有业务资产修复由用户模型完成。格式补问最多一次，不占业务重建；恢复必须验证原始响应和输入，不能重置首次加两次重建预算。测试应使用假模型及合成源证据，不在 CI 请求真实模型。Windows/Linux CI 仍需通过，分发适配器端点脱敏后需同步来源记录与哈希。

修改业务规则时同步 Skill 和对应测试。保留首次生成加两次重建上限、至少两个不同编码、GZK最终命名、源值不变及平台实际入口验证。不要将运输/认证/输出协议故障当成业务重建理由。

适配层来自 DeepSeek 生成结果。调整该资产后必须更新其来源与标准化文本 SHA256，运行完整平台入口校验；不得静默更改源特定的业务核心。

使用合成数据写测试，不提交本机绝对路径、真实规则表、原始业务文件、API响应日志或密钥。提交说明写明触发条件、变化后行为及验证方式。
