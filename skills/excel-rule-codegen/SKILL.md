---
name: excel-rule-codegen
description: Generate or revise production-ready standalone Python Excel parsers from a rule workbook such as excelrule.xlsx and a source .xls/.xlsx/.csv workbook. Use when Codex must inspect Excel rules for indicator name, data, unit, end-date, global normalization, platform download by fileMd5, comments, validation, or Git hygiene for parser scripts.
---

# Excel Rule Codegen

## Agent and platform delivery modes

For `excel_match_agent`, read [references/platform-runtime.md](references/platform-runtime.md) first. This mode uses only the excelrule indicator-name example and name/value construction logic as business input; unit/date rules are inferred by the user's DeepSeek from the actual source. Routing fields locate rules only. Final names follow GZK for the same indicator. These current Agent requirements override legacy instructions below about reading other rule columns, template downloads, fixed output folders, or publication-date fallbacks.

The Agent generates a restricted internal `main(params)` over `workbook.sheets`, executes it and reviews names. A successful delivery for an automatic workflow must additionally include the complete platform parser that accepts `fileMd5`, downloads/reads the source and calls the same core. Never hand the internal parser alone to a platform that supplies only fileMd5. The Agent's tested DeepSeek-generated shared adapter may be appended without changing business parsing logic. Validate the full delivered file through its final main with platform inputs before reporting success.

## Purpose

Use this skill to generate or fix one standalone Python parser from:

1. a rule workbook, treated as the source of truth;
2. a source workbook, treated as the actual layout to inspect.

The parser must expose `main(params)` and return:

- `indicator_name`
- `indicator_data`
- `indicator_unit`
- `end_date`

These four fields are production keys, not optional diagnostics. Downstream matching requires a nonblank indicator identity, data value, unit, and `end_date`; an empty `end_date` is a matching failure for monthly, quarterly, and yearly indicators even when the name and unit are correct.

## Core rule

Use the rule workbook for the authorized name construction logic and the actual source for source layout, values, units and reporting periods. Reuse code structure from similar templates only when the applicable rules agree. Never invent transformations, fallback dates, fallback file names, fallback units, or fake data. In Agent mode, do not consult excelrule's old unit/date columns or old sample filename for current reporting periods.

If a field cannot be derived from the rule workbook, the source workbook, or runtime `params`, do not fabricate it. For production output rows, prefer skipping or flagging records that lack required fields rather than emitting rows with blank `end_date` or blank required identity/data fields.

## Required workflow

1. Read the rule workbook first.
   - Identify the exact row by requested source file, path, or user-specified row.
   - Read global rules on the far-right columns before generating code.
   - Extract parser filename, source file, target table, path, indicator-name logic, normalization, unit logic, unit position, date logic, and date position.
   - When indicator-name logic contains a worksheet or the user provides path-code name-concatenation or regex workbooks, look up the matching `数据源路径代码` worksheet. Apply a path-specific regex rule when available; use the concatenation workbook's complete name for construction prechecks. The same indicator's GZK name has final priority. If no mapping matches, retain the name constructed from the source and authorized rules.
   - If a mapping sheet is missing, report its workbook and path code without inventing a replacement mapping. In the already authorized Agent workflow, record the missing reference and use actual source/name rules with GZK fallback; do not silently claim a completed concatenation-table comparison or stop to request permission again.

2. Inspect the source workbook.
   - Check sheet names, sheet count, merged cells, title rows, unit rows/columns, date rows/columns, data start rows, blank rows, remark rows, and independent tables inside a sheet.
   - For multi-sheet files, add `sheet_name` only when the rule says so.
   - For hierarchy rules, inspect the real indentation/numbering rather than assuming the table always starts from column A.

3. Generate one `.py` file.
   - Do not create a project or modules.
   - Use the old self-contained style: helpers + `BASE_DOWNLOAD_URL` + `main(params)` + `ExcelData`.
   - Use `fileMd5` for source download, not parseFileMd5 or markdownMD5. The Agent shared adapter uses `_PLATFORM_XLSX_MAGIC`, `_PLATFORM_XLS_MAGIC` and `_PLATFORM_CSV_ENCODINGS` to avoid collisions with the parsing core. Standalone templates may use the unprefixed constants below. Both must drive real format branches.
   - Detect the downloaded content by those constants and route `.xlsx`, `.xls`, and `.csv` into the same template-specific parsing core. Do not replace this with filename-extension-only detection or inline magic literals.
   - In this workspace, write generated parser `.py` files to `D:\excel\excel` by default. Keep source Excel files, rules, prompts, and skills outside that output folder.
   - Name the `.py` according to the rule workbook. If the rule says to use the path, replace `\` with `_`.
   - Put a complete Chinese traceability docstring immediately after `# -*- coding: utf-8 -*-`; this is mandatory for every generated parser. Follow the exact field list in `references/production-rules.md` and populate it from the rule workbook and inspected source workbook.
   - A one-line summary such as `r"""excelrule第195行；读取指定sheet。"""` is never sufficient. Do not omit fields merely to shorten the file; use an empty value only when the rule workbook truly has no value.
   - When upgrading an existing batch that already records `excelrule第N行` in each module docstring, use `scripts/upgrade_traceability_headers.py` to populate the complete headers deterministically from the rule workbook, then inspect the result before delivery.

4. Validate.
   - Compile every touched parser.
   - If the source workbook exists locally, run a representative parse using local bytes and internal parse helpers.
   - Inspect sample names, units, and dates.
   - Run the forbidden-pattern scans in `references/validation-checklist.md`.

## Must-read references

Before writing or modifying parser code, read:

- `references/production-rules.md`
- `references/name-unit-date-rules.md`
- `references/validation-checklist.md`

## Important recent lessons

- For parsing timeouts or large historical tables, read the performance section in `references/production-rules.md`: benchmark the complete entry point, stream source rows, precompute column names, and exclude repeated metadata columns without truncating valid history.

- Keep bracket content such as `(%)`, `(万元)`, `(万平方米)` in `indicator_name` unless the rule explicitly says to delete it.
- If the rule workbook contains global normalization, apply it everywhere relevant: remove enumeration punctuation as specified, remove English comma and single quote, and remove line breaks.
- Generated comments must come from `excelrule.xlsx` fields and global rules. Avoid hand-written long Chinese strings that can become mojibake under Windows shell encoding.
- Do not leave `if __name__ == "__main__"`, `input()`, or `sys.stdin` in production parsers.
- Generated production parser source must not contain `datetime` at all: no import, alias, class reference, type check, annotation, helper, comment, or logging call containing that token. Read date-like Excel values through their `year`/`month`/`day` attributes, convert legacy `.xls` serial dates with `xlrd.xldate_as_tuple()`, and calculate month ends with `calendar.monthrange()`.
- Do not use `TEMPLATE_NAME` or a sample file name to fake title/date.
- Do not use external `title`, `fileName`, `filename`, or page title as part of `indicator_name` unless the rule workbook explicitly says the indicator name comes from that external field. These fields are usually date/source metadata only. For CSV files with no real sheet name, derive the pseudo sheet name from the rule/source filename or an explicit constant from the rule, not from runtime `title`.
- Preserve numeric values exactly; never round.
- Multi-format support is part of the production contract, even when the sample source has only one format. The constants must drive real format branches; unused declarations do not satisfy the contract.
- Full traceability is part of the production contract. Every parser must retain the rule row, source metadata, field derivation rules, selected-sheet/remark restrictions, and global normalization rules in its top docstring so a reviewer can reconstruct why the parser behaves as it does.
- For exchange/market-data templates that use external `publictime`, normalize `publictime` to `YYYY-MM-DD` before writing `end_date`; do not pass through timestamps such as `2026-08-15 11:22:33`.
- When numeric cells arrive as strings with thousands separators, validate with commas removed and output the comma-free numeric value/string. Do not leave values such as `13,509.68` in `indicator_data`.
- For Shenzhen Stock Exchange / exchange market-data templates, infer units in this order: table/header bracket unit first, then A-column/indicator-name bracket unit, then `倍` for valuation ratio names such as `市盈率`/`市净率`, then `%` for other names containing `率`, and only then `-`.
- Protected sheets and hidden rows/columns do not normally block value extraction. If an `.xlsx` cannot be opened by `openpyxl` because of invalid styles such as a bad `xl/styles.xml` border style, keep the source file unchanged and add a read-only xlsx XML fallback that reads workbook metadata, shared strings, and the target worksheet XML directly.
- Treat the shared date parser as a required component for date-heavy templates: it must handle title/header/remark sources, month ranges, quarters, half-years, years, English months, Excel serial dates, and custom-formatted cells such as monthly `2026-1-1` values that should normalize to month end.
- Date derivation priority is specific indicator/header first, then the source title/file name such as `2026年1-6月...`, then external common title fields only when rule context permits. Publication dates usually describe page release time and must not override the indicator period. Examples: `一季度GDP累计值` ends on `YYYY-03-31`; `二季度累计值` ends on `YYYY-06-30`; a title `2026年1-6月...` ends on `2026-06-30` only when the indicator itself does not provide a more specific period.
- Shared date parsing must explicitly cover `1-x月`, `x月`, quarterly, half-year, and annual expressions. Do not leave all `end_date` values empty for monthly rules; that causes otherwise correct records to fail downstream matching.
- Resolve dates in this strict order: the indicator sentence/cell or its header, the Markdown/source page title period, external common `params["title"]`, and only then normalized `publictime`. When a period has only a month, supply its year from the external title or an already established end-date year. If only `publictime` works, or the date remains ambiguous, report that row to the user for confirmation instead of hiding the fallback. In the delivery summary, state the implemented logic and include a concrete example such as `2026年6月30日`.
- Preserve punctuation that belongs to fixed GZK names. Do not strip `:` / `：` after `其中`, or parentheses in names such as `其他(净)`, merely because they are punctuation.
- The rule row remark controls source scope. If it names a worksheet, table, or range, parse only that source content; if it is empty, parse all effective source content.
- When the same `(indicator_name, end_date)` appears with different values, inspect the source structure before choosing. Typical fixes include selecting the more specific monthly row over a colliding annual row, aggregating split subperiod rows for period-total columns while taking the last subperiod for end-of-period columns, or restoring missing parent/header components. Never silently keep arbitrary first/last values.
- For Git upload hygiene in this workspace, only parser `.py` files and `.gitignore` should be staged. Do not stage `.artifact_analysis`, `node_modules`, `org`, `excelrule`, `prompt`, `__pycache__`, `.pyc`, or source Excel files.
- Treat rule remarks such as “解除文件保护，取消隐藏行隐藏列” as a warning about workbook structure, not permission to mutate the source. Protected sheets and hidden rows/columns are readable and must remain untouched. For `.xlsx`, try `openpyxl` first; if loading fails because of malformed styles/XML, fall back to direct ZIP/XML reading of workbook relationships, shared strings, and worksheet cells while ignoring `xl/styles.xml`. Worksheet cells may omit the `r` coordinate: use `c/@r` when present and the cell's sequential position when absent. Never unlock, unhide, edit, or resave the source workbook, and validate against the original downloaded bytes when a real `fileMd5` is available.
