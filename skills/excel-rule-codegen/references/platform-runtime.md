# Current Excel Agent runtime and platform delivery

Business code generation and semantic name review use the user's configured DeepSeek API. Codex maintains orchestration, execution and verification; do not substitute Codex-authored business parsing logic. A previously tested, generic DeepSeek-generated platform adapter can be reused with provenance; new source-specific cores must be generated from this task's actual evidence.

## Business input and name repair

Use only excelrule's 指标名称示例 and 指标名称取值逻辑 (alias 指标取值逻辑) as business rules. Paths and path codes locate the row and name-concatenation sheet. Infer 指标单位取值逻辑、指标单位位置、截止日期取值逻辑、截止日期位置 from the actual source, this Skill and runtime metadata; do not use old rule columns or old example filenames for them.

Compare the concatenation sheet's complete names with actual ExcelData names, then have DeepSeek resolve plausible unmatched names against actual source titles, parent rows, headers and GZK. The same indicator's final name must be the GZK name. Units and actual frequencies assist semantic comparison; convertible quantities do not require conversion here. Incompatible dimensions are different indicators. Missing frequency remains unknown. Do not infer all indicators' frequencies from the report's overall frequency.

No unresolved name issue and at least two distinct matched indicator codes: export immediately. Genuine name construction issue, or fewer than two codes: regenerate the complete parsing core with DeepSeek, execute and review again. First generation plus at most two rebuilds; otherwise fail for manual handling. Empty GZK stops immediately. Never force matches to increase a percentage; no 80% gate. Preserve every required source numeric cell, including repeated reports of the same value. Do not deduplicate away required source evidence.

## Two executable entrypoints

The internal parser is intentionally restricted: `main(params)` reads `params['workbook']['sheets']`, returns `{'code':0,'msg':'success','ExcelData':[...]}` and does not download or read files. Each source sheet includes `name`, `rows`, `merged_ranges`, `number_formats`. Records preserve `indicator_name`, `indicator_data`, `indicator_unit`, `end_date`, plus actual `source.sheet/cell` for auditing. Integers stay integers, floats retain their original precision, source strings retain their valid numeric text.

The deliverable platform parser is a complete standalone file. Its final `main(params)` accepts fileMd5 and optional upstream metadata, downloads bytes from `https://files.example.invalid/api/file/download/{fileMd5}`, reads sheets into the same structure and calls the unchanged internal core. It does not need an API Key at parser execution time; DeepSeek is used for generation and review in the Agent. Do not export the restricted internal function alone as platform-ready code.

Use the versioned DeepSeek-generated adapter in `excel_match_agent/assets/platform-adapter.py.txt`, with hash/provenance checked by the Agent's platform export module. Do not rewrite source-specific name, value, unit or date logic in the adapter. Keep the validated core verbatim at the start of the complete file. Supply both .py and .txt with identical content. Existing valid workbook inputs may bypass downloading.

The platform adapter uses fixed-endpoint GET, a download timeout and byte limit, no model credentials and no ambient proxies or redirect forwarding. Detect xlsx/xls by magic bytes; decode explicit CSV through the declared encoding list. Load openpyxl only for xlsx and xlrd only for xls. Close workbooks. Read merged ranges and number formats as well as source values; never modify or resave the source. Reject HTML/error bodies. Missing/conflicting source identifiers and read/download failures must return nonzero code, an explanatory msg and empty ExcelData, not silent success. Only fileMd5 identifies the source; parseFileMd5 and markdownMD5 are different artifacts.

## Actual period and platform verification

Period priority: specific source indicator/header, actual source title, actual downloaded source filename, then compatible upstream title metadata. Decode Content-Disposition filenames, including RFC5987, percent-encoded and Latin1/UTF8 Chinese variants. A real downloaded filename overrides a conflicting external source_name/title. Do not hardcode a template/month or use publication time as the statistical period. publictime is publication metadata only in this workflow.

After name review and matching pass, export the complete platform file and execute its final main. First replay with the same injected workbook and require exact equality of all source records. When `platform_verify_download` is enabled, also run with upstream fileMd5/metadata and no workbook, perform a real download, require nonempty successful ExcelData, and require exact equality with the verified internal records. Match those actual platform rows using the same GZK and require the same fixed JSON output. A failed platform check must withhold success output and report the platform stage; do not consume another business-parser rebuild merely for a transport/export failure.

The matching node's final JSON remains unitArray, codeOutputResult and OUTDATA. The parsing node still returns code/msg/ExcelData; map ExcelData to the workflow's excel_data input. Keep integer indicator_data as JSON numbers and floats as strings at the matching export layer, with all other node fields following the established contract. Unit conversion belongs to downstream nodes.
