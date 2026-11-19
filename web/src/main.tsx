import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import "./i18n";
import { App, parseHash } from "./App";
import { resolveNavigateTarget } from "./copilotIntents";

// 副驾宿主动作的动词白名单（harden-copilot-ask-and-intent 4.3）：
// index.html 的 navigate handler 在 loader 的 init 握手里就可能被调用，早于 React
// 首次渲染，所以在这里同步暴露。判定逻辑在 copilotIntents.ts（有单测），这里只注入。
(window as unknown as { __wireResolveNav?: (to: string) => string | null }).__wireResolveNav = (to) =>
  resolveNavigateTarget(to, parseHash);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
