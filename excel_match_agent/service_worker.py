"""HTTP任务独立工作进程；复用原Agent，不改变生成、审核和重建规则。"""
import json
from pathlib import Path
import sys


def main():
    from .app import run_job
    request_path, result_path = map(Path, sys.argv[1:3])
    payload = json.loads(request_path.read_text(encoding='utf-8'))
    result = run_job(payload['job'], Path(payload['job_base']))
    temporary = result_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(result_path)


if __name__ == '__main__':
    main()
