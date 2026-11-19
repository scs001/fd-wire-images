// 跨线副驾（萬星 copilot.js）的挂载判定（unify-wire-copilot-surface 2.1/2.2）。
//
// 一条界面上只能有一个对话入口。本 app 里：
// - 控制台/管理面有原生 AI 助手（AssistantPanel / ChatPage）——它能读本企业
//   数据、走动作闸、按客户档位计量，是有用的那个；
// - 公开面没有原生助手，产品咨询与页面导览归萬星外向副驾。
//
// 所以萬星在这几类路由上退场。判定抽成纯函数是为了可单测：内联脚本里跑
// location.hash 的实现无法在单测里覆盖。

/** 萬星外向副驾是否应在本路由上出现。 */
export function shouldMountWanxingCopilot(hash: string): boolean {
  const path = (hash || "#/").replace(/^#/, "") || "/";
  // 控制台与内部后台：原生助手在场
  if (path === "/console" || path.startsWith("/console/")) return false;
  if (path === "/admin" || path.startsWith("/admin/")) return false;
  // 公开只读面：两个都不出现（assistant-panel 既有约束）
  if (path.startsWith("/share/")) return false;
  if (path === "/showcase" || path.startsWith("/showcase/")) return false;
  // 登录中转
  if (path === "/login") return false;
  return true;
}
