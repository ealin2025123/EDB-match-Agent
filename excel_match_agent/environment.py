"""读取项目内DeepSeek配置；不输出变量值，不执行插值或.env中的命令。"""
import os
from pathlib import Path


def load_environment(path=None, environ=None):
    path = Path(path) if path is not None else (Path.cwd() / ".env" if (Path.cwd() / ".env").is_file() else Path(__file__).with_name(".env"))
    environ = os.environ if environ is None else environ
    allowed = {"DEEPSEEK_API_KEY", "CUSTOM_LLM_API_TOKEN", "DEEPSEEK_API_KEY_ENV", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"}
    if not path.is_file():
        return
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f".env第{line_number}行应使用NAME=VALUE格式")
        name, value = (part.strip() for part in line.split("=", 1))
        if name not in allowed:
            continue
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f".env第{line_number}行引号未闭合")
            value = value[1:-1]
        if not environ.get(name) and value:
            environ[name] = value
