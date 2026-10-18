"""Markdown 分支的可信输入读取；不执行模型或修改规则工作簿。"""
import hashlib
import json
from pathlib import Path
import re
import urllib.request
from html.parser import HTMLParser

from .source_download import download_url, DEFAULT_DOWNLOAD_BASE_URL, NoRedirect, SourceDownloadError


def exported_value(path, key):
    """读取平台导出的 label：JSON；只解码指定字段，避免拼接重复展示值。"""
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
        return data[key] if key in data else data
    except json.JSONDecodeError:
        marker = re.search(r"(?m)^" + re.escape(key) + r"[：:]\s*", text)
        if not marker:
            raise ValueError("导出文件缺少字段: " + key)
        return json.JSONDecoder().raw_decode(text[marker.end():].lstrip())[0]


def start_parameters(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("开始节点须为对象")
        return data
    except json.JSONDecodeError:
        values = {}
        for line in text.splitlines():
            match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)[：:]\s*(.*)$", line)
            if match and match[1] != "label":
                values[match[1]] = match[2]
        return values


def normalize_task(job, resolve):
    original = start_parameters(resolve(job["start_file"])) if job.get("start_file") else {}
    upstream = {**original, **job.get("upstream_metadata", {})}
    merged = {**upstream, **job}
    aliases = ("markdownMD5", "markdownMd5", "markdown_md5", "parseMarkdownMD5", "parseMarkdownMd5")
    ids = {merged[key] for key in aliases if merged.get(key)}
    if len(ids) > 1:
        raise ValueError("Markdown MD5 别名冲突")
    if not ids:
        raise ValueError("缺少 markdownMD5；禁止回退 fileMd5/parseFileMd5")
    merged["markdownMD5"] = ids.pop()
    merged["upstream_metadata"] = upstream
    return merged


def decode_markdown(raw):
    if not raw or len(raw) > 8_000_000:
        raise ValueError("Markdown 为空或超过8MB")
    if raw.startswith((b"PK", bytes.fromhex("D0CF11E0A1B11AE1"), b"%PDF")):
        raise ValueError("Markdown 下载返回了原始文件")
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("无法解码 Markdown")
    is_json = False
    if text.lstrip().startswith(("{", "[")):
        try:
            is_json = isinstance(json.loads(text), (dict, list))
        except json.JSONDecodeError:
            pass
    if not text.strip() or is_json or re.match(r"\s*(?:<!doctype\s+html|<html\b)", text, re.I):
        raise ValueError("正文为空或返回原始 HTML/JSON；不是 Markdown 正文")
    if "\x00" in text:
        raise ValueError("Markdown 包含二进制内容")
    return text


def fetch_bytes(url, timeout=30, max_bytes=8_000_000):
    transport = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open
    try:
        with transport(urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": "ExcelMatchAgent/0.2"}), timeout=timeout) as response:
            if response.status != 200:
                raise SourceDownloadError("下载未返回HTTP 200")
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise SourceDownloadError("下载内容超过大小限制")
        return raw
    except OSError:
        raise SourceDownloadError("正文/网页下载失败，请检查地址及网络；未使用其他MD5兜底") from None


def acquire_markdown(job, resolve, folder):
    md5 = job["markdownMD5"]
    url = download_url(job.get("download_base_url", DEFAULT_DOWNLOAD_BASE_URL), md5)
    if job.get("markdown_path"):
        raw = resolve(job["markdown_path"]).read_bytes()
        origin = "local_markdown"
    else:
        raw = fetch_bytes(url, job.get("download_timeout", 30))
        origin = "download"
    text = decode_markdown(raw)
    (folder / "source.md").write_text(text, encoding="utf-8")
    return text, {"origin": origin, "markdownMD5": md5, "url": url,
                  "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                  "content_md5": hashlib.md5(raw).hexdigest()}


def rule_row(job, resolve):
    if job.get('rule_source') not in (None, 'inline', 'workbook'):
        raise ValueError('rule_source须为inline或workbook')
    if job.get("rule") and job.get('rule_source') != 'workbook':
        return dict(job["rule"])
    from openpyxl import load_workbook
    path = resolve(job.get("excelrule_path", "excelrule/excelrule.xlsx"))
    book = load_workbook(path, read_only=False, data_only=True)
    try:
        sheet = book[job["rule_sheet"]] if job.get("rule_sheet") else book.worksheets[0]
        headers = [cell.value for cell in sheet[1]]
        selected = []
        number = job.get('rule_row')
        if number is not None and (isinstance(number, bool) or not (isinstance(number, int) or isinstance(number, str) and number.isdigit())
                                   or int(number) < 2 or int(number) > sheet.max_row):
            raise ValueError('rule_row超出规则工作表数据范围')
        iterator = sheet.iter_rows(min_row=int(number), max_row=int(number)) if number else sheet.iter_rows(min_row=2)
        for cells in iterator:
            row = {str(key): cell.value for key, cell in zip(headers, cells) if key is not None}
            if job.get("rule_row"):
                take = cells[0].row == int(job["rule_row"])
            elif job.get("path_code"):
                take = str(row.get("数据源路径代码", "")).strip() == str(job["path_code"])
            else:
                take = str(row.get("路径", "")) == job.get("pageTitle")
            if take:
                row["规则表行"] = cells[0].row
                for key, cell in zip(headers, cells):
                    if key == "文件名称" and cell.hyperlink:
                        row["source_url"] = cell.hyperlink.target
                selected.append(row)
        if len(selected) != 1:
            raise ValueError("excelrule 需精确定位唯一规则行，当前数量: " + str(len(selected)))
        row = selected[0]
        if job.get('rule_source') == 'workbook':
            if job.get('pageTitle') and str(row.get('路径', '')).strip() != str(job['pageTitle']).strip():
                raise ValueError('工作簿规则行的路径与开始节点pageTitle不一致，请检查工作表及行号')
            if not row.get('目标模块') and row.get('所属模块'):
                row['目标模块'] = row['所属模块']
            if not row.get('目标表') and row.get('数据目标表'):
                row['目标表'] = row['数据目标表']
            warnings = []
            for key in ('数据来源编码', '数据源路径代码'):
                value = row.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if abs(value) >= 10**15:
                        row[key + '_原始单元格值'] = str(value)
                        row[key] = ''
                        warnings.append(key + '为超过15位的Excel数值，精度无法保证；不猜测末尾数字')
                    elif float(value).is_integer():
                        row[key] = str(int(value))
            overrides = job.get('rule_overrides', {})
            if not isinstance(overrides, dict) or set(overrides) - {'数据来源编码', '数据源路径代码'}:
                raise ValueError('rule_overrides仅允许经独立核实的来源编码/路径代码')
            for key, value in overrides.items():
                if not isinstance(value, str) or not value.isdigit():
                    raise ValueError('规则编码覆盖值必须是精确数字文本')
                row[key] = value
            wanted = {'规则表行','数据来源编码','采集数据源','数据源路径代码','指标名称示例','路径','老采集站点名称',
                      '文件名称','指标名称取值逻辑','指标取值逻辑','指标名称标准化处理','指标单位取值逻辑',
                      '指标单位位置','截止日期取值逻辑','截止日期位置','目标模块','目标表','所属模块','数据目标表',
                      '备注','是否通过初步测试','文件类型','source_url','数据来源编码_原始单元格值','数据源路径代码_原始单元格值'}
            row = {key: value for key, value in row.items() if key in wanted}
            row.update(规则来源工作簿=str(path), 规则来源工作表=sheet.title)
            if warnings:
                row['规则读取警告'] = warnings
            if overrides:
                row['编码修复依据'] = job.get('rule_override_provenance', '任务配置提供的精确编码文本')
        return row
    finally:
        book.close()


def regex_rules(job, row, resolve):
    from openpyxl import load_workbook
    code = str(row.get("数据源路径代码", "")).strip()
    if not code:
        raise ValueError("正则任务缺少数据源路径代码")
    new = resolve(job.get("regex_path", "excelname拼接规则/任务汇总总表0911.xlsx"))
    old = resolve(job.get("legacy_regex_path", "excelname拼接规则/指标名称正则逻辑_分路径代码(2).xlsx"))
    found, result = False, []
    if new.is_file():
        book = load_workbook(new, read_only=True, data_only=True)
        try:
            if "汇总" in book.sheetnames:
                for number, cells in enumerate(book["汇总"].values, 1):
                    if len(cells) > 10 and str(cells[2]).strip() == code:
                        found = True
                        selectors = json.loads(cells[10] or "[]")
                        for selector in selectors:
                            if selector.get("selector_type") == "regex":
                                for pattern in selector.get("rules", []):
                                    result.append({"indicator_name": cells[5], "pattern": pattern, "row": number})
        finally:
            book.close()
    if found:
        source = {"path": str(new), "sheet": "汇总", "format": "selectors"}
    else:
        if not old.is_file():
            raise ValueError("无正则拼接规则")
        book = load_workbook(old, read_only=True, data_only=True)
        try:
            if code in book.sheetnames:
                for number, cells in enumerate(book[code].values, 1):
                    if len(cells) > 1 and cells[0] and cells[1] and str(cells[0]) != "indicator_name":
                        result.append({"indicator_name": cells[0], "pattern": cells[1], "row": number})
        finally:
            book.close()
        source = {"path": str(old), "sheet": code, "format": "legacy"}
    if not result:
        raise ValueError("无可用正则；禁止用相近任务或混用两套规则")
    for item in result:
        if not isinstance(item["indicator_name"], str) or not isinstance(item["pattern"], str):
            raise ValueError("正则名称及模式须为字符串")
        re.compile(item["pattern"])
    return {**source, "rules": result}


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def page_evidence(job, row, resolve):
    url = row.get("source_url") or job.get("source_url")
    if not url and str(row.get("文件名称", "")).startswith(("https://", "http://")):
        url = row["文件名称"]
    if not url:
        raise ValueError("HTML 设计核验缺少规则行原网页链接")
    download_url(url, "check")  # 复用 HTTP(S) 地址和认证信息检查，不请求拼接后的URL。
    try:
        raw = resolve(job["webpage_path"]).read_bytes() if job.get("webpage_path") else fetch_bytes(url, job.get("download_timeout", 30))
    except SourceDownloadError:
        raise SourceDownloadError("原网页无法访问，HTML设计核验尚未完成；未用Markdown冒充网页证据", "ORIGINAL_WEBPAGE_UNAVAILABLE") from None
    page = raw.decode("utf-8", errors="replace")
    parser = PageText()
    parser.feed(page)
    text = "\n".join(parser.parts)
    if not text.strip():
        raise SourceDownloadError("原网页没有可读正文，不能确认设计证据", "ORIGINAL_WEBPAGE_UNAVAILABLE")
    return {"url": url, "text": text, "sha256": hashlib.sha256(raw).hexdigest()}
