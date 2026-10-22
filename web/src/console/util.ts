// 控制台呈现层的纯函数工具：tool_trace 留痕解析、时间与摘要格式化。
import type { ChatMessage } from "./api";

/** tool_trace 消息 content 的留痕结构（loop.execute 后 JSON 序列化落库）。 */
export interface ToolTracePayload {
  tool_call_id?: string;
  tool_name: string;
  arguments?: unknown;
  result?: unknown;
}

/** 解析 tool_trace 留痕 JSON；非法结构返回 null（调用方按原文如实呈现）。 */
export function parseToolTrace(content: string): ToolTracePayload | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    return null;
  }
  if (
    !parsed ||
    typeof parsed !== "object" ||
    typeof (parsed as { tool_name?: unknown }).tool_name !== "string"
  ) {
    return null;
  }
  return parsed as ToolTracePayload;
}

/**
 * 后端时间（naive UTC，如 "2026-10-04T09:30:00"）→ 本地可读时间串。
 * 无时区标记时按 UTC 解析再转本地，避免时区错位。
 */
export function formatServerDate(naiveIso: string): string {
  const normalized = /[Zz]|[+-]\d{2}:?\d{2}$/.test(naiveIso)
    ? naiveIso
    : `${naiveIso}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime())
    ? naiveIso
    : date.toLocaleString();
}

/** 工具入参摘要：顶层键值 `k=v` 取前 3 项、单值截断，便于折叠行展示。 */
export function summarizeArgs(args: unknown): string {
  if (args === null || args === undefined) return "";
  if (typeof args !== "object") return truncate(String(args), 60);
  const entries = Object.entries(args as Record<string, unknown>).slice(0, 3);
  const parts = entries.map(([k, v]) => {
    const value =
      v && typeof v === "object" ? truncate(JSON.stringify(v), 40) : String(v);
    return `${k}=${truncate(value, 40)}`;
  });
  const rest =
    Object.keys(args as Record<string, unknown>).length - entries.length;
  if (rest > 0) parts.push(`+${rest}`);
  return parts.join(", ");
}

/** 产物 spec 摘要：一行 JSON、截断到 120 字符。 */
export function summarizeSpec(spec: Record<string, unknown>): string {
  return truncate(JSON.stringify(spec), 120);
}

/** 产物标题（spec.title 优先；缺失回落 kind + id）。 */
export function artifactTitle(
  artifact: { id: number; kind: string; spec: Record<string, unknown> },
  fallback: string,
): string {
  const title = artifact.spec?.title;
  return typeof title === "string" && title.trim() ? title.trim() : fallback;
}

export function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/** 供排序：消息按 id 升序（append-only，id 即次序）。 */
export function sortMessages(messages: ChatMessage[]): ChatMessage[] {
  return [...messages].sort((a, b) => a.id - b.id);
}
