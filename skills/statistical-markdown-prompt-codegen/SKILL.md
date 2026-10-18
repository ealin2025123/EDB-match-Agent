---
name: statistical-markdown-prompt-codegen
description: Generate source-specific Markdown statistical-table extraction prompts from designated rows of the rule workbook. Use when the user requests prompts that map source-table data to GZK records rather than Python parser code.
---

# Statistical Markdown Prompt Codegen

Generate one Markdown prompt per requested rule row. The prompt guides a model to extract `out_data` from received Markdown and uniquely match records against `GZK.GZKJG`.

## Traceability comment

Start every generated prompt with an HTML comment copied from the rule row in this exact field order:

```markdown
<!--
规则表行：
数据源路径编码：
数据来源编码：
采集数据源：
路径：
老采集站点名称：
目标表：
目标模块：
源文件：
MD5：
-->
```

Populate values directly from the rule workbook. Keep a field with an empty value when the rule cell is empty. This is traceability metadata, not an extraction instruction.

## Scope

- Use this skill for prompt generation, not for Python parser generation.
- Read the requested rule rows first. Treat indicator-name logic, normalization, unit logic, date logic, target module, and remark as the source of truth.
- Inspect the matching source artifact whenever available to confirm table titles, hierarchy, merged headers, units, periods, and exclusions.
- If the row's indicator-name logic is unrelated to regular expressions, use the standard table-based prompt. Do not read or cite the path-specific regex workbook.

## Prompt requirements

Every prompt must:

1. State the input contract: Markdown, `title`, and `GZK.GZKJG`.
2. Explain the actual table scope. A nonempty rule-row remark limits extraction; an empty remark means parse all meaningful source content.
3. Define candidate-name construction from observed row hierarchy and effective leaf headers, including any row-specific replacements.
4. Require identical normalization for candidates and `rule_indicator_name`, then output only unique matches. `indicator_name` and `indicator_code` must come from the same GZK record.
5. Bind data, unit, and date to the same source cell and effective headers. Preserve zeroes, negatives, and source precision; never calculate, fill, or guess.
6. Require the JSON contract:

```json
{"out_data":[{"indicator_code":"","indicator_name":"","indicator_data":"","indicator_unit":"","end_date":""}]}
```

Use `{"out_data":[]}` when no eligible unique match exists.

DeepSeek must distinguish same-indicator naming errors from clearly different statistical scopes. GZK names and codes are authoritative only after semantic identity is established. Exclude clearly different currency, population, hierarchy, or period scopes and continue extracting other indicators without requesting confirmation. For example, total domestic-and-foreign-currency loans cannot match foreign-currency-only loans or their growth rates. Record these GZK items as not_in_source in the independent coverage review, not missing or uncertain. Convertible units alone do not establish a scope difference. Escalate only when evidence is genuinely insufficient to decide identity.

## Date and unit rules

- Derive `end_date` from the individual indicator's own statistical period before using a document-level title. Convert months, quarters, half-years, and years to their period end.
- Publication dates and `publictime` are not statistical periods.
- Read units from the same row or effective column header. Rate columns use `%` only when their table/header establishes a percentage. `百分点` is distinct from `%`.
- If a required period cannot be determined from the source or the permitted external title, do not output the record rather than inventing a date.

## Privacy and portability

The traceability HTML comment may contain the rule-row business path, source filename, and MD5. The executable prompt body below that comment must not contain Windows local directories, absolute filesystem paths, local URLs, or workspace-only operational instructions. Translate needed extraction detail into business table titles, column labels, hierarchy rules, unit rules, period rules, and scope restrictions.

## Quality check

Before delivery, confirm that every prompt starts with a complete traceability comment; that its executable body has no Windows local path; that it is scoped to the rule-row remark; and that it contains no regex-workbook dependency when the row is non-regex. Save prompts in the user-requested output folder without altering existing prompt folders.
