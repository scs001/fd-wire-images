// AI 构建页（#/console/chat，侧栏显示名「AI 构建」；wire-assistant-panel）：
// 助手会话的**整页形态**——复用与右下角浮层完全相同的 AssistantConversation
// （同一套统一副驾循环、同一套动作卡与失败纪律），只是把事务区放大到整页。
//
// 壹座深链仍在本页保留（spec 场景「保留壹座深链」）：深度分析场景不做页面
// 内嵌，用新标签页跳转（design D4）。页面文案不含任何测试账号明文。
import { useTranslation } from "react-i18next";
import { AssistantConversation } from "./assistant/AssistantConversation";
import { YIZUO_PLATFORM_URL } from "../components/footerConstants";

export function ChatPage({ planCode = "" }: { planCode?: string }) {
  const { t } = useTranslation();

  return (
    <div
      data-testid="chat-page"
      // 宽度基线来自外壳（wire-console-shell）；这里只保留卡片自身的内边距。
      className="flex flex-col rounded-lg border border-slate-200 bg-white p-6"
    >
      <h1 className="text-xl font-semibold">
        {t("console.assistant.pageTitle")}
      </h1>
      <p className="mt-2 text-sm text-slate-600">
        {t("console.assistant.pageSubtitle")}
      </p>
      <div className="mt-4 flex min-h-[26rem] flex-col">
        <AssistantConversation
          tab="chat"
          planCode={planCode}
          transcriptClassName="h-[20rem]"
        />
      </div>
      <div className="mt-4 border-t border-slate-200 pt-3">
        <a
          data-testid="chat-page-base-link"
          href={YIZUO_PLATFORM_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-slate-500 hover:text-slate-800"
        >
          {t("console.assistant.goBase")}
        </a>
      </div>
    </div>
  );
}
