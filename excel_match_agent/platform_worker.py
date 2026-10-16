"""无模型认证环境的完整平台入口测试进程。核心已在父进程检查，适配层校验来源。"""
import contextlib
import io
import json
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    code_path, input_path, output_path = map(Path, sys.argv[1:])
    params = json.loads(input_path.read_text(encoding='utf-8'))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = runpy.run_path(str(code_path))['main'](params)
    except Exception as exc:
        result = {'code': -99, 'msg': '平台入口执行失败：' + str(exc)[:1000], 'ExcelData': []}
    output_path.write_text(json.dumps(result, ensure_ascii=False, default=str, allow_nan=False), encoding='utf-8')
