# 开始节点后使用；只手填pageTitle/fileMd5，不覆盖平台自动补齐的元数据。
import json
import re


def main(params):
    if isinstance(params,str):
        params = json.loads(params)
    page = params.get('pageTitle') or params.get('pagetitle')
    md5 = params.get('fileMd5') or params.get('filemd5')
    if not isinstance(page,str) or not page.strip():
        raise ValueError('MISSING_PAGE_TITLE:开始节点缺少pageTitle')
    if not isinstance(md5,str) or not re.fullmatch(r'[a-fA-F0-9]{32}',md5.strip()):
        raise ValueError('INVALID_FILE_MD5:fileMd5须为32位原文件MD5')
    return {'pageTitle':page.strip(),'fileMd5':md5.strip()}
