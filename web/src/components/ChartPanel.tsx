// Empty echarts container: the chart instance, resize and dispose lifecycle are
// real from day one; data wiring arrives with the analysis console slices.
import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import { useTranslation } from "react-i18next";

interface ChartPanelProps {
  title?: string;
  height?: number;
}

export function ChartPanel({ title, height = 320 }: ChartPanelProps) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    chart.setOption({ series: [] });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, []);

  return (
    <section className="rounded-lg border border-slate-200 p-4">
      {title && (
        <h3 className="text-sm font-medium text-slate-600 mb-2">{title}</h3>
      )}
      <div className="relative">
        <div ref={ref} style={{ height }} data-testid="chart-container" />
        <div className="absolute inset-0 flex items-center justify-center text-sm text-slate-400 pointer-events-none">
          {t("console.chartEmpty")}
        </div>
      </div>
    </section>
  );
}
