# Validation checklist

For the current Excel Agent also follow [platform-runtime.md](platform-runtime.md): test the complete delivered file's final main with fileMd5 and no workbook, not only internal workbook injection. Require nonzero errors for failed input/download and exact equality with the verified core's records and final matching JSON. Check lazy format dependencies and decoded actual filename priority against a conflicting upstream title. The Agent-prefixed format constants are valid equivalents of the legacy unprefixed constants below. Platform export/transport failures must not silently return success or reset the first generation plus two rebuild budget.

Run these checks before reporting completion.

## Syntax

Compile every touched parser:

```powershell
D:\excel\.venv\Scripts\python.exe -m py_compile <parser.py>
```

If that Python is unavailable, find the local environment with `xlrd` and `openpyxl`. In this workspace, `D:\excel\.venv\Scripts\python.exe` has been the reliable environment for `.xls` tests.

## Production scan

Search touched files, or the full parser folder when asked, for forbidden patterns:

- `__main__`
- `input(`
- `sys.stdin`
- `TEMPLATE_NAME`
- `template_name`
- `or TEMPLATE_NAME`
- the token `datetime` anywhere in generated parser source, including imports, aliases, class references, annotations, comments, and logging helpers
- hardcoded local source-file reads at runtime
- title fallback to sample filenames

Local test harnesses may read `D:\excel\org`, but production parser code must not.

For every parser using `fileMd5` or `parseFileMd5`, verify all of the following:

- `XLSX_MAGIC = b"PK"` is declared and referenced by format dispatch;
- `XLS_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")` is declared and referenced;
- `CSV_ENCODINGS` contains `utf-8-sig`, `gbk`, `utf-8`, and `gb2312`, and the CSV decoder iterates it;
- `.xlsx`, `.xls`, and `.csv` reach the same template parsing core;
- format detection does not depend only on file extension and does not use inline magic literals instead of the constants.

If a parser needs dates or Excel serial conversion, confirm that it uses no date-object dependency. Read `year`, `month`, and `day` attributes from date-like cell values; use `xlrd.xldate_as_tuple()` for legacy `.xls` serial dates; and use `calendar.monthrange()` for period-end calculation.

Run a case-insensitive scan before delivery and require zero matches:

```powershell
rg -ni "datetime" -- <parser.py>
```

## Name deletion scan

Search for patterns that may delete name content:

- `match.start`
- `text[:match`
- `replace("%", "")`
- `name_without_unit`
- `cleaned_name`
- broad `re.sub` that removes bracket content from names

If such code exists, inspect whether it only removes a leading row number required by the rule. Otherwise change it so indicator names keep the original content.

## Comment scan

Top comments must:

- use a multiline raw docstring immediately after the UTF-8 coding line;
- include all mandatory traceability labels from `production-rules.md`, including source metadata, `excelrule行号`, source file, indicator example, all four field derivation rules, position information, remark/Sheet restrictions, and global rules;
- include far-right global rules;
- not contain mojibake or broken Chinese;
- use `r"""..."""` when Windows paths such as `D:\excel\org\...` appear.

Reject one-line headers such as `r"""excelrule第195行；读取指定sheet。"""` even when the statement itself is correct.

Prefer reading comment content from `excelrule.xlsx` rather than hand-writing long Chinese strings.

## Date behavior samples

Verify the shared date method with samples:

- `2026年1-6月` → `2026-06-30`
- `2026年1至6月` → `2026-06-30`
- `1-6月` + title year `2026` → `2026-06-30`
- `2026年6月` → `2026-06-30`
- `2026年5月` → `2026-05-31`
- `2026年五月` → `2026-05-31`
- `2026年1-5月` → `2026-05-31`
- `2026年1至5月` → `2026-05-31`
- `2026年前5个月` → `2026-05-31`
- `2026.04` → `2026-04-30`
- `202604` → `2026-04-30` when context is monthly
- `2026M04` → `2026-04-30`
- `1-5月` + title year `2026` → `2026-05-31`
- `2026年一季度` → `2026-03-31`
- `一季度GDP累计值` + title year `2026` → `2026-03-31`
- `2026年第2季度` → `2026-06-30`
- `二季度累计值` + title `2026年1-6月...` → `2026-06-30`
- `1984Q1` → `1984-03-31`
- `2026年上半年` → `2026-06-30`
- `2026H2` → `2026-12-31`
- `2026年全年` → `2026-12-31`
- single year `2025` → `2025-12-31`
- `May 2026` → `2026-05-31`

If the rule says remark date overrides title date, verify row-level remarks.

Verify that article publication dates do not override a more specific reporting period. For example, title `2026年1-6月...` and publication date `2026-07-15` should still produce period-end dates based on the indicator/header/title period, not `2026-07-15`.

For monthly, quarterly, half-year, and annual rules, fail validation if parsed production rows have blank `end_date`.

For custom-formatted Excel dates, verify both raw and displayed cases:

- raw date `2026-01-01` in monthly context → `2026-01-31`;
- displayed `2026-1-1` in monthly context → `2026-01-31`;
- exact daily rule + `2026-01-01` → `2026-01-01`;
- plausible Excel serial date converts correctly before period-end normalization.

If the rule says “本季” is only valid in quarter-end months, verify that non-quarter months skip only the intended 本季 columns and their adjacent同比 columns.

## Name/unit samples

Verify:

- `同比增长(%)` remains `同比增长(%)`;
- unit extraction returns `%`;
- bracket units such as `(万元)` remain in the name unless deletion is explicitly requested;
- sibling hierarchy does not concatenate same-level nodes;
- sheet name is included only when required by the rule.
- external `title`, `fileName`, `filename`, `pageTitle`, or webpage/article title is not included in `indicator_name` unless the rule explicitly requires it. For CSV pseudo sheet names, test with a deliberately wrong runtime title and confirm the indicator-name prefix still comes from the rule/source filename, not runtime metadata.

For exchange/market-data templates using external `publictime`, verify:

- `publictime="2026-08-15 11:22:33"` outputs `end_date="2026-08-15"`;
- `publictime="2026年8月15日"` outputs `end_date="2026-08-15"`;
- empty `publictime` remains empty unless the rule gives another date source.

For numeric strings with thousands separators, verify `indicator_data` contains no commas after parsing:

- `13,509.68` → `13509.68`;
- `2,896` → `2896`;
- `42,178,684,208,741.09` → `42178684208741.09`.

For Shenzhen Stock Exchange / exchange market-data templates, verify the unit priority:

- header bracket unit overrides the indicator-name fallback;
- A-column / indicator-name bracket unit is used when the header has no unit;
- valuation ratio indicators such as `股票平均市盈率` output `倍` when no bracket unit is present;
- ordinary rate indicators without bracket units output `%`, for example `股票平均换手率` should not output `-`;
- only truly unitless indicators output `-`.

## Parse test

If the source workbook is available:

1. Load the parser with `importlib.util.spec_from_file_location` or `runpy.run_path`.
2. Read local source bytes from `D:\excel\org` in the test harness only.
3. Call `parse_file_bytes(raw, title=...)` or the closest internal parse function.
4. Report:
   - output count;
   - several sample rows;
   - whether `indicator_name`, `indicator_unit`, and `end_date` match the rule.
   - whether every emitted row has nonblank `indicator_name`, `indicator_data`, `indicator_unit`, and `end_date`.

If the `excelrule` row remark specifies a worksheet, table, row range, column range, or other source fragment, verify the parse only reads that scope. If the remark is empty, verify the parse covers all effective content in the source file.

Do not add local file reads to the parser for testing.

## Protected/hidden XLSX and XML fallback

When the rule remark mentions Sheet protection, hidden rows/columns, or the source triggers an `openpyxl` style/XML error, require all of the following:

- normal `openpyxl` loading is attempted first;
- the parser contains a read-only XLSX ZIP/XML fallback that does not read `xl/styles.xml`;
- `xl/workbook.xml` and `xl/_rels/workbook.xml.rels` resolve the target Sheet path;
- `xl/sharedStrings.xml`, inline strings, numeric cells, and cached formula results are handled;
- worksheet cells with explicit `c/@r` coordinates and cells with no coordinate are both handled; coordinate-free cells are assigned by sequential XML position rather than all collapsing into column A;
- the XML fallback can read the target Sheet and produces the same rule-level output as the normal reader on the available sample;
- hidden rows and hidden columns needed by the rule are still present in the parsed rows/cells;
- when a real `fileMd5` is available, the original downloaded bytes are tested in addition to any local copy, because resaving may repair the malformed XML structure;
- source bytes or file hash are unchanged before and after testing;
- no code unlocks Sheet protection, clears hidden flags, modifies the ZIP package, or resaves the source workbook.

## Large output caution

For parsers producing large `ExcelData`, estimate JSON size:

```python
len(json.dumps({"ExcelData": data}, ensure_ascii=False).encode("utf-8"))
```

If parsing is fast locally but the platform times out, suspect:

- download API latency;
- large JSON serialization;
- logging;
- downstream unit conversion query;
- downstream matching or database insertion.

## Git check

Before upload, confirm `git status --short` does not include source Excel files, `org`, `.artifact_analysis`, `node_modules`, `__pycache__`, or `.pyc`. It should mainly show `.py` files and `.gitignore`.
