/**
 * ai_trace.ts — pi 扩展:记录每一次模型调用的**精确计时**与用量。
 *
 * 为什么需要它:pi 的 session JSONL 只在 session 行写时间戳,assistant 的
 * message_start / message_end 没有时间,单靠日志算不出"这一次调用花了多久"。
 * 这个扩展挂在 provider 钩子上,把每次请求的 start / 首包 / 结束时间写进
 * <worker 日志>.calls(JSONL),ai_monitor.py 再据此算延迟与用量。
 *
 * 输出路径:环境变量 ROTOM_AI_TRACE_FILE(solver 启动 worker 时按日志同名注入)。
 * 未设置 = 不记录(零开销)。
 *
 * 记录格式(每行一条):
 *   {"kind":"ai_call","pid":...,"start_ms":...,"end_ms":...,"duration_ms":...,
 *    "ttfb_ms":...,"status":200,"provider":"...","api":"...","model":"...",
 *    "request_bytes":1234,"usage":{...},"error":""}
 *
 * 设计约束:任何异常都必须吞掉 —— 监控绝不能把 agent 弄挂。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

type Pending = {
  start_ms: number;
  request_bytes: number;
  model?: string;
  ttfb_ms?: number;
  status?: number;
};

export default function (pi: ExtensionAPI) {
  const path = (process.env.ROTOM_AI_TRACE_FILE || "").trim();
  if (!path) return;

  try {
    mkdirSync(dirname(path), { recursive: true });
  } catch {
    /* 目录建不出来就静默关掉 */
    return;
  }

  const pending: Pending[] = [];

  const write = (rec: Record<string, unknown>) => {
    try {
      appendFileSync(path, JSON.stringify(rec) + "\n", "utf8");
    } catch {
      /* 磁盘满/权限问题:不影响 agent */
    }
  };

  pi.on("before_provider_request", (event) => {
    let bytes = 0;
    let model: string | undefined;
    try {
      const payload = event.payload as { model?: unknown } | undefined;
      if (payload && typeof payload === "object" && payload.model) model = String(payload.model);
      bytes = Buffer.byteLength(JSON.stringify(event.payload ?? null), "utf8");
    } catch {
      bytes = 0;
    }
    pending.push({ start_ms: Date.now(), request_bytes: bytes, model });
  });

  pi.on("after_provider_response", (event) => {
    const cur = pending.length ? pending[pending.length - 1] : undefined;
    if (!cur) return;
    cur.status = Number(event.status) || 0;
    cur.ttfb_ms = Date.now() - cur.start_ms;
  });

  pi.on("message_end", (event) => {
    const msg = event.message as any;
    if (!msg || msg.role !== "assistant" || !pending.length) return;
    const cur = pending.shift() as Pending;
    const end = Date.now();
    write({
      kind: "ai_call",
      pid: process.pid,
      start_ms: cur.start_ms,
      end_ms: end,
      duration_ms: end - cur.start_ms,
      ttfb_ms: cur.ttfb_ms ?? null,
      status: cur.status ?? null,
      provider: msg.provider ?? null,
      api: msg.api ?? null,
      model: msg.model ?? cur.model ?? null,
      request_bytes: cur.request_bytes,
      usage: msg.usage ?? null,
      error: "",
    });
  });

  // 请求失败(没有 assistant message)时也要把半条记录收尾,避免下一次调用错配
  pi.on("turn_end", () => {
    while (pending.length) {
      const cur = pending.shift() as Pending;
      const end = Date.now();
      write({
        kind: "ai_call",
        pid: process.pid,
        start_ms: cur.start_ms,
        end_ms: end,
        duration_ms: end - cur.start_ms,
        ttfb_ms: cur.ttfb_ms ?? null,
        status: cur.status ?? 0,
        provider: null,
        api: null,
        model: cur.model ?? null,
        request_bytes: cur.request_bytes,
        usage: null,
        error: "no assistant message_end (provider call failed or aborted)",
      });
    }
  });
}