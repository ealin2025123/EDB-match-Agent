# Production parser rules

For the current Excel Agent, [platform-runtime.md](platform-runtime.md) defines the internal/parser delivery distinction and overrides legacy rules in this file where they conflict. Platform success requires the complete file's final main to work with fileMd5 alone; the internal workbook-only core is not a standalone platform deliverable. Use only the two authorized excelrule business fields in Agent mode; infer actual units and periods from the current source.

## Runtime contract

Generate a single `.py` file with:

```python
def main(params):
    ...
    return {"code": 0, "msg": "success", "ExcelData": data_list}
```

The platform calls `main(params)` directly. Do not add:

- `if __name__ == "__main__"`
- `input()`
- `sys.stdin`
- CLI prompts
- manual file selection

## Download

Use the platform download API and `fileMd5`. Do not substitute parseFileMd5 or markdownMD5:

```python
BASE_DOWNLOAD_URL = "https://files.example.invalid/api/file/download/"

file_md5 = params.get("fileMd5")
if not file_md5:
    return {"code": -1, "msg": "缺少 fileMd5", "ExcelData": []}

response = requests.get(BASE_DOWNLOAD_URL.rstrip("/") + "/" + file_md5, timeout=60)
response.raise_for_status()
raw = response.content
```

Do not read the source workbook from `C:\...` or `D:\...` at runtime. Local paths are only for separate development tests.

## No fabricated fallback

Never add:

- `TEMPLATE_NAME = "..."`
- `CONFIG["template_name"]`
- `title = params.get("title") or "2026年..."`
- date fallback from a sample filename

Accept platform fields as-is:

```python
title = params.get("title") or params.get("fileName") or params.get("filename") or ""
```

If title/date/unit is missing and the rule cannot derive it, do not fabricate a value. Dirty upstream data should remain dirty. For production output rows, however, do not silently emit records with blank required fields; skip them or make the failure visible during validation.

## File support

Every parser that downloads bytes through `fileMd5` / `parseFileMd5` must support `.xlsx`, `.xls`, and `.csv` and declare these exact shared constants:

```python
XLSX_MAGIC = b"PK"
XLS_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
CSV_ENCODINGS = ("utf-8-sig", "gbk", "utf-8", "gb2312")
```

Use the constants in content-based dispatch; do not leave them unused and do not replace them with inline byte literals or filename-extension-only checks.

- `.xlsx`: `openpyxl.load_workbook(io.BytesIO(raw), data_only=True)`
- `.xls`: `xlrd.open_workbook(file_contents=raw)`
- `.csv`: try every encoding in `CSV_ENCODINGS`

Detect by file header when possible:

- xlsx magic: `b"PK"`
- xls magic: `D0 CF 11 E0 A1 B1 1A E1`

Route all three readers into one template-specific row/sheet parsing core so indicator-name, unit, date, and numeric rules are not duplicated by format.

## Rule remarks and source scope

Each `excelrule` row remark describes what content to read from that row's source file and the valid parsing range. Treat it as a scope constraint, not an optional comment:

- if the remark names a worksheet, table, row range, column range, or other source fragment, parse only that content;
- if the remark says only a specific sheet should be parsed, do not parse sibling sheets even when they look similar;
- if the remark is empty, parse all effective content in that source file;
- if the remark is ambiguous, inspect the workbook and ask the user rather than inventing a range.

Carry the selected-sheet/range decision into the top traceability docstring under `备注/Sheet限制`.

### Protected/hidden XLSX and malformed styles

Sheet protection and hidden rows/columns do not prevent Python from reading stored cells. A rule remark such as “解除文件保护，取消隐藏行隐藏列” must not be implemented by changing the source file.

For affected `.xlsx` templates:

1. Try `openpyxl` normally first.
2. If `openpyxl` fails because `styles.xml` or related workbook XML is malformed, read the XLSX ZIP package directly without loading `xl/styles.xml`.
3. Resolve Sheet names and targets through `xl/workbook.xml` and `xl/_rels/workbook.xml.rels`.
4. Restore shared text through `xl/sharedStrings.xml`; also support inline strings and ordinary numeric/formula-result cells.
5. Read target cells from `xl/worksheets/sheet*.xml`. Hidden `<row>` and hidden column ranges remain part of the XML and must be read normally.
6. Do not assume every `<c>` element has an `r="A1"`-style coordinate. When `c/@r` exists, use it; when it is absent, assign columns sequentially in XML order and continue sequentially after the last explicit coordinate.
7. Send both the normal `openpyxl` result and the XML-fallback result into the same template-specific parsing core.

Do not unlock Sheet protection, clear hidden flags, edit the package, remove `styles.xml`, or resave the workbook. The fallback is read-only and must operate on downloaded bytes in memory.

When a real platform `fileMd5` is available, validate the parser against those original downloaded bytes. A locally resaved copy is not sufficient because resaving can repair styles or add missing cell coordinates and hide defects in the XML fallback.

## Output rows

Each output row must use:

```python
{
    "indicator_name": indicator_name,
    "indicator_data": indicator_data,
    "indicator_unit": indicator_unit,
    "end_date": end_date,
}
```

`indicator_name`, `indicator_data`, `indicator_unit`, and `end_date` are required for downstream matching. A blank `end_date` is not a harmless “no match” condition for monthly, quarterly, or annual rules; it prevents otherwise correct names and units from entering final output. Gate emitted rows with a helper equivalent to:

```python
def is_complete_record(name, data, unit, date_text):
    return bool(name) and data is not None and str(data).strip() != "" and bool(unit) and bool(date_text)
```

Keep zero data values valid. If a required field cannot be derived from the rule, source workbook, or allowed runtime metadata, do not invent it; skip the row or report it in validation rather than returning malformed production records.

Skip rows with no valid data. Preserve numeric values exactly:

- do not round;
- do not format numbers into new precision;
- keep original numeric strings when that is safest.
- remove display thousands separators from numeric strings before returning `indicator_data`, for example `13,509.68` must become `13509.68` and `2,896` must become `2896`.

For Shenzhen Stock Exchange / exchange market-data templates, infer units in this strict order:

1. bracket unit in the current table/header cell;
2. bracket unit in the A-column or indicator-name cell;
3. `倍` for valuation ratio names such as `市盈率` and `市净率`;
4. `%` when the combined header/name contains `率` and it is not a valuation ratio;
5. `-` only when none of the above applies.

Treat these as no data unless the user explicitly says otherwise:

- empty string;
- `-`, `--`, `---`, `—`;
- `#N/A`, `#REF!`;
- `NA`, `N/A`;
- non-numeric text in data cells.

Zero is valid and must be kept.

For exchange or market-data parsers whose rule says the date comes from external `publictime`, normalize it before assigning `end_date`:

```python
def normalize_publictime(value):
    text = raw_text(value)
    if not text:
        return ""
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return parse_common_end_date(text)
```

Do not assign `end_date = publictime or ""` directly, because timestamps may fail downstream date matching/filtering. If `publictime` is missing and the rule provides no other date source, leave `end_date` empty rather than fabricating one.

For common statistical templates, implement a shared period parser before emitting rows. It must handle:

- cumulative month ranges such as `2026年1-6月`, `2026年1至6月`, `1-6月` with a known year, and use the last month;
- single months such as `2026年6月`, `2026.06`, `2026-06`, `202606`, and use month end;
- quarters such as `2026年一季度`, `2026年第2季度`, `2026Q3`, and `2026年1-2季度`, and use quarter end;
- half-year and annual text such as `2026年上半年`, `2026H2`, `2026年全年`, and a single year.

Date priority is: indicator/header-specific period first; then the rule-specified title/file-name source; then allowed external common title fields. Publication dates usually describe article release time and must not override the data period. For example, in a file titled `2026年1-6月...`, `一季度GDP累计值` should still end on `2026-03-31`, while `二季度累计值` or an otherwise unspecified title-level period ends on `2026-06-30`.

## Complete traceability header (mandatory)

Every generated parser must start with this complete traceability header immediately after the UTF-8 coding line. Populate it from `excelrule.xlsx` and the inspected source workbook rather than hand-writing guesses:

```python
# -*- coding: utf-8 -*-
r"""
采集数据源：<采集数据源>
数据来源编码：<数据来源编码>
数据源路径代码：<数据源路径代码>
路径：<路径>
老采集站点名称：<老采集站点名称>
文件名称：<文件名称>
目标表：<目标表；规则表为空时保留标签并留空>
excelrule行号：<row number>
源文件：D:\excel\org\<文件名称>
指标名称示例：<指标名称示例>

拆解规则：
1. 指标名称取值逻辑：...
2. 指标名称标准化处理：...
3. 指标单位取值逻辑：...
4. 指标单位位置：...
5. 截止日期取值逻辑：...
6. 截止日期位置：...
7. 备注/Sheet限制：...
8. 全局规范：...
"""
```

Use `r"""..."""` to avoid Windows path escape warnings. Keep the field order stable. Include every label even when its value is empty. The header must describe the rules actually implemented; if workbook inspection refines row/column details, state that refinement without contradicting the rule workbook. A one-line or abbreviated module docstring is prohibited.

Do not leave mojibake in comments. If working through PowerShell causes Chinese literal corruption, read labels and rule text from `excelrule.xlsx`, or build fixed labels with Unicode escapes.

## Code style

Use one self-contained file. Keep comments in Chinese where they explain rule-specific behavior.

Do not copy one generic config parser into many files unless each generated script is actually checked against its own workbook and rule row.

Avoid shared-runner namespace conflicts. Generated parser source must not contain the token `datetime` anywhere, including imports, aliases, class references, type checks, annotations, comments, or logging helpers.

Handle dates without that dependency:

```python
year = getattr(value, "year", None)
month = getattr(value, "month", None)
day = getattr(value, "day", None)
if all(isinstance(part, int) for part in (year, month, day)):
    text = f"{year:04d}-{month:02d}-{day:02d}"
```

For legacy `.xls` serial dates, call `xlrd.xldate_as_tuple()` and format the returned year/month/day tuple directly. Use `calendar.monthrange()` for month-end calculation. Do not construct a date object merely to format a string.

Prefer explicit helper functions:

- `standardize_text`
- `clean_data_value`
- `parse_end_date`
- `extract_unit`
- `parse_sheet_rows`
- `parse_file_bytes`

Avoid broad helpers that apply unrequested substitutions across all templates.

## Repository hygiene

In the `D:\excel` Git repository, upload parser code only unless the user explicitly asks otherwise:

- stage root `.py` parser scripts;
- stage `.gitignore`;
- do not stage `.artifact_analysis`, `node_modules`, `org`, `excelrule`, `prompt`, `__pycache__`, `.pyc`, or source Excel files.

Recommended `.gitignore` pattern:

```gitignore
*
!/*.py
!/.gitignore
/.artifact_analysis/
/org/
/prompt/
/excelrule/
/__pycache__/
/.venv/
*.pyc
```
