# 开发说明

安装：`python -m pip install -e ".[xls,dev]"`。

测试：`python -X utf8 -m unittest discover -s excel_match_agent/tests -q`。

隐私检查：`python -X utf8 scripts/check_release.py`。

目录约定：业务解析生成/审核在 deepseek.py、name_review.py；主循环在 app.py；匹配逻辑在 matching.py；平台导出在 platform_export.py；生成规则在 skills/excel-rule-codegen。

修改业务规则时同步 Skill 和对应测试。保留首次生成加两次重建上限、至少两个不同编码、GZK最终命名、源值不变及平台实际入口验证。不要将运输/认证/输出协议故障当成业务重建理由。

适配层来自 DeepSeek 生成结果。调整该资产后必须更新其来源与标准化文本 SHA256，运行完整平台入口校验；不得静默更改源特定的业务核心。

使用合成数据写测试，不提交本机绝对路径、真实规则表、原始业务文件、API响应日志或密钥。提交说明写明触发条件、变化后行为及验证方式。
