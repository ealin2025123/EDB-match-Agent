"""按上游 filemd5 下载源文件；与模型 API 认证、生成代码执行完全分开。"""
from email.message import Message
import hashlib
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_DOWNLOAD_BASE_URL = "https://files.example.invalid/api/file/download"
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


class SourceDownloadError(RuntimeError):
    def __init__(self, message, code="SOURCE_DOWNLOAD_FAILED"):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def download_url(base_url, filemd5):
    if not isinstance(base_url, str):
        raise SourceDownloadError("下载地址必须为HTTP(S)字符串", "SOURCE_DOWNLOAD_CONFIG_INVALID")
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise SourceDownloadError("下载地址须为无认证信息、查询参数或片段的HTTP(S)地址", "SOURCE_DOWNLOAD_CONFIG_INVALID")
    if not isinstance(filemd5, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", filemd5):
        raise SourceDownloadError("自动下载需要有效filemd5标识（字母、数字、下划线或连字符）", "SOURCE_DOWNLOAD_CONFIG_INVALID")
    return base_url.rstrip("/") + "/" + urllib.parse.quote(filemd5, safe="")


def safe_filename(name, fallback):
    # 服务端文件名只用于当前任务目录，不能作为任意本机路径。
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:180]
    if not name or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        return fallback
    return name


def download_source(filemd5, folder, *, base_url=DEFAULT_DOWNLOAD_BASE_URL, filename_hint=None,
                    timeout=30, max_bytes=MAX_DOWNLOAD_BYTES, transport=None):
    url = download_url(base_url, filemd5)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise SourceDownloadError("download_timeout须为正数秒数", "SOURCE_DOWNLOAD_CONFIG_INVALID")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise SourceDownloadError("download_max_bytes须为正整数", "SOURCE_DOWNLOAD_CONFIG_INVALID")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / "source.download.part"
    # 内网下载不继承环境代理、DeepSeek Key或模型请求的认证头。
    transport = transport or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream, */*"}, method="GET")
    began = time.monotonic()
    try:
        with transport(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                raise SourceDownloadError("源文件下载未返回HTTP 200")
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise SourceDownloadError("源文件超过download_max_bytes限制", "SOURCE_DOWNLOAD_TOO_LARGE")
            disposition = Message()
            disposition["Content-Disposition"] = response.headers.get("Content-Disposition", "")
            header_name = disposition.get_filename()
            if header_name:
                header_name = urllib.parse.unquote(header_name)
                # 部分内网服务把UTF-8中文直接放进HTTP头，标准读取后会呈Latin-1乱码。
                try:
                    header_name = header_name.encode("latin-1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            # 若服务端只有MD5名称，优先用规则表的真实报表名称保留期间信息。
            header_stem = Path(str(header_name or "")).stem
            if not header_name or header_stem.lower() == filemd5.lower():
                name = filename_hint or header_name or filemd5
            else:
                name = header_name
            name = safe_filename(name, filemd5)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
            total, prefix = 0, b""
            digest, md5 = hashlib.sha256(), hashlib.md5()
            with partial.open("wb") as output:
                while True:
                    if time.monotonic() - began >= timeout:
                        raise SourceDownloadError("源文件下载超时")
                    chunk = response.read(min(256 * 1024, max_bytes - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise SourceDownloadError("源文件超过download_max_bytes限制", "SOURCE_DOWNLOAD_TOO_LARGE")
                    prefix = (prefix + chunk)[:8]
                    digest.update(chunk)
                    md5.update(chunk)
                    output.write(chunk)
            if not total:
                raise SourceDownloadError("下载返回空文件", "SOURCE_DOWNLOAD_INVALID")
            if declared and declared.isdigit() and total != int(declared):
                raise SourceDownloadError("源文件下载不完整，与Content-Length不符", "SOURCE_DOWNLOAD_INVALID")
            if prefix.startswith(b"PK"):
                suffix = ".xlsx"
            elif prefix.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
                suffix = ".xls"
            elif content_type in {"application/json", "text/html", "application/problem+json"}:
                raise SourceDownloadError("接口返回JSON/HTML而非源表格，请检查filemd5及接口权限", "SOURCE_DOWNLOAD_INVALID")
            elif Path(name).suffix.lower() in {".csv", ".tsv"}:
                suffix = Path(name).suffix.lower()
            elif content_type in {"text/csv", "application/csv", "text/tab-separated-values"}:
                suffix = ".tsv" if content_type == "text/tab-separated-values" else ".csv"
            else:
                raise SourceDownloadError("下载内容无法识别为xlsx/xls/csv/tsv；请提供真实文件名或检查接口", "SOURCE_DOWNLOAD_INVALID")
            if Path(name).suffix.lower() != suffix:
                name = (Path(name).stem or filemd5) + suffix
            path = folder / name
            partial.replace(path)
            content_md5 = md5.hexdigest()
            return path, {"origin": "download", "url": url, "path": str(path), "source_name": name,
                          "bytes": total, "sha256": digest.hexdigest(), "content_md5": content_md5,
                          "md5_matches_identifier": content_md5.lower() == filemd5.lower()
                          if re.fullmatch(r"[0-9a-fA-F]{32}", filemd5) else None}
    except urllib.error.HTTPError as exc:
        raise SourceDownloadError(f"源文件下载返回HTTP {exc.code}，请检查filemd5、接口及内网访问权限") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SourceDownloadError("源文件下载连接失败、超时或无法保存，请检查内网连通及下载目录") from None
    finally:
        partial.unlink(missing_ok=True)
