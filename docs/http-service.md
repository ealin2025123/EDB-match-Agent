# HTTP 服务与 DC 工作流接入

## 启动与配置

从仓库根目录运行 `python -m excel_match_agent.service init-config`。已有配置不会覆盖。服务配置默认在 `excel_match_agent/service-runtime/config.json`，包含独立随机 `api_token`、`host`、`port`、`runtime_dir` 和三分支 `profiles`。

profiles 指向完整 CLI 任务模板，配置下载地址、规则工作簿、Skill 和模型选项。init-config 优先寻找本地任务模板，否则使用根目录 examples 模板；示例规则和下载地址需要替换。服务不会向客户端开放任意本机配置路径或模型凭据设置。

`python -m excel_match_agent.service check-config` 检查配置，`python -m excel_match_agent.service serve` 在前台启动。默认地址为本机回环；跨机器访问需管理员自行配置监听、路由和最小必要访问权限。不要用 `localhost` 指代另一台服务器。

模型配置放根目录 `.env` 或进程环境；HTTP Bearer 使用服务配置中的 `api_token`，二者不可混用。修改服务代码后需在队列空闲时重启，避免中断任务。不要同时启动多个实例或手工删除锁文件。

## 请求与幂等

`GET /health` 不需令牌，预期 `{"status":"ok","service":"excel-match-agent","contract_version":1}`。

提交：`POST /v1/match/jobs`，请求头 `Authorization: Bearer <SERVICE_TOKEN>` 与 `Content-Type: application/json`。合成结构模板见 [service-request.json](../examples/service-request.json)。

必需业务字段：`pageTitle`、真实 `fileType`、本次有效 `GZKJG`（也接受 `GZK.GZKJG`）；Excel 用 `fileMd5`，HTML/Prompt 用 `markdownMD5`。可传 `title`、`publictime` 和稳定的 `workflow_run_id`。不得提交 PythonCode、模型 Key 或本机路径作为 HTTP 入参。

受理返回 `job_id`；查询 `GET /v1/match/jobs/{job_id}?wait_seconds=25`，wait_seconds 必须在 0 到 25 之间。该参数控制本次 HTTP 等待，不代表整个任务超时。

相同业务输入会按幂等策略复用任务；规则数组仅换顺序不应创建重复任务。显式运行 ID 不得绑定不同请求。已通过结果可在相同范围内复用；资产缓存则用于不同源文件的同路径任务。两者不是同一层缓存。

状态：`queued` 排队、`running` 执行中、`ready` 正式交付、`failed/interrupted` 终止。客户端超时但未收到 job_id 不意味着未受理，保持输入和运行 ID 一致重试。

## DC 节点顺序及字段

1. 上游准备本次 pageTitle、源标识、真实文件类型和非空 GZK。
2. [DC_Agent接入.py](../excel_match_agent/workflow_nodes/DC_Agent接入.py) 填自己的 SERVICE_BASE_URL、SERVICE_TOKEN；三分支共用。保留完整 agent 数组，含 status、stage、job_id、parse_strategy、error_code、message、result_json、code_sha256 以及两类资产字段。
3. 对 queued/running 保留同一 job_id 继续查询；接入默认等待约 100 秒后以 DC_AGENT_RETRY 提示继续。不要每次清空编号重复提交。
4. ready 进入 [DC_匹配结果输出.py](../excel_match_agent/workflow_nodes/DC_匹配结果输出.py)，三分支共用最新版。该节点不再执行 Python 或调用模型提取。
5. 接出 OUTDATA 映射下游 out_data；Excel/HTML 可同时输出 excel_data。按原业务链执行一次单位清洗/换算、内审及入库。

| 分支 | agent 资产字段 | 接出模式 |
| --- | --- | --- |
| Excel / HTML | PythonCode、PythonFile | 默认 core；接原表格频率审核链可用 table_audit 并传当前 GZKJG |
| Prompt | PromptCode、PromptFile | 默认 core；接原 MD 单位数组链设 platform_chain="md_array" |

Prompt 的 md_array 模式整理去重单位对和空待计算比例，不自行换算。不能给 Prompt 设置 table_audit。Prompt 已由 Agent 执行并审核，不再接旧的提示词提取节点。统一接出保留历史资产字段名 python_code/python_file；Prompt 下其内容是提示词，不能当 Python 执行。

服务端 result._asset_transfer 是传输备份，不是业务明细；接出核对任务、分支和 SHA256 后移除。代码字段缺失时可恢复，非空但被篡改的字段不会静默覆盖。不要删除哈希校验绕过错误。

excel_data 必须配置为接出输出映射。服务已支持 HTML 明细，而旧 DC 接出脚本只允许 Excel 时，会报“匹配结果与分支JSON契约不符”；请更新整份脚本。

## 常见错误

| 错误 | 处理 |
| --- | --- |
| GZKJG_EMPTY / INVALID_GZK | 检查上游规则查询及 pageTitle；在调用模型前拒绝 |
| SOURCE_NO_VALID_DATA / SOURCE_GZK_NO_RELEVANT_DATA | 看具体源文预审原因；证据不足允许继续 |
| EMPTY_EXTRACTION | 修复预算内仍为空；核对源文件及规则 |
| END_DATE_EMPTY | 统计期间无法确定，禁止用发布时间顶替 |
| SERVICE_TIMEOUT / CONNECTION_REFUSED | 从 DC 运行环境测试 /health；本机成功不代表 DC 网络可达 |
| HTTP 401 | 独立服务令牌不匹配，不是 DeepSeek Key |
| 哈希或分支契约错误 | 保留原 job_id，核对最新版脚本、字段映射及传输备份 |

连通性诊断见 workflow_nodes/DC_连通性诊断.py 和 DC_health_probe_ascii.py。它们只测试网络，不提交业务任务，也不需要服务令牌。失败记录及源文件保留；清除失败任务的复用索引是显式维护操作，不自动重跑历史任务。
