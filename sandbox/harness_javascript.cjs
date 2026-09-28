"use strict";

// 可信 JS harness：由 runner 以 `node --max-old-space-size=32` 启动，单任务单进程。
// 语言层限制是纵深防御（容器才是安全边界）：
// - 堆上限由 runner 的启动参数施加（Node 无 setrlimit）；
// - require/import 在加载脚本时以无 require 的包装函数禁用（脚本只需语言内置全局）；
// - process/globalThis 等全局残余由容器边界（read_only/internal 网络/cap_drop）兜底。

const fs = require("node:fs");
const path = require("node:path");

// 用户日志走 fd 2，协议 fd 1 只保留给最终 JSON 输出
console.log = (...args) => console.error(...args);
console.warn = (...args) => console.error(...args);
console.info = (...args) => console.error(...args);
process.stdout.write = (chunk, encoding, callback) =>
  process.stderr.write(chunk, encoding, callback);

function emit(payload) {
  // replacer 显式拒绝非有限数：JSON.stringify 默认把 NaN/Infinity 静默转成
  // null，会伪装成合法值穿透协议校验
  const encoded = JSON.stringify(payload, (_key, value) => {
    if (typeof value === "number" && !Number.isFinite(value)) {
      throw new TypeError("结果含非有限数（NaN/Infinity）");
    }
    return value;
  });
  fs.writeSync(1, encoded);
}

// 与 bot 侧静态审查共享同一约束：脚本不允许任何模块导入。
// 用 new Function 手工构造 CommonJS 包装（而非 require），require 参数
// 直接是抛错函数，顶层与函数体内的 require 均被拦截。
function loadScript(source, taskDir) {
  const wrapper = new Function(
    "exports",
    "require",
    "module",
    "__filename",
    "__dirname",
    source,
  );
  const module = { exports: {} };
  wrapper(
    module.exports,
    () => {
      throw new Error("沙盒脚本不允许 require/import");
    },
    module,
    "script.cjs",
    taskDir,
  );
  return module.exports;
}

function execute(taskDir) {
  const task = JSON.parse(fs.readFileSync(path.join(taskDir, "task.json"), "utf8"));
  if (
    task === null ||
    typeof task !== "object" ||
    task.context === null ||
    typeof task.context !== "object" ||
    Array.isArray(task.context)
  ) {
    throw new TypeError("任务文件字段非法");
  }
  if (task.entry !== "ask" && task.entry !== "verify") {
    throw new TypeError("entry 必须是 ask 或 verify");
  }
  const script = loadScript(
    fs.readFileSync(path.join(taskDir, "script.cjs"), "utf8"),
    taskDir,
  );
  const fn = script === null || typeof script !== "object" ? undefined : script[task.entry];
  if (typeof fn !== "function") {
    throw new TypeError(`入口 ${task.entry} 必须是函数`);
  }
  const result = fn(task.context);
  if (result != null && typeof result.then === "function") {
    throw new TypeError("不允许 Promise 结果");
  }
  if (result === null || typeof result !== "object" || Array.isArray(result)) {
    throw new TypeError("入口返回值必须是对象");
  }
  // JSON.stringify 遇 BigInt 抛 TypeError，自然落入统一 crash 协议；
  // NaN/Infinity 会序列化为 null，由 bot 侧结果校验按类型规则拒绝
  emit({ ok: true, result });
}

try {
  execute(process.argv[2]);
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  try {
    emit({
      ok: false,
      error: "crash",
      detail: error && error.name ? error.name : "Error",
    });
  } catch {
    // fd 1 不可写等极端情况：以非零退出让 runner 归类为 crash
    process.exit(2);
  }
}
