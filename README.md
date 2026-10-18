# Excel Match Agent

使用你自己的 **DeepSeek / OpenAI 兼容 API**，从源内容及 Skill 生成解析资产，执行、精确匹配 GZK、独立审核，并输出与原工作流兼容的 JSON。支持 **Excel、HTML、Prompt** 三种分支，默认纯 Python，可在 VS Code 终端独立运行。

项目本身是 Agent；Skills 是交给 DeepSeek 的规范。业务代码生成、提示词生成、模型提取、语义判断及修复调用你的 API；Python 负责下载、调度、执行、匹配和校验，不调用 Codex 生成业务解析代码。

## 三种分支

| parse_strategy | 输入内容 | DeepSeek 生成/执行 | 最终交付 |
| --- | --- | --- | --- |
| excel | fileMd5 原文件或本地 Excel/CSV、规则表、GZK | 生成 Python，执行后审核 | 匹配 JSON、完整平台 PY/TXT |
| html | markdownMD5 正文、规则表、名称正则、GZK、原网页核验 | 生成 Python，执行后审核 | 匹配 JSON、完整平台 PY/TXT |
| prompt | markdownMD5 正文、规则表、GZK | 生成 Prompt，调用模型提取并审核 | TXT 契约的匹配 JSON、Prompt Markdown |

HTML/Prompt 按下载地址加 `/markdownMD5` 获取正文，不用 fileMd5 原文件代替。HTML 原网页只用于生成期设计和核验；交付解析器运行时只下载 Markdown。

三分支共用“解析 → 匹配 → 审核 → 必要时重建”闭环，保留各自输出契约：Excel/HTML 输出 unitArray、codeOutputResult、OUTDATA；Prompt 只输出 OUTDATA，保留 TXT 分支频率字段，没有 unitArray 和 SJPLLB。

## 判断与重建规则

- Excel 的规则表提供名称示例、名称取值逻辑；单位及日期逻辑由 DeepSeek 根据实际源表推导。实际统计期间优先，历史示例和发布时间不能覆盖源表。
- 结合名称拼接/正则规则预判断，再由 DeepSeek 判断同一指标；同一指标最终使用 GZK 名称和编码。
- **明确不同口径不匹配，继续其他指标**。例如本外币贷款不能当作外币贷款，对应同比同理。正则命中子串不代表业务相同。
- 单位、频率辅助语义判断；可换算单位不阻止匹配，单位转换由后续节点处理。
- **没有匹配率门槛**。没有应对应但名称未匹配的问题，且至少匹配 **2 个不同编码**即可通过；否则按证据重建代码或 Prompt。
- 首次生成后最多重建 **2 次**，仍未通过交人工处理；GZK 为空直接报告。
- HTML/Prompt 生成 JSON 重复字段等格式错误时，向同一 DeepSeek 自动补问一次；不静默覆盖重复键，不占业务重建次数。仍无效返回 MODEL_PROTOCOL_FAILED。
- 截断内容不执行；认证、网络、上游正文丢失和平台导出失败报告具体原因，不重置业务预算。

## 安装和配置

需要 Python 3.10+。在克隆或下载的项目根目录运行：

```powershell
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -e ".[xls]"
Copy-Item .env.example .env
```

本机 .env 填写：

```dotenv
DEEPSEEK_API_KEY=填写平台Key
DEEPSEEK_API_KEY_ENV=DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL=填写你的兼容API地址
DEEPSEEK_MODEL=填写实际支持的模型ID
```

程序自动添加 Authorization: Bearer，无需另填一份相同 Token。job 显式配置优先于环境默认值，不回退到其他模型提供商。仓库只分发空 Key 的 .env.example。

VS Code 打开项目根目录，选择 .venv 内解释器。Linux/macOS 使用 .venv/bin/python。只处理 xlsx/csv 可安装 `-e .`，旧 xls 才需要 xlrd。

## 准备并运行任务

分别使用 [Excel 模板](examples/excel-job.json)、[HTML 模板](examples/html-job.json)、[Prompt 模板](examples/prompt-job.json)，各自固定 parse_strategy。

```powershell
Copy-Item examples/html-job.json examples/html.private.json
# 编辑 html.private.json 后运行：
& ".\.venv\Scripts\python.exe" -m excel_match_agent run --job ".\examples\html.private.json" --diagnostics
```

真实任务放 private/ 或 *.private.json，均被 Git 忽略。相对路径**以 job 文件所在目录为基准**。example.invalid 是不可用占位地址，运行前必须替换。

| 输入 | Excel | HTML / Prompt |
| --- | --- | --- |
| 开始节点 | fileMd5、pageTitle、可选上游元数据 | start_file 指向开始节点 TXT/JSON，含 markdownMD5 |
| 规则库 | gzk_file 支持纯 JSON 及平台导出 TXT | gzk_file 使用规则库查询转**输出** TXT/JSON |
| 规则行 | excelrule_path，按路径定位 | rule_source=workbook、excelrule_path、rule_sheet、rule_row |
| 行内规则 | 名称示例及取值逻辑 | 也可直接提供 rule 对象；workbook 模式自动读取，不重复填写 |
| 正则 | 名称拼接表按配置读取 | HTML/登记正则任务配置 regex_path 或 legacy_regex_path；普通表格 Prompt 不需要 |
| Skill | 模板已配置 | 模板分别指向对应 Skill |

新正则表：汇总 Sheet 的 C 列任务 ID 精确匹配路径代码，F 列名称、K 列 selectors 中 regex rules，同任务全部行读取。旧表：按精确路径代码找同名 Sheet，读取 A/B 列。超过15位编码应存为文本；精度丢失时使用经核实的 rule_overrides，不猜数字。

合成示例：Excel 使用 examples/job.local.json，Prompt 使用 examples/prompt.local.json。它们会调用真实模型，但不含业务数据。doctor 当前针对 Excel 配置，不作为 HTML/Prompt 完整预检。

## 到哪里找结果

终端 --diagnostics 显示 output_dir 和状态；每次新建 runs/日期时间-随机编号/，按修改时间找到本次目录。

| 文件 | 用途 |
| --- | --- |
| run-result.json | status、错误、审核、重建次数和阶段耗时 |
| node-output.json | 最终匹配 JSON，仅正式成功时导出 |
| platform/platform-parser.py / .txt | Excel/HTML 完整平台代码，复制全部内容到工作流 |
| platform/platform-validation.json | 真实下载入口与审核结果一致性验证 |
| attempt-N/parser.py | 内部核心，不直接贴到只接收 MD5 的平台节点 |
| attempt-N/prompt.md | Prompt 提示词，取报告 verified_asset 指向的通过版本 |
| attempt-N/generation/protocol-repair/ | HTML/Prompt 格式补问记录 |

正式通过为 candidate_pass。HTML 经用户明确授权只回归 Markdown 时为 markdown_verified_pending_webpage，结果在 regression-node-output.json，不作为正式通过。失败时查看 failure_kind、notifications 和 human-review.json，不把历史文件当本次成功结果。

Excel 平台入口按 fileMd5 下载原文件；HTML 按 markdownMD5 下载正文，均返回 code/msg/ExcelData。Prompt 交付提示词，在工作流模型节点执行。平台解析代码运行时不再调用模型。

## 测试、隐私及限制

离线检查不调用真实 API：

```powershell
python -X utf8 -m unittest discover -s excel_match_agent/tests -q
python -X utf8 scripts/check_release.py
```

GitHub Actions 在 Windows/Linux 运行测试和隐私扫描。凭据、真实文件、工作簿、任务、运行记录及模型请求不纳入仓库或发布包；内网地址用本机配置。本地 .env 保留，不上传。

默认 total_timeout=null，没有整体120秒限制，各阶段仍有独立超时。耗时查看 elapsed_seconds，不保证每个任务120秒内完成。Python 子进程和 AST 白名单不是操作系统级沙箱；Docker 是内部解析的可选后端。

目前是本地 CLI，尚无 HTTP 服务及上游 GZK 查询 API。完整使用请克隆仓库或下载源代码包；单独安装 wheel 时仍需外部 Skill 路径。项目尚未指定开源许可证。

[输入输出约定](docs/contracts.md) · [独立测试指南](docs/testing-guide.md) · [排障](docs/troubleshooting.md) · [更新记录](CHANGELOG.md) · [隐私](SECURITY.md)
