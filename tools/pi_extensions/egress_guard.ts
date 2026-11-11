/**
 * egress_guard.ts — pi 扩展:出口合规闸门(程序强制,PreToolUse 同款语义)
 *
 * 为什么存在:提示词里的"必须走代理"会被同义改写/换环境遗忘绕过;
 * 这里的 tool_call 拦截把铁律升级成程序强制 —— 与编辑器侧 PreToolUse hook(EGRESS_GUARD_SCRIPT 指向的同一策略脚本)同一套判定。
 *
 * 策略脚本:环境变量 EGRESS_GUARD_SCRIPT 指向一个 hook 协议脚本
 *   stdin : {"tool_name":"Bash","tool_input":{"command":"..."}}
 *   exit 0 放行 / exit 2 拦截,拦截原因写 stderr(会原样回给模型,含合规改法)
 * 未设置该变量 = 本扩展不启用(普通 CTF/跑分场景零开销)。
 *
 * 语义与 ZCode hook 一致:fail-open(策略脚本缺失/异常/超时一律放行,不阻塞工作);
 * 逃生舱(EGRESS_ALLOW_DIRECT=1 写在命令里)由策略脚本自己识别并留痕。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";

export default function (pi: ExtensionAPI) {
  const script = (process.env.EGRESS_GUARD_SCRIPT || "").trim();
  if (!script) return;

  const check = (command: string) =>
    new Promise<{ code: number; message: string }>((resolve) => {
      let message = "";
      let settled = false;
      const finish = (code: number) => {
        if (settled) return;
        settled = true;
        resolve({ code, message });
      };
      try {
        const child = spawn("python3", [script], { stdio: ["pipe", "ignore", "pipe"] });
        // 策略脚本 hang 死也不能卡住 worker:5s 超时按 fail-open 放行
        const timer = setTimeout(() => {
          child.kill("SIGKILL");
          finish(0);
        }, 5000);
        child.stderr?.on("data", (d: Buffer) => {
          message += d.toString();
        });
        child.on("error", () => {
          clearTimeout(timer);
          finish(0); // python3 不存在等启动失败 → fail-open
        });
        child.on("close", (code) => {
          clearTimeout(timer);
          finish(code ?? 0);
        });
        child.stdin.write(JSON.stringify({ tool_name: "Bash", tool_input: { command } }));
        child.stdin.end();
      } catch {
        finish(0);
      }
    });

  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return undefined;
    const command = String((event.input as { command?: unknown })?.command ?? "");
    if (!command.trim()) return undefined;
    const { code, message } = await check(command);
    if (code === 0) return undefined;
    return {
      block: true,
      reason:
        (message || "出口合规闸门拦截:探测命令必须显式走代理。") +
        "\n(egress_guard) 本条命令未执行;请按上面的合规改法改写后重发,不要变换同义写法硬闯。",
    };
  });
}
