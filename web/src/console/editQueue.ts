// 行内编辑器纯逻辑（wire-dataset-form-editing 任务 2.1）：
// 脏队列归约 + 按入队顺序的顺序保存（失败隔离）。UI（DatasetDetail 的
// PreviewGrid）只是薄壳——本文件不依赖 React/i18n，独立可测。
import type { CorrectionInput } from "./api";

/** 脏队列项：修正载荷 + 本地序号（seq 即入队顺序 = 保存提交顺序）。 */
export interface EditQueueItem {
  seq: number;
  payload: CorrectionInput;
}

/** 追加一项（seq = 当前最大 + 1）；队列保持入队序，保存按此序逐条提交。 */
export function enqueueEdit(
  items: EditQueueItem[],
  payload: CorrectionInput,
): EditQueueItem[] {
  const seq = items.reduce((max, item) => Math.max(max, item.seq), 0) + 1;
  return [...items, { seq, payload }];
}

/**
 * 队列项的行上下文描述（失败/结果呈现用；行锚/列名/值为数据，无 i18n）：
 * update → `行锚 · 列 = 新值`；delete → `行锚`；insert → values JSON。
 */
export function describeEditItem(payload: CorrectionInput): string {
  if (payload.op === "update") {
    return `${payload.row_id} · ${payload.column} = ${String(payload.value)}`;
  }
  if (payload.op === "delete") return payload.row_id;
  return JSON.stringify(payload.values);
}

/** 单条保存结果：ok=已生效；失败携带原始错误（UI 用 apiErrorText 取服务端 detail）。 */
export interface SaveOutcome {
  item: EditQueueItem;
  ok: boolean;
  error?: unknown;
}

/**
 * 按入队顺序逐条提交（严格串行保序）；单条失败不中断后续条目（失败隔离），
 * 也不对失败条目自动重试——调用方按返回序列逐条呈现结果，全部提交完后
 * 由调用方刷新预览与修正历史。
 */
export async function saveEditQueue(
  items: EditQueueItem[],
  submit: (payload: CorrectionInput) => Promise<void>,
): Promise<SaveOutcome[]> {
  const outcomes: SaveOutcome[] = [];
  for (const item of items) {
    try {
      await submit(item.payload);
      outcomes.push({ item, ok: true });
    } catch (error) {
      outcomes.push({ item, ok: false, error });
    }
  }
  return outcomes;
}
