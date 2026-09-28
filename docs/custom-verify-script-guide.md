# 自定义验证脚本编写指南

管理员可为本群编写自定义入群验证题：脚本定义 **提问（`ask`）** 与 **验证（`verify`）** 两个接口，在独立沙盒容器内执行（无网络、无文件系统、资源受限），对宿主环境零影响。

## 契约

脚本必须定义两个同步函数（Python 为模块级函数，JavaScript 为 `module.exports` 导出）：

```python
# Python（上传为 .py 文件）
def ask(ctx) -> dict:
    """ctx = {
        "api_version": 1,
        "challenge_id": "<会话标识>",
        "group_id": "<群 ID 字符串>",
        "user": {"id": "...", "first_name": ..., "username": ..., "language_code": ...},
        "locale": "zh-Hans",
        "issued_at": <毫秒时间戳>, "expires_at": <毫秒时间戳>,
        "attempt_no": 1,
        "state": None,          # ask 时恒为 None
    }"""
    return {
        "text": "1 + 1 = ?",                       # 必填，≤2000 字符（将图片化渲染）
        "options": [                                # 可选；省略 = 文本作答模式
            {"text": "2", "value": "v2"},
            {"text": "3", "value": "v3"},
        ],
        "state": {"answer": "v2"},                  # 可选，≤4KB；verify 时原样回传
    }


def verify(ctx) -> dict:
    """ctx 同 ask，另加：
        "state": {...},     # ask 存的私有状态
        "input": "...",     # 按钮逻辑值（options 模式）或用户原文（文本模式）
    """
    return {"decision": "pass"}   # "pass" = 通过；"retry" = 答错（触发既有答错处罚）
```

JavaScript（上传为 `.js` 文件，CommonJS，**禁止 require/import**，只用语言内置全局）：

```javascript
module.exports.ask = function (ctx) {
  const a = 1 + Math.floor(Math.random() * 9);
  return { text: `${a} + 1 = ?`, state: { answer: a + 1 } };
};

module.exports.verify = function (ctx) {
  return { decision: Number(ctx.input) === ctx.state.answer ? "pass" : "retry" };
};
```

## 硬性限制

| 项 | 值 |
|----|----|
| 脚本大小 | ≤64KB，单文件，UTF-8 |
| 执行时长 | ask/verify 各 ≤2 秒（超时即失败） |
| 内存 | 128MB（Python）/ 32MB 堆（JS） |
| 题面 | ≤2000 字符；按钮 ≤8 个 |
| state | ≤4KB（紧凑 JSON） |

**禁止**（静态审查会拒绝并给出行号）：

- Python：`allowlist` 之外的导入（仅允许 `math, random, re, json, string, hashlib, hmac, base64, unicodedata, datetime, calendar, itertools, functools, collections, heapq, bisect, textwrap, difflib, statistics, decimal, fractions, copy, operator`）；`eval/exec/compile/open/__import__/getattr` 等调用；双下划线属性访问；任何文件/网络/进程操作
- JavaScript：一切 `require` / `import`；`eval`、动态 `Function`、`process`、`globalThis`、`fetch`、定时器

## 上传与启用

```
群内：/customverify upload        → 引导到私聊
私聊：发送 .py / .js 文件          → 三重审查（静态 → AI → 沙盒试跑）
群内：/customverify enable <id>   → 启用（新入群使用脚本题）
群内：/customverify disable       → 停用（回落群组原验证方式）
群内：/customverify history       → 版本记录；rollback <id> 可回滚
```

**行为须知**：

- 上传即三重审查，任一不过不入库；AI 审查不可用时不降级通过（fail-closed）
- 启用后**新入群**使用脚本题；已在途的验证会话仍按其出题时的版本判定
- 答错判定与全部内置题型一致：一次答错 ban 1 小时
- 沙盒故障不计答错：用户会收到「稍后重试」提示
- `verify` 永远返回 `retry` 的脚本会拒绝所有新人——**上传前务必用 `enable` 前自测**（`test` 流程见 /help）

## 完整示例：问答题（Python）

```python
import random

_QUESTION_BANK = [
    ("这个群主要讨论什么？", {"技术交流": "pass", "发广告": "retry"}),
    ("群规允许刷屏吗？", {"不允许": "pass", "允许": "retry"}),
]


def ask(ctx):
    question, mapping = random.choice(_QUESTION_BANK)
    options = list(mapping.items())
    random.shuffle(options)
    return {
        "text": question,
        "options": [{"text": text, "value": value} for text, value in options],
        "state": {"mapping": mapping},
    }


def verify(ctx):
    decision = ctx["state"]["mapping"].get(ctx["input"], "retry")
    return {"decision": decision}
```
