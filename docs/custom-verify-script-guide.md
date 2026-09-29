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

## AI 审查通道说明

脚本入库的 AI 审查固定走**主 AI provider**（`AI_SPAM_PROTOCOL`）。主协议为
`typesafe_systemone`（Jev）时，其固定 questions 不接受自定义审查指令，审查会
自动改经 **Vision 通道**执行（要求 Vision 主或备已配置且协议非 Jev，启动期校验；
运行日志会记录「脚本 AI 审查经 vision_* 通道执行」）。两者都不可用时上传被阻断
（fail-closed，不会跳过审查）。注意：审查请求会把**脚本源码全文**发送到对应
provider 配置的第三方端点（主协议端点或 Vision 端点）——与群图片检测发送至
Vision 端点的既有数据流一致，但数据类别不同，部署时应知悉。

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

# 每题：(题面, [(按钮文本, 语义token, 是否正确答案), ...])
# ⚠️ value 是回传给 verify 的语义 token（仅字母/数字/下划线/连字符），
#    不是判定结果——判定恒由 verify 的返回值表达
_QUESTION_BANK = [
    (
        "这个群主要讨论什么？",
        [("技术交流", "tech", True), ("发广告", "ad", False)],
    ),
    (
        "群规允许刷屏吗？",
        [("不允许", "no-spam", True), ("允许", "spam", False)],
    ),
]


def ask(ctx):
    question, options = random.choice(_QUESTION_BANK)
    shuffled = list(options)
    random.shuffle(shuffled)
    correct = next(value for _text, value, ok in shuffled if ok)
    return {
        "text": question,
        "options": [{"text": text, "value": value} for text, value, _ok in shuffled],
        "state": {"correct": correct},
    }


def verify(ctx):
    # ctx["input"] = 用户点击按钮的 value（语义 token）
    return {"decision": "pass" if ctx["input"] == ctx["state"]["correct"] else "retry"}
```

> **常见错误**：不要把 decision（pass/retry）当作按钮 value——value 的职责是
> 「标识用户选了哪个选项」，判定恒由 `verify` 的返回值表达。dry-run 会对
> 每个按钮选项逐一试跑，所有选项都返回 retry 的脚本（无解题）会被拒绝入库。

## 完整示例：问答题（JavaScript）

与上面的 Python 示例同构（随机抽题 + 按钮乱序 + 正确答案存 state），供两种语言对照：

```javascript
// 每题：{ text, options: [{ text, value, correct }] }
// ⚠️ value 是回传给 verify 的语义 token（仅字母/数字/下划线/连字符），
//    不是判定结果——判定恒由 verify 的返回值表达
const QUESTION_BANK = [
  {
    text: "这个群主要讨论什么？",
    options: [
      { text: "技术交流", value: "tech", correct: true },
      { text: "发广告", value: "ad", correct: false },
    ],
  },
  {
    text: "群规允许刷屏吗？",
    options: [
      { text: "不允许", value: "no-spam", correct: true },
      { text: "允许", value: "spam", correct: false },
    ],
  },
];

function ask(ctx) {
  const question = QUESTION_BANK[Math.floor(Math.random() * QUESTION_BANK.length)];
  const options = [...question.options];
  // Fisher–Yates 乱序：按钮顺序随机化（JS 无内置 shuffle）
  for (let i = options.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [options[i], options[j]] = [options[j], options[i]];
  }
  return {
    text: question.text,
    options: options.map(({ text, value }) => ({ text, value })),
    state: { correct: options.find((option) => option.correct).value },
  };
}

function verify(ctx) {
  // ctx.input = 用户点击按钮的 value（语义 token）
  return { decision: ctx.input === ctx.state.correct ? "pass" : "retry" };
}

module.exports = { ask, verify };
```

> **JS 特有注意**：
> - 结果里的数字必须是有限数——返回值含 `NaN` / `Infinity` 或 `BigInt` 时，
>   沙盒会拒绝序列化并按执行失败（crash）处理。文本判定优先用字符串比较；
>   确需数字解析时注意 `Number("abc")` 得 `NaN`、与任何值比较恒为 false
>   （一律判 retry）
> - 沙盒无 `require` / `import`，示例中的数组展开、解构、`Math.random` 都是
>   语言内置能力，静态审查全部放行
> - `console.log` 可用于调试，输出进容器日志（stderr），不影响协议结果
