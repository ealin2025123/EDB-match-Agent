# 排障

| 现象 | 检查位置与处理 |
| --- | --- |
| ExcelData 为空，GZK 有数据 | 先看解析节点 code/msg。不要把内部 workbook-only 核心直接贴到只传 fileMd5 的平台节点；使用完整 platform-parser.txt |
| 平台有 ExcelData，匹配节点 excel_data 为空 | 核对节点字段映射；返回字段名是 ExcelData，大小写敏感 |
| GZKJG_EMPTY | 提供这份源文件对应的上游规则库 JSON |
| MODEL_BACKEND_UNAVAILABLE | 检查地址、真实模型 ID、Key、网络和服务可用性；不靠重建代码修复认证错误 |
| MODEL_OUTPUT_TRUNCATED | 检查模型响应元数据和输出额度；不执行截断内容。使用支持的流式接口及适当额度 |
| NAME_REVIEW_PROTOCOL_ERROR | 查看已保存的审核校验错误；完整合法原始回复可显式续跑，不能手改模型判断 |
| INSUFFICIENT_MATCHES | 少于两个不同编码；按拼接表和实际源表重建，仍受两次上限约束 |
| PLATFORM_EXPORT_FAILED | 查看 platform/ 下测试输入、输出和 msg；真实下载必须从平台测试环境可访问 |
| 日期与上游 title 不一致 | 先核对实际源表和下载文件名。上游网页标题可能陈旧，publictime 也不是统计期间 |
| xlsx 导入时提示缺少 xlrd | 使用完整新平台文件；xlrd 只应在 xls 分支加载 |

运行目录内包含业务数据，只在可信环境中查看。提交 Issue 时提供错误码、阶段和合成复现，不直接贴完整模型请求或真实源文件。
