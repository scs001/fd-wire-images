// 错误边界（wire-console-ux-fixes 任务 2.1/2.2）：class 组件兜底子树渲染异常
// ——渲染期 throw 不再白屏整页，落 amber 风格错误卡（与控制台阻塞/错误面一致）
// +「重试」按钮（清 state 重挂载 children）。componentDidCatch 留 console 痕迹
// 便于排查。两层布点：App.renderRoute 外围（全局兜底）+ ConsoleView ready 分支
// children 外围（页签内容层——单页异常只降级内容区，侧栏顶栏仍在）。
import { Component, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

type ErrorBoundaryProps = { children: ReactNode };

type ErrorBoundaryState = { error: Error | null; nonce: number };

/** 错误卡（降级态 UI）：amber 边框风格对齐控制台既有错误卡；retry 触发重挂载。 */
function ErrorCard({
  onRetry,
}: {
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      data-testid="error-boundary-card"
      className="mx-auto max-w-xl rounded-lg border border-amber-200 bg-amber-50/50 p-8"
      role="alert"
    >
      <h1 className="text-xl font-semibold">
        {t("common.errorBoundary.title")}
      </h1>
      <p className="mt-3 text-sm text-slate-600">
        {t("common.errorBoundary.body")}
      </p>
      <button
        type="button"
        data-testid="error-boundary-retry"
        onClick={onRetry}
        className="mt-5 rounded-md bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-700"
      >
        {t("common.errorBoundary.retry")}
      </button>
    </div>
  );
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null, nonce: 0 };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: { componentStack?: string | null }) {
    // 留痕（不上报后端——前端观测后续任务再接）：错误 + 组件栈。
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  /** 重试：清错误态 + nonce 重挂载 children（恢复到抛错前的挂载面）。 */
  private readonly handleRetry = () => {
    this.setState((prev) => ({ error: null, nonce: prev.nonce + 1 }));
  };

  render() {
    if (this.state.error) {
      return <ErrorCard onRetry={this.handleRetry} />;
    }
    // key=nonce：重试时整棵子树重挂载（重置子组件内部 state）。
    return <div key={this.state.nonce}>{this.props.children}</div>;
  }
}
