package runner

import (
	"testing"
	"time"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

func TestWatermarkTrackerIntPicksMax(t *testing.T) {
	tr := newWatermarkTracker("id")
	tr.Observe([]adapters.Row{{"id": int64(5), "v": "x"}, {"id": int64(9)}, {"id": int64(7)}})
	got := tr.Value()
	if got == nil || *got != "9" {
		t.Fatalf("整数最大值 = %#v, want 9", got)
	}
	// 窄整型与 int64 同型数值比较。
	tr2 := newWatermarkTracker("id")
	tr2.Observe([]adapters.Row{{"id": int32(3)}, {"id": int64(12)}, {"id": int(4)}})
	if got := tr2.Value(); got == nil || *got != "12" {
		t.Fatalf("混合整型最大值 = %#v, want 12", got)
	}
}

func TestWatermarkTrackerBigIntLossless(t *testing.T) {
	// 2^53+1 与 2^53+2 在 float64 下无法区分，整数路径必须按 int64 比较与字符串化。
	big1, big2 := int64(1)<<53+1, int64(1)<<53+2
	tr := newWatermarkTracker("id")
	tr.Observe([]adapters.Row{{"id": big1}})
	if got := tr.Value(); got == nil || *got != "9007199254740993" {
		t.Fatalf("大整数字符串化 = %#v, want 9007199254740993", got)
	}
	tr.Observe([]adapters.Row{{"id": big2}})
	if got := tr.Value(); got == nil || *got != "9007199254740994" {
		t.Fatalf("大整数取最大 = %#v, want 9007199254740994", got)
	}
	tr.Observe([]adapters.Row{{"id": big1}})
	if got := tr.Value(); got == nil || *got != "9007199254740994" {
		t.Fatalf("较小值不得回退水位: %#v", got)
	}
}

func TestWatermarkTrackerFloat(t *testing.T) {
	tr := newWatermarkTracker("amount")
	tr.Observe([]adapters.Row{{"amount": -0.5}, {"amount": 1.25}, {"amount": 0.75}})
	if got := tr.Value(); got == nil || *got != "1.25" {
		t.Fatalf("float 最大值 = %#v, want 1.25", got)
	}
	tr2 := newWatermarkTracker("amount")
	tr2.Observe([]adapters.Row{{"amount": 1e21}})
	if got := tr2.Value(); got == nil || *got != "1e+21" {
		t.Fatalf("float 'g' 最短表示 = %#v, want 1e+21", got)
	}
}

func TestWatermarkTrackerTime(t *testing.T) {
	early := time.Date(2026, 10, 6, 4, 0, 0, 0, time.UTC)
	late := time.Date(2026, 10, 6, 6, 30, 0, 500000000, time.UTC) // 带纳秒
	shanghai := late.In(time.FixedZone("CST", 8*3600))
	tr := newWatermarkTracker("updated_at")
	// 乱序行取最大；时区不同但同一时刻的较早值不覆盖。
	tr.Observe([]adapters.Row{{"updated_at": late}, {"updated_at": early}, {"updated_at": shanghai.Add(-time.Hour)}})
	got := tr.Value()
	if got == nil || *got != "2026-10-06T06:30:00.5Z" {
		t.Fatalf("time 最大值（UTC RFC3339Nano）= %#v, want 2026-10-06T06:30:00.5Z", got)
	}
}

func TestWatermarkTrackerStringAndBytes(t *testing.T) {
	tr := newWatermarkTracker("ref")
	tr.Observe([]adapters.Row{{"ref": "b"}, {"ref": "a"}, {"ref": []byte("c")}})
	if got := tr.Value(); got == nil || *got != "c" {
		t.Fatalf("字典序最大 = %#v, want c", got)
	}
}

func TestWatermarkTrackerNilAndMissingSkipped(t *testing.T) {
	tr := newWatermarkTracker("wm")
	tr.Observe([]adapters.Row{{"wm": nil}, {"other": int64(5)}, {}})
	if got := tr.Value(); got != nil {
		t.Fatalf("全 nil/缺列不应产生水位: %#v", got)
	}
	tr.Observe([]adapters.Row{{"wm": nil}, {"wm": int64(3)}})
	if got := tr.Value(); got == nil || *got != "3" {
		t.Fatalf("nil 跳过后应记录有效值: %#v", got)
	}
}

func TestWatermarkTrackerKindConflictTakesLater(t *testing.T) {
	// 异常情况：型冲突（先整数后字符串）→ 取后到的。
	tr := newWatermarkTracker("wm")
	tr.Observe([]adapters.Row{{"wm": int64(99)}})
	tr.Observe([]adapters.Row{{"wm": "abc"}})
	if got := tr.Value(); got == nil || *got != "abc" {
		t.Fatalf("型冲突应取后到者: %#v", got)
	}
}

func TestWatermarkTrackerUnknownTypeTextual(t *testing.T) {
	tr := newWatermarkTracker("wm")
	tr.Observe([]adapters.Row{{"wm": true}})
	if got := tr.Value(); got == nil || *got != "true" {
		t.Fatalf("未知类型按 %v 文本化: %#v", "%v", got)
	}
}
