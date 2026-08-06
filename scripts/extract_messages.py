#!/usr/bin/env python3
"""扫描 Bot 出站文案并输出 CSV 清单

仅做盘点，不生成 catalog，也不自动修改业务代码。用于：
- 迁移前盘点出站文案的分布；
- 作为人工分配翻译 key 的工作底稿；
- 后续 CI guard 的基础（禁止新增字符串字面量）。

扫描目标：
- ``answer`` / ``reply`` / ``send_message`` / ``edit_text`` / ``edit_caption``
  及所有 ``send_*`` 方法的 ``text`` 与 ``caption`` 参数；
- ``InlineKeyboardButton(text=...)``；
- ``BotCommand(description=...)``。

示例::

    python scripts/extract_messages.py
    python scripts/extract_messages.py --root src --output message_inventory.csv
"""

import argparse
import ast
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

# 直接接收文本的 aiogram 方法（text 为位置或关键字参数）
DIRECT_TEXT_METHODS = frozenset(
    {"answer", "reply", "send_message", "edit_text", "edit_caption"}
)


@dataclass(frozen=True, slots=True)
class MessageCandidate:
    """一个待人工确认的出站文案候选"""

    file: str
    line: int
    call_type: str
    expression: str
    estimated_key: str


def dotted_name(node: ast.AST) -> str:
    """提取函数调用的点分名称（如 bot.send_message）"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def source_expression(source: str, node: ast.AST) -> str:
    """返回字符串内容、f-string 源码或变量表达式"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    segment = ast.get_source_segment(source, node)
    if segment:
        return segment
    try:
        return ast.unparse(node)
    except Exception:
        return f"<{type(node).__name__}>"


def keyword_value(call: ast.Call, name: str) -> ast.AST | None:
    """获取指定关键字参数的值节点"""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def positional_text_argument(call: ast.Call, short_name: str) -> ast.AST | None:
    """按常见 aiogram 签名推断位置参数中的文案"""
    if short_name in {"answer", "reply", "edit_text"}:
        return call.args[0] if call.args else None
    if short_name == "send_message":
        return call.args[1] if len(call.args) >= 2 else None
    return None


def estimated_key(path: Path, root: Path, surface: str, line: int) -> str:
    """生成仅供人工整理的临时 key"""
    relative = path.relative_to(root)
    module = ".".join(relative.with_suffix("").parts).replace("_", ".")
    return f"{module}.todo.{surface}.line_{line}"


class OutboundVisitor(ast.NodeVisitor):
    """提取直接出站调用及 UI 构造器文本"""

    def __init__(self, path: Path, root: Path, source: str) -> None:
        self.path = path
        self.root = root
        self.source = source
        self.items: list[MessageCandidate] = []

    def visit_Call(self, node: ast.Call) -> None:
        call_name = dotted_name(node.func)
        short_name = call_name.rsplit(".", 1)[-1]

        if short_name == "InlineKeyboardButton":
            value = keyword_value(node, "text")
            if value is None and node.args:
                value = node.args[0]
            self._append(node, "InlineKeyboardButton.text", "button", value)

        elif short_name == "BotCommand":
            value = keyword_value(node, "description")
            if value is None and len(node.args) >= 2:
                value = node.args[1]
            self._append(node, "BotCommand.description", "command_description", value)

        elif short_name in DIRECT_TEXT_METHODS or short_name.startswith("send_"):
            text_value = keyword_value(node, "text")
            if text_value is None:
                text_value = positional_text_argument(node, short_name)
            if text_value is not None:
                self._append(node, f"{call_name}.text", "message", text_value)

            caption_value = keyword_value(node, "caption")
            if caption_value is not None:
                self._append(node, f"{call_name}.caption", "caption", caption_value)

        self.generic_visit(node)

    def _append(
        self,
        call: ast.Call,
        call_type: str,
        surface: str,
        value: ast.AST | None,
    ) -> None:
        if value is None:
            return
        self.items.append(
            MessageCandidate(
                file=str(self.path),
                line=call.lineno,
                call_type=call_type,
                expression=source_expression(self.source, value),
                estimated_key=estimated_key(self.path, self.root, surface, call.lineno),
            )
        )


def scan_file(path: Path, root: Path) -> list[MessageCandidate]:
    """扫描单个 Python 文件"""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        print(f"跳过语法错误文件 {path}: {exc}", file=sys.stderr)
        return []
    visitor = OutboundVisitor(path, root, source)
    visitor.visit(tree)
    return visitor.items


def write_csv(items: list[MessageCandidate], output: TextIO) -> None:
    """输出 CSV"""
    writer = csv.writer(output)
    writer.writerow(["文件", "行号", "调用类型", "字符串或变量", "预估 key"])
    for item in sorted(items, key=lambda value: (value.file, value.line)):
        writer.writerow(
            [item.file, item.line, item.call_type, item.expression, item.estimated_key]
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描 Bot 出站文案")
    parser.add_argument("--root", default="src", help="扫描根目录（默认 src）")
    parser.add_argument("--output", default="-", help="CSV 输出文件，'-' 表示 stdout")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    items: list[MessageCandidate] = []
    for path in sorted(root.rglob("*.py")):
        items.extend(scan_file(path, root))

    if args.output == "-":
        write_csv(items, sys.stdout)
        return

    # utf-8-sig 带 BOM，便于 Excel 直接打开中文表头
    output_path = Path(args.output)
    with output_path.open("w", encoding="utf-8-sig", newline="") as output:
        write_csv(items, output)


if __name__ == "__main__":
    main()
