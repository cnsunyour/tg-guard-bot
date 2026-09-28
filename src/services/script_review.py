"""管理员验证脚本的静态风险审查（上传前置门槛，allowlist 制）。

定位：**审计辅助与提前报错，不是安全边界**——静态检查能拦截低级风险与
笔误（含行号报错），动态构造的绕过（getattr 字符串、混淆编码）由沙盒
容器边界兜底。与沙盒运行时的 import 白名单（sandbox/harness_python.py
_ALLOWED_MODULES）共享同一 allowlist，两处改动必须同步。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

# 与 sandbox/harness_python.py 的 _ALLOWED_MODULES 同步维护
ALLOWED_PYTHON_MODULES: frozenset[str] = frozenset(
    {
        "math",
        "random",
        "re",
        "json",
        "string",
        "hashlib",
        "hmac",
        "base64",
        "unicodedata",
        "datetime",
        "calendar",
        "itertools",
        "functools",
        "collections",
        "heapq",
        "bisect",
        "textwrap",
        "difflib",
        "statistics",
        "decimal",
        "fractions",
        "copy",
        "operator",
    }
)

# 危险内建调用：反射逃逸面 / IO / 执行入口（合法验证脚本无需任何一项）
_FORBIDDEN_BUILTINS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
    }
)

# 未导入即引用的敏感模块名（静态报错比运行时 NameError 更友好）。
# 内含危险内建名：`f = eval` 这类别名间接调用经 Name 引用即可静态拦截
_SENSITIVE_NAMES: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "socket",
        "subprocess",
        "shutil",
        "pathlib",
        "io",
        "urllib",
        "http",
        "requests",
        "httpx",
        "ctypes",
        "cffi",
        "threading",
        "multiprocessing",
        "signal",
        "gc",
        "pty",
        "importlib",
        "builtins",
        "runpy",
        "code",
        "codeop",
        "compileall",
        "pickle",
        "shelve",
        "marshal",
        # 危险内建：出现任何形式的引用（含别名赋值 `f = eval`）直接报错
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "input",
    }
)

# JavaScript 高危模式（词法级粗检；字符串字面量中的同形词可能误报，
# fail-closed 方向的误报让管理员改写表达方式即可）
_JS_RULES: tuple[tuple[str, str], ...] = (
    (r"\brequire\s*\(", "不允许 require/import（脚本只用语言内置全局）"),
    # 直接禁 import 关键字：默认导入（import x from）/命名导入/动态 import() 全覆盖
    (r"\bimport\b", "不允许 require/import（脚本只用语言内置全局）"),
    (r"\bexport\s+", "请使用 module.exports 导出，不支持 ESM 语法"),
    (r"\beval\s*\(", "不允许 eval"),
    (r"\bnew\s+Function\b", "不允许动态构造函数"),
    (r"\bFunction\s*\(\s*[\"']", "不允许动态构造函数"),
    (r"\bprocess\s*\.", "不允许访问 process"),
    (r"\bglobalThis\b", "不允许访问 globalThis"),
    (r"\bfetch\s*\(", "不允许网络访问"),
    (r"\bXMLHttpRequest\b", "不允许网络访问"),
    (r"\bWebSocket\b", "不允许网络访问"),
    (r"\bBuffer\b", "不允许访问 Buffer"),
    (r"\bsetTimeout\s*\(|\bsetInterval\s*\(", "脚本是同步执行的，不支持定时器"),
    (r"\bAtob\b|\bbtoa\b|\batob\s*\(|\bbtoa\s*\(", "不允许 Base64 转换（常见混淆手段）"),
)

_JS_LINE_COMMENT = re.compile(r"//[^\n]*")
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass(frozen=True, slots=True)
class ScriptViolation:
    """一条静态审查违规：行号（1 起）+ 分类 + 面向管理员的说明。"""

    line: int
    kind: str
    message: str


def review_python(source: str) -> list[ScriptViolation]:
    """AST 级审查 Python 脚本，返回违规列表（空列表 = 通过）。"""
    violations: list[ScriptViolation] = []
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        return [
            ScriptViolation(line=exc.lineno or 1, kind="syntax", message=f"语法错误: {exc.msg}")
        ]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in ALLOWED_PYTHON_MODULES:
                    violations.append(
                        ScriptViolation(
                            line=node.lineno,
                            kind="import",
                            message=f"模块不在允许列表: {alias.name}",
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if node.level or root not in ALLOWED_PYTHON_MODULES:
                violations.append(
                    ScriptViolation(
                        line=node.lineno,
                        kind="import",
                        message=f"模块不在允许列表: {node.module or '相对导入'}",
                    )
                )
        elif isinstance(node, ast.Call):
            name = _called_name(node.func)
            if name in _FORBIDDEN_BUILTINS:
                violations.append(
                    ScriptViolation(line=node.lineno, kind="call", message=f"不允许调用 {name}()")
                )
        elif isinstance(node, ast.Attribute):
            # dunder 属性访问是 CPython 对象图逃逸的标准起点，全部拒绝
            if node.attr.startswith("__") and node.attr.endswith("__"):
                violations.append(
                    ScriptViolation(
                        line=node.lineno,
                        kind="dunder",
                        message=f"不允许访问双下划线属性 .{node.attr}",
                    )
                )
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in _SENSITIVE_NAMES:
                violations.append(
                    ScriptViolation(
                        line=node.lineno,
                        kind="sensitive_name",
                        message=f"引用了敏感模块名 {node.id}（且该模块不允许导入）",
                    )
                )
    return violations


def review_javascript(source: str) -> list[ScriptViolation]:
    """词法级粗检 JavaScript 脚本；先剥离注释再逐规则匹配。"""
    stripped = _JS_LINE_COMMENT.sub("", source)
    # 块注释用等量换行回填，保证后续违规行号不偏移
    stripped = _JS_BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), stripped)
    violations: list[ScriptViolation] = []
    for lineno, line in enumerate(stripped.splitlines(), start=1):
        for pattern, message in _JS_RULES:
            if re.search(pattern, line):
                violations.append(
                    ScriptViolation(line=lineno, kind="js_forbidden", message=message)
                )
    return violations


def review_source(language: str, source: str) -> list[ScriptViolation]:
    """按语言分发的静态审查入口（语言合法性由上层校验）。"""
    if language == "python":
        return review_python(source)
    if language == "javascript":
        return review_javascript(source)
    return [ScriptViolation(line=1, kind="language", message=f"不支持的语言: {language}")]


def _called_name(func: ast.expr) -> str:
    """提取调用目标的名字；非简单名字（如 obj.method()）返回空串。"""
    if isinstance(func, ast.Name):
        return func.id
    return ""
