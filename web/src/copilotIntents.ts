// 副驾宿主动作的动词白名单（harden-copilot-ask-and-intent 4.3）。
//
// 为什么需要它：宿主声明了 navigate 之后，agent 会提议「跳到某个路由」。白名单不是
// 「看起来像路径就放行」——本 app 的 parseHash 对任何未知 hash 都回落成 home，那样
// 「跳转成功」是假的，agent 会以为用户到了目标页。所以这里用同一个解析器反查：
// 解析结果若回落到 home 而目标明显不是 home，就判定为未知路由并拒绝。
//
// 抽成独立模块而不是写在 index.html 里，是为了能被 vitest 直接测。
import type { Route } from "./App";

/** 判断 navigate 的目标是否是本 app 认识的路由。返回规范化后的 hash，或 null。 */
export function resolveNavigateTarget(to: string, parseHash: (h: string) => Route): string | null {
  const raw = String(to ?? "");
  // 只接受站内路由：`/x` 或 `#/x`。外部 URL、javascript:、`//host` 一律拒绝。
  if (!/^#?\/(?!\/)/.test(raw)) return null;
  const hash = raw.charAt(0) === "#" ? raw : `#${raw}`;
  const parsed = parseHash(hash);
  const isHomeTarget = /^#\/?$/.test(hash) || hash === "#/";
  // parseHash 把未知路径回落成 home；除目标本身就是 home 外，那个 home 都是回落信号。
  if (parsed.name === "home" && !isHomeTarget) return null;
  return hash;
}
