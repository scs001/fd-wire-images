// AI 助手浮层外壳（wire-assistant-panel）：右下角常驻入口 + 展开面板。
//
// 这里只管「开/合」与外壳尺寸；统一副驾循环、动作卡、失败纪律全在
// AssistantConversation（与「AI 构建」整页形态共用同一套实现）。
//
// Esc 收起但**不丢弃草稿**——草稿状态在会话组件里，收起只是卸载外壳；为了让
// 草稿在收起后仍在，外壳保持挂载、仅切换可见性（见 spec 场景「Esc 收起」）。
//
// 尺寸（unify-wire-copilot-surface 3.x）：默认 24rem × 30rem，可拖拽右下角缩放，
// 也可一键在默认与放大之间切换。尺寸写 localStorage，下次打开沿用——不进
// preferences 表：那是一次 API 往返换一个纯观感偏好，不划算。
import { useCallback, useEffect, useState } from "react";
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

const SIZE_KEY = "wire.assistant.panel-size";
/** 默认尺寸（rem）与放大尺寸（视口比例）。 */
const DEFAULT_SIZE = { w: 24, h: 30 };
const MAXIMIZED = { w: "60vw", h: "85vh" } as const;
const MIN_SIZE = { w: 18, h: 20 };

interface StoredSize {
  w: number;
  h: number;
  maximized: boolean;
}

function readSize(): StoredSize {
  try {
    const raw = localStorage.getItem(SIZE_KEY);
    if (!raw) return { ...DEFAULT_SIZE, maximized: false };
    const parsed = JSON.parse(raw) as Partial<StoredSize>;
    const w = Number(parsed.w);
    const h = Number(parsed.h);
    return {
      w: Number.isFinite(w) && w >= MIN_SIZE.w ? w : DEFAULT_SIZE.w,
      h: Number.isFinite(h) && h >= MIN_SIZE.h ? h : DEFAULT_SIZE.h,
      maximized: parsed.maximized === true,
    };
  } catch {
    return { ...DEFAULT_SIZE, maximized: false };
  }
}

export function AssistantPanel({ tab, planCode, context }: AssistantPanelProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [size, setSize] = useState<StoredSize>(readSize);

  const toggleMaximized = useCallback(() => {
    setSize((current) => {
      const next = { ...current, maximized: !current.maximized };
      try {
        localStorage.setItem(SIZE_KEY, JSON.stringify(next));
      } catch {
        /* 隐私模式：尺寸不记忆，不影响功能 */
      }
      return next;
    });
  }, []);

  /** 拖拽把手停下时间（`resize` 事件）记录当前像素尺寸，换算回 rem 存储。 */
  const rememberDragged = useCallback((event: React.SyntheticEvent<HTMLDivElement>) => {
    const el = event.currentTarget;
    const rootFont =
      parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    setSize((current) => {
      if (current.maximized) return current; // 放大态拖拽先还原再记
      const next: StoredSize = {
        w: Math.round(el.offsetWidth / rootFont),
        h: Math.round(el.offsetHeight / rootFont),
        maximized: false,
      };
      try {
        localStorage.setItem(SIZE_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  // Esc 收起（草稿因组件保持挂载而保留）。
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // 缩放后的尺寸：放大态给视口比例，否则给记忆的 rem。
  const panelStyle = size.maximized
    ? { width: MAXIMIZED.w, height: MAXIMIZED.h }
    : {
        width: `${size.w}rem`,
        height: `${size.h}rem`,
        minWidth: `${MIN_SIZE.w}rem`,
        minHeight: `${MIN_SIZE.h}rem`,
      };

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
      {/* 外壳常驻挂载、按 open 切换可见性：草稿与会话状态不因收起而丢。
          resize:both 用浏览器原生把手——自写拖拽是十倍代码换同样结果。 */}
      <div
        data-testid="assistant-panel"
        data-open={open ? "on" : "off"}
        data-maximized={size.maximized ? "on" : "off"}
        role="dialog"
        aria-label={t("console.assistant.title")}
        aria-hidden={!open}
        onMouseUp={rememberDragged}
        style={{ ...panelStyle, resize: "both", overflow: "hidden" }}
        className={`fixed bottom-6 right-6 z-40 flex max-w-[calc(100vw-2rem)] max-h-[calc(100vh-2rem)] flex-col rounded-xl border border-slate-200 bg-white p-4 shadow-2xl ${
          open ? "" : "hidden"
        }`}
      >
        <div className="mb-1 flex items-center justify-end gap-1">
          <button
            type="button"
            data-testid="assistant-maximize"
            aria-label={
              size.maximized
                ? t("console.assistant.restore")
                : t("console.assistant.maximize")
            }
            title={
              size.maximized
                ? t("console.assistant.restore")
                : t("console.assistant.maximize")
            }
            onClick={toggleMaximized}
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          >
            {size.maximized ? "⤡" : "⤢"}
          </button>
          <button
            type="button"
            data-testid="assistant-close"
            aria-label={t("console.assistant.close")}
            onClick={() => setOpen(false)}
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          >
            ✕
          </button>
        </div>
        <AssistantConversation
          tab={tab}
          planCode={planCode}
          context={context}
          transcriptClassName="min-h-0 flex-1"
        />
      </div>
    </>
  );
}
