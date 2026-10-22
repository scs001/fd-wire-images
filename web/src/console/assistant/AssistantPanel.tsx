// AI 助手浮层外壳（wire-assistant-panel）：右下角常驻入口 + 展开面板。
//
// 这里只管「开/合」与外壳尺寸；统一副驾循环、动作卡、失败纪律全在
// AssistantConversation（与「AI 构建」整页形态共用同一套实现）。
//
// Esc 收起但**不丢弃草稿**——草稿状态在会话组件里，收起只是卸载外壳；为了让
// 草稿在收起后仍在，外壳保持挂载、仅切换可见性（见 spec 场景「Esc 收起」）。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AssistantConversation } from "./AssistantConversation";
import type { AssistantContext } from "../api";

export interface AssistantPanelProps {
  /** 当前页签（上下文注入用）。 */
  tab: string;
  /** 当前档位 code（免费档渲染能力说明徽章；档位校验在后端闸）。 */
  planCode: string;
  /** 当前产物/数据集标识（可选）。 */
  context?: AssistantContext;
}

export function AssistantPanel({ tab, planCode, context }: AssistantPanelProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  // Esc 收起（草稿因组件保持挂载而保留）。
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <>
      <button
        type="button"
        data-testid="assistant-launcher"
        aria-label={t("console.assistant.open")}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        hidden={open}
        className="fixed bottom-6 right-6 z-40 flex h-12 w-12 items-center justify-center rounded-full bg-slate-900 text-white shadow-lg transition-transform hover:scale-105"
      >
        <span aria-hidden="true" className="text-lg">
          ✳
        </span>
      </button>
      {/* 外壳常驻挂载、按 open 切换可见性：草稿与会话状态不因收起而丢。 */}
      <div
        data-testid="assistant-panel"
        data-open={open ? "on" : "off"}
        role="dialog"
        aria-label={t("console.assistant.title")}
        aria-hidden={!open}
        className={`fixed bottom-6 right-6 z-40 flex h-[30rem] w-[24rem] flex-col overflow-hidden rounded-xl border border-slate-200 bg-white p-4 shadow-2xl ${
          open ? "" : "hidden"
        }`}
      >
        <button
          type="button"
          data-testid="assistant-close"
          aria-label={t("console.assistant.close")}
          onClick={() => setOpen(false)}
          className="absolute right-2 top-2 rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
        >
          ✕
        </button>
        <AssistantConversation
          tab={tab}
          planCode={planCode}
          context={context}
          transcriptClassName="h-[17rem]"
        />
      </div>
    </>
  );
}
