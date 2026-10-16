"""父进程调用的解析执行器；支持本地Python和可选Docker模式。"""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import sys

ALLOWED_MODULES = {"calendar", "collections", "decimal", "itertools", "json", "math", "re", "statistics", "unicodedata"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "input", "__import__", "globals", "locals", "vars", "getattr",
                   "setattr", "delattr", "breakpoint", "help", "exit", "quit", "memoryview", "type", "object"}


class LimitedLog(io.StringIO):
    def write(self, message):
        if self.tell() + len(message) > 100000:
            raise ValueError("解析器日志超过100000字符")
        return super().write(message)


def check_code(code):
    tree = ast.parse(code)
    if not any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in tree.body):
        raise ValueError("生成代码缺少 main(params)")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else [(node.module or "").split(".")[0]]
            if getattr(node, "level", 0) or any(name not in ALLOWED_MODULES for name in names):
                raise ValueError("解析器只能导入计算/文本处理白名单模块")
        if isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("__")):
            raise ValueError(f"解析器禁止访问 {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("解析器禁止访问私有属性或反射接口")
    return tree


def invoke(code, params):
    tree = check_code(code)
    namespace = {"__name__": "generated_parser"}
    # 白名单仅是代码检查。本地Python模式不提供系统级文件/网络隔离。
    with contextlib.redirect_stdout(LimitedLog()):
        exec(compile(tree, "generated_parser.py", "exec"), namespace)
        result = namespace["main"](params)
    if isinstance(result, dict):
        if result.get("code", 0) != 0:
            raise ValueError(str(result.get("msg", "解析器返回失败")))
        result = result.get("ExcelData")
    if not isinstance(result, list) or len(result) > 100000:
        raise ValueError("解析器必须返回ExcelData列表，且最多100000条")
    return result


if __name__ == "__main__":
    try:
        code = Path(sys.argv[1]).read_text(encoding="utf-8")
        params = json.load(sys.stdin)
        os.environ.clear()
        rows = invoke(code, params)
        print(json.dumps({"ExcelData": rows}, ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
