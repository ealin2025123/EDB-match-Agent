"""扫描工作树中的凭据/私有路径和打包禁入文件，只报告文件名与规则，不回显敏感值。"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', '__pycache__', 'build', 'dist', '.pytest_cache'}
TOKEN_PATTERNS = [re.compile(r'gh[pousr]_[A-Za-z0-9_]{20,}'),
                  re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),
                  re.compile(r'sk-[A-Za-z0-9]{20,}'),
                  re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')]
PRIVATE_PATTERNS = [re.compile(r'\b10\.(?:\d{1,3}\.){2}\d{1,3}\b'),
                    re.compile(r'\b192\.168\.(?:\d{1,3}\.)\d{1,3}\b'),
                    re.compile(r'\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b'),
                    re.compile(r'[A-Za-z]:[/\\]+Users[/\\]+[^/\\\s]+', re.I)]


def scan(secret_env=None):
    secrets = []
    if secret_env:
        for line in Path(secret_env).read_text(encoding='utf-8-sig').splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            value = value.strip().strip('"\'')
            if not key.strip().upper().endswith('_ENV') and any(marker in key.upper() for marker in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')) and len(value) >= 12:
                secrets.append(value)
    failures, checked = [], 0
    if (ROOT / '.git').exists():
        tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True).stdout.decode('utf-8').split('\0')
        for name in filter(None, tracked):
            path = Path(name)
            if ((path.name.startswith('.env') and path.name != '.env.example')
                    or any(part in {'runs', 'jobs', 'private', '.venv', '__pycache__'} for part in path.parts)
                    or path.suffix in {'.log', '.pem', '.key', '.zip'} or path.name.endswith('.private.json')):
                failures.append({'file': name, 'rule': 'local-only artifact is tracked by Git'})
    for path in ROOT.rglob('*'):
        relative = path.relative_to(ROOT)
        if not path.is_file() or any(part in SKIP or part.endswith('.egg-info') for part in relative.parts):
            continue
        if path.name.startswith('.env') and path.name != '.env.example':
            # 本机.env允许存在，但不能被Git跟踪；上传清单须另外核对。
            continue
        if any(part in {'runs', 'jobs', 'private'} for part in relative.parts) or path.suffix in {'.log', '.pem', '.key', '.zip'}:
            continue
        if path.name.endswith('.private.json'):
            continue
        checked += 1
        raw = path.read_bytes()
        contents = [raw.decode('utf-8', errors='ignore')]
        if path.suffix == '.xlsx':
            with zipfile.ZipFile(path) as book:
                contents += [book.read(name).decode('utf-8', errors='ignore') for name in book.namelist() if name.endswith('.xml')]
        text = '\n'.join(contents)
        if any(value in text for value in secrets) or any(pattern.search(text) for pattern in TOKEN_PATTERNS):
            failures.append({'file': relative.as_posix(), 'rule': 'credential-like content'})
        if any(pattern.search(text) for pattern in PRIVATE_PATTERNS):
            failures.append({'file': relative.as_posix(), 'rule': 'private endpoint or personal absolute path'})
    print(json.dumps({'checked_files': checked, 'findings': failures, 'status': 'pass' if not failures else 'fail'}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--secrets-env', type=Path, help='Optional local env used only for exact-value leak checking; values are not printed')
    sys.exit(scan(parser.parse_args().secrets_env))
