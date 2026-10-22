// CSV 导出纯函数（wire-dashboard-export 任务 1.1）：看板整板客户端本地导出
// ——面板快照本就内嵌在产物 spec 里，零请求零新端点即可合成整板单文件。
// 运行时零依赖（仅从 spec.ts 引入类型）；RFC4180 转义 + UTF-8 BOM + CRLF
// 行尾，Excel 直接双击打开不乱码。
import type { DashboardPanel } from "./dashboard/spec";

/**
 * RFC4180 单格转义：含逗号/双引号/换行（\r 或 \n）→ 双引号包裹 + 内部
 * 引号翻倍；null → 空串（快照缺格子的如实空值）；数值 String() 原样
 * （不加引号、不失真）。
 */
export function csvEscape(value: string | number | null): string {
  if (value === null) return "";
  const text = String(value);
  if (/[",\r\n]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

/**
 * 列头 + 数据行 → CSV 表体：每行按列序拼接（列头同样走转义），
 * CRLF 行尾（RFC4180 口径），末行不带尾换行。
 */
export function toCsv(
  columns: string[],
  rows: Array<Array<string | number | null>>,
): string {
  const lines = [columns.map(csvEscape).join(",")];
  for (const row of rows) {
    lines.push(row.map(csvEscape).join(","));
  }
  return lines.join("\r\n");
}

/**
 * 整板合成（导出文件全文）：UTF-8 BOM 前缀（Excel 中文识别）+ 逐面板
 * section——`# {面板标题}（{type}）` 注释行 + 面板快照表（toCsv）+ 空行
 * 分隔；无快照面板只有注释行（如实空结构，不报错）；panels 空/legacy
 * （null）→ 仅 `# {title}` 一行（空板如实）。行尾 CRLF。
 *
 * v2（wire-chart-contract-v2）：raw_option 面板按其自含的 series 抽表参与
 * 导出；抽不出表格化数据时该 section 标注未导出并**继续导出其余面板**——
 * 一个面板表达不了表格不该毁掉整份导出。
 */
export function boardToCsv(
  title: string,
  panels: DashboardPanel[] | null,
): string {
  let sections: string[];
  if (panels === null || panels.length === 0) {
    // legacy（无面板结构）与空板：整板只有标题行。
    sections = [`# ${title}`];
  } else {
    sections = panels.map((panel) => {
      const comment = `# ${panel.title}（${panel.type}）`;
      if (panel.snapshot) {
        return `${comment}\r\n${toCsv(panel.snapshot.columns, panel.snapshot.rows)}`;
      }
      if (panel.raw_option !== undefined) {
        const extracted = rawOptionToTable(panel.raw_option);
        return extracted === null
          ? `${comment}\r\n# （该面板的数据无法表格化，未导出）`
          : `${comment}\r\n${toCsv(extracted.columns, extracted.rows)}`;
      }
      return comment;
    });
  }
  return `\uFEFF${sections.join("\r\n\r\n")}`;
}

/**
 * 从 raw_option 的 series 抽表格：每个系列一列，x 轴类目作首列（若声明）。
 * 抽不出（无 series / 系列无 data 数组 / 空数据）返回 null。
 *
 * 与渲染器 `extractRawSeries` 同口径（那边面向 UI，这边面向导出）——
 * 两处一致才能保证「看到的表 = 导出的表」。
 */
export function rawOptionToTable(
  raw: Record<string, unknown>,
): { columns: string[]; rows: Array<Array<string | number | null>> } | null {
  const series = raw.series;
  if (!Array.isArray(series) || series.length === 0) return null;
  const objects = series.filter(
    (entry): entry is Record<string, unknown> =>
      typeof entry === "object" && entry !== null && !Array.isArray(entry),
  );
  if (objects.length === 0) return null;

  const columns: string[] = [];
  const valueColumns: Array<Array<string | number | null>> = [];
  for (const entry of objects) {
    const data = entry.data;
    if (!Array.isArray(data)) return null;
    const name =
      typeof entry.name === "string" && entry.name !== ""
        ? entry.name
        : `series${valueColumns.length + 1}`;
    columns.push(name);
    valueColumns.push(
      data.map((cell): string | number | null => {
        if (Array.isArray(cell)) {
          const candidate = cell[1];
          return typeof candidate === "number" || typeof candidate === "string"
            ? candidate
            : null;
        }
        return typeof cell === "number" || typeof cell === "string" ? cell : null;
      }),
    );
  }

  const rowCount = Math.max(...valueColumns.map((values) => values.length));
  if (rowCount === 0) return null;

  const xAxis = raw.xAxis;
  const axisList = Array.isArray(xAxis) ? xAxis : [xAxis];
  const firstAxis = axisList.find(
    (axis): axis is Record<string, unknown> =>
      typeof axis === "object" && axis !== null && !Array.isArray(axis),
  );
  const categories = firstAxis?.data;
  const hasCategories = Array.isArray(categories) && categories.length > 0;

  return {
    columns: hasCategories ? ["x", ...columns] : columns,
    rows: Array.from({ length: rowCount }, (_, r) => [
      ...(hasCategories
        ? [
            typeof categories[r] === "number" || typeof categories[r] === "string"
              ? (categories[r] as string | number)
              : null,
          ]
        : []),
      ...valueColumns.map((values) => values[r] ?? null),
    ]),
  };
}
