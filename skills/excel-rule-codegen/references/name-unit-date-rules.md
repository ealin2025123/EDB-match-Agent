# Name, unit, and date rules

## Indicator name rules

Build `indicator_name` exactly as described by the rule workbook.

Common patterns seen in this workspace:

- `A列 + B3/C3`
- `A列从外层取到最小一层 + 表头`
- `sheet名称 + A列从外层取到最小一层`
- `指标列 + 其余列名`
- `A1 + A列 + 多行表头`
- `sheet内独立表格表名 + 指标列 + 列名`
- `路径/目标表 + 行列组合`

Do not add `sheet_name` unless the rule says so, the indicator-name example requires it, or the user explicitly corrects the rule.

Do not use external `title`, `fileName`, `filename`, `pageTitle`, or article/webpage titles as an indicator-name prefix or sheet-name substitute unless the rule explicitly says so. Runtime title values can change by platform, disclosure title, or page metadata and must normally be used only for date parsing. For CSV files with no real sheet name, use the rule workbook's filename stem, target-table name, or another explicit rule-provided pseudo sheet name; never fall back to arbitrary runtime `title`.

For hierarchy parsing:

- Parent rows often have labels but no data; child rows often have data.
- Maintain a stack from indentation, numbering, outline level, or observed table structure.
- Correct order often matters, such as `一` → `(一)` → `1` → `(1)`.
- Same-level nodes must replace each other; do not concatenate siblings such as `城市农村` or `猪肉牛肉`.
- Some rows that look like parents are actually child nodes. For example, the user clarified that `#轻工业` is not a parent in one industrial table.
- Do not stop hierarchy at the first data row if deeper rows still inherit parents.

## Name normalization

Apply only the normalization listed in the rule workbook and global rules.

Common allowed operations:

- full-width to half-width with `unicodedata.normalize("NFKC", text)`;
- remove spaces and line breaks;
- remove Chinese enumeration punctuation such as `、` when global rules say so;
- remove English comma and single quote when global rules say so;
- lowercase English only when the rule says English uppercase to lowercase;
- remove leading numbering only when the rule says to remove leading numbers.

Do not remove Chinese comma unless the global rule says so.

## Do not delete name content

Hard rule: do not delete content from `indicator_name` merely because it is also useful as a unit.

Examples that must remain in names unless the rule explicitly says to delete them:

- `同比增长(%)`
- `增长速度(%)`
- `规模以上工业产值(万元)`
- `商品房销售面积(万平方米)`

Unit extraction may return `%`, `万元`, or `万平方米`, but the name should still contain `(%)`, `(万元)`, or `(万平方米)` unless deletion is explicitly requested.

Avoid dangerous name-building patterns:

- `text[:match.start()]`
- `match.start()` used to truncate names
- `replace("%", "")`
- `name_without_unit`
- `cleaned_name`
- broad `re.sub(...)` that removes bracket content from names

If such code exists, verify that it only removes a leading row number required by the rule.

## Header replacement

Only apply replacements explicitly written in the rule workbook.

Examples:

- `1-\d+月` → `累计值`
- `1-\d+月累计` → `累计值`
- `\d+月实际` → `当期值`
- `1-\d+月同比` → `累计同比`
- `\d+年.*?` → `当期值`
- `.*?季度` → `累计值`
- `1-\d+季` → `累计值`

If the rule only says `指标列 + 其余列名`, do not invent replacements such as:

- `本月` → `当期值`
- `本季` → `当期值`
- `累计` → `累计值`

Special case already clarified: for resident consumer price data, rules may require `累计同比涨跌幅(%)`, not `累计值同比涨跌幅(%)`.

## Unit rules

Use the rule workbook first. If it gives priority, follow it. Otherwise use this default priority:

1. Row-level independent unit column, such as `计量单位`.
2. Unit inside the indicator-name brackets.
3. Unit inside the column header brackets.
4. Table-level declaration, such as `单位：亿元`.
5. Nearest valid parent-node unit.
6. Fixed unit from the rule workbook.

When extracting bracket units, use the last bracket pair if multiple bracket pairs exist.

Percent rules:

- Headers containing `%`, `同比±%`, `增长%`, or `同比增长(%)` generally use unit `%`.
- Keep `(%)` in `indicator_name` unless deletion is explicitly requested.

If a row has no valid data, do not output it.

## 通用日期处理方法

This is the shared end-date method discussed with the user. Make it broad, but never fabricate dates.

Empty `end_date` values cause downstream GZK matching failure. For monthly, quarterly, half-year, and annual rules, generated parsers must actively derive a period-end date from allowed sources instead of returning a table full of blank dates. Records still must not use invented dates: if no allowed source yields a date, skip the affected production row or expose the issue during validation.

### 1. Date source priority

Use the date source specified by the rule row, but prefer the most specific data-period evidence available for each indicator. Common sources:

1. explicit date cell in a row/column header;
2. row-level remark/date column, such as E-column remarks;
3. indicator-name or column-label text such as `一季度累计值`, `二季度累计值`, `1-6月累计`, or `6月当期`;
4. rule-specified source title/file name, such as `2026年1-6月...`, when the indicator itself does not contain a more specific period;
5. external common title fields from `params`, only when rule context permits them as date metadata;
6. current year only when the rule explicitly says “年份取当年/当前年”.

If the rule says row remark overrides title, use the remark for that row first. If the remark has no valid date, fall back to the rule-specified default source.

Do not use a sample template file name as a fallback. Publication or release dates from HTML, such as `发布日期 2026-07-15`, usually describe when the page was published and should be ignored when the indicator/header/title already identifies the reporting period.

Examples:

- title `2026年1-6月滕州市...` + indicator `一季度GDP累计值` → `2026-03-31`;
- title `2026年1-6月滕州市...` + indicator `二季度累计值` → `2026-06-30`;
- title `2026年1-6月滕州市...` + indicator with no specific period → `2026-06-30`.

If no valid date can be derived, return `""` from the date helper and do not treat the affected production row as a valid matched output.

### 2. Frequency first, then period end

Infer or obey the disclosure frequency before normalizing:

- daily: keep the exact day if the rule says to keep daily dates;
- monthly or cumulative monthly: use the last day of the selected month;
- quarterly: use the last day of the selected quarter;
- half-year: use `06-30` or `12-31`;
- yearly: use `12-31`.

When the rule only says “通用日期” for economic monthly/quarterly disclosures, default to period end, not the first day.

### 3. Month and cumulative-month patterns

Convert recognized monthly periods to month end:

- `2026年5月` → `2026-05-31`
- `2026年05月份` → `2026-05-31`
- `2026年五月` → `2026-05-31`
- `2026年1-5月` → `2026-05-31`
- `2026年1至5月` → `2026-05-31`
- `2026年1到5月` → `2026-05-31`
- `2026年1—5月` → `2026-05-31`
- `2026年1~5月` → `2026-05-31`
- `2026年1月至5月` → `2026-05-31`
- `2026年前5个月` → `2026-05-31`
- `2026年前五月` → `2026-05-31`
- `1-5月` + title year `2026` → `2026-05-31`
- `1—5月份` + title year `2026` → `2026-05-31`
- `截至2026年5月底` → `2026-05-31`
- `2026年5月末` / `2026年5月底` → `2026-05-31`

For ranges, always use the last month. Do not parse `2026年1-4月` as January.

### 4. Numeric year-month patterns

Recognize numeric formats:

- `2026.04` → `2026-04-30`
- `2026.4` → `2026-04-30`
- `2026/04` → `2026-04-30`
- `2026/4` → `2026-04-30`
- `2026-04` → `2026-04-30`
- `2026-4` → `2026-04-30`
- `202604` → `2026-04-30` when the rule context is monthly and the string is clearly `YYYYMM`;
- `2026M04` / `2026-M04` → `2026-04-30`.

Avoid interpreting arbitrary six-digit codes as dates unless the rule context or header position makes date usage clear.

### 5. Quarter patterns

Recognize Chinese and English quarter formats:

- `2026年一季度` → `2026-03-31`
- `2026年二季度` → `2026-06-30`
- `2026年三季度` → `2026-09-30`
- `2026年四季度` → `2026-12-31`
- `2026年1季度` / `2026年第1季度` / `2026年第一季度` → `2026-03-31`
- `1984Q1` → `1984-03-31`
- `2026Q3` → `2026-09-30`
- `2026年1-2季度` → `2026-06-30`

For “本季” columns, only output them when the rule says they are valid. Example from the discussion: some templates should output “本季” only when the collected month is 3, 6, 9, or 12; otherwise skip only the “本季” data and its adjacent同比增长 column, not unrelated columns.

### 6. Half-year and year patterns

Recognize:

- `2026年上半年` → `2026-06-30`
- `2026年下半年` → `2026-12-31`
- `2026H1` → `2026-06-30`
- `2026H2` → `2026-12-31`
- single year `2025` → `2025-12-31`
- title/header with only year → `YYYY-12-31`
- `2025年全年` / `2025年度` → `2025-12-31`

Default to natural year unless the rule explicitly says fiscal year.

### 7. Exact-day and custom-formatted Excel dates

This is important for Excel cells with custom display formats.

When reading `.xlsx`, capture both:

- `cell.value`, which may be a date-like object exposing `year`/`month`/`day`, text, or an Excel serial number;
- `cell.number_format`, because the displayed text may be monthly/quarterly even when the raw date is the first day.

Rules:

- If the rule says the disclosure is monthly and `cell.value` is `2026-01-01`, normalize to `2026-01-31`.
- If the rule says month end and the displayed value looks like `2026-1-1`, `2026/1/1`, or `2026年1月1日`, use `2026-01-31`.
- If the rule says daily or “具体到日”, keep the exact day, e.g. `2026-01-01`.
- If the cell is an Excel serial in a plausible date range, convert it using Excel’s date system, then apply the frequency rule.
- For `.xls`, use `xlrd.xldate_as_tuple(value, datemode)` when the cell type/format indicates a date, then format the tuple directly without constructing a date object.

This avoids the bug where a displayed monthly date such as `2026-1-1` incorrectly falls back to title date or stays as the first day.

### 8. English month patterns

Recognize English month names when they appear in international workbooks:

- `May 2026` → `2026-05-31`
- `2026 May` → `2026-05-31`
- `Jan 2026` → `2026-01-31`

Only use these when the cell/header is clearly a date label.

### 9. Implementation notes

- Normalize separators before matching: `—`, `–`, `至`, `到`, `~`, `/`, `.`, and `-`.
- Normalize Chinese numerals for months/quarters: `一` to `十二`, `第一季度`, `一季度`.
- Use `calendar.monthrange(year, month)[1]` for month end.
- Keep a helper such as `normalize_to_period_end(year, month=None, quarter=None, half=None, day=None, frequency="month")`.
- Return `""` when no valid date is available.
- Never use a template file name or hardcoded sample title as a fallback date.
