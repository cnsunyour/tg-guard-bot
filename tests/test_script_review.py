"""静态风险审查器的单元测试。"""

import pytest

from src.services.script_review import review_javascript, review_python, review_source

pytestmark = [pytest.mark.unit]


class TestPythonReview:
    def test_legitimate_script_passes(self):
        source = (
            "import random\n"
            "import math\n"
            "\n"
            "def ask(ctx):\n"
            "    a, b = random.randint(1, 9), random.randint(1, 9)\n"
            "    answer = a + b\n"
            "    choices = sorted({answer, answer + 1, answer - 1, max(a, b)})\n"
            "    return {\n"
            "        'text': f'{a} + {b} = ?',\n"
            "        'options': [{'text': str(c), 'value': f'v{c}'} for c in choices],\n"
            "        'state': {'answer': answer},\n"
            "    }\n"
            "\n"
            "def verify(ctx):\n"
            "    return {'decision': 'pass' if str(ctx['state']['answer']) == ctx['input'] else 'retry'}\n"
        )
        assert review_python(source) == []

    def test_allowlist_import_ok_forbidden_import_rejected(self):
        assert review_python("import json\n\ndef ask(ctx):\n    return {'text': 'x'}\n") == []
        violations = review_python("import socket\n\ndef ask(ctx):\n    return {'text': 'x'}\n")
        assert len(violations) == 1
        assert violations[0].kind == "import"
        assert violations[0].line == 1

    def test_relative_import_rejected(self):
        violations = review_python("from .helpers import thing\n")
        assert any(v.kind == "import" for v in violations)

    def test_forbidden_builtin_calls_rejected_with_line(self):
        source = "def ask(ctx):\n    return eval(ctx['input'])\n"
        violations = review_python(source)
        assert any(v.kind == "call" and v.message.startswith("不允许调用 eval") for v in violations)
        assert violations[0].line == 2

    @pytest.mark.parametrize(
        "call", ["open('x')", "exec('1')", "compile('1', 'x', 'eval')", "getattr(a, 'b')"]
    )
    def test_each_forbidden_call(self, call):
        source = f"def ask(ctx):\n    {call}\n    return {{'text': 'x'}}\n"
        assert any(v.kind == "call" for v in review_python(source))

    def test_dunder_attribute_access_rejected(self):
        source = "def ask(ctx):\n    return {'text': str(ctx.__class__)}\n"
        violations = review_python(source)
        assert any(v.kind == "dunder" and "__class__" in v.message for v in violations)

    def test_sensitive_name_reference_rejected(self):
        source = "def ask(ctx):\n    return {'text': os.environ}\n"
        violations = review_python(source)
        assert any(v.kind == "sensitive_name" and "os" in v.message for v in violations)

    def test_syntax_error_reported(self):
        violations = review_python("def ask(ctx)\n    return 1\n")
        assert len(violations) == 1
        assert violations[0].kind == "syntax"
        assert violations[0].line == 1

    def test_print_allowed(self):
        source = "def ask(ctx):\n    print('debug')\n    return {'text': 'x'}\n"
        assert review_python(source) == []


class TestJavaScriptReview:
    def test_legitimate_script_passes(self):
        source = (
            "module.exports.ask = function (ctx) {\n"
            "  const a = 1 + Math.floor(Math.random() * 9);\n"
            "  return { text: a + ' + 1 = ?', state: { answer: a + 1 } };\n"
            "};\n"
            "module.exports.verify = function (ctx) {\n"
            "  return { decision: Number(ctx.input) === ctx.state.answer ? 'pass' : 'retry' };\n"
            "};\n"
        )
        assert review_javascript(source) == []

    @pytest.mark.parametrize(
        ("snippet", "expected_hint"),
        [
            ('const fs = require("fs");', "require"),
            ("import x from 'y';", "require"),
            ("eval('1+1');", "eval"),
            ("const f = new Function('return 1');", "动态构造"),
            ("process.env.HOME;", "process"),
            ("globalThis.secret;", "globalThis"),
            ("fetch('https://x.com');", "网络"),
            ("setTimeout(fn, 100);", "定时器"),
        ],
    )
    def test_forbidden_patterns(self, snippet, expected_hint):
        violations = review_javascript(snippet)
        assert violations, f"应命中至少一条规则: {snippet}"
        assert expected_hint in violations[0].message
        assert violations[0].line == 1

    def test_comments_are_ignored(self):
        assert review_javascript("// require('fs') 注释里提到不算\n") == []
        assert review_javascript("/* process.exit() */\nconst a = 1;\n") == []

    def test_line_number_accuracy(self):
        source = "const a = 1;\nconst b = 2;\nconst c = require('fs');\n"
        violations = review_javascript(source)
        assert len(violations) == 1
        assert violations[0].line == 3


class TestDispatch:
    def test_unknown_language_fails_closed(self):
        violations = review_source("ruby", "puts 1")
        assert len(violations) == 1
        assert violations[0].kind == "language"
