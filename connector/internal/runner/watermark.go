package runner

import (
	"fmt"
	"strconv"
	"time"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

// watermarkKind 区分水位值的比较语义；同型取最大，型冲突（异常情况）取后到者。
type watermarkKind int

const (
	kindNone watermarkKind = iota
	kindInt
	kindFloat
	kindTime
	kindString
)

// watermarkTracker 追踪一轮同步中水位列的最大值（纯状态结构，便于单测）。
// 行值已是适配器收敛后的三型值（int64/float64/string/nil），time.Time /
// []byte / 窄整型按驱动原生值兜底支持。
type watermarkTracker struct {
	column string
	kind   watermarkKind
	has    bool
	maxInt int64
	maxFlt float64
	maxTm  time.Time
	maxStr string
}

func newWatermarkTracker(column string) *watermarkTracker {
	return &watermarkTracker{column: column}
}

// Observe 逐行取 row[column] 并保留最大值；nil（NULL/缺列）跳过。
func (t *watermarkTracker) Observe(rows []adapters.Row) {
	for _, row := range rows {
		v, ok := row[t.column]
		if !ok || v == nil {
			continue
		}
		t.observe(v)
	}
}

func (t *watermarkTracker) observe(v any) {
	switch x := v.(type) {
	case int64:
		t.setInt(x)
	case int:
		t.setInt(int64(x))
	case int8:
		t.setInt(int64(x))
	case int16:
		t.setInt(int64(x))
	case int32:
		t.setInt(int64(x))
	case uint:
		t.setInt(int64(x))
	case uint8:
		t.setInt(int64(x))
	case uint16:
		t.setInt(int64(x))
	case uint32:
		t.setInt(int64(x))
	case uint64:
		t.setInt(int64(x))
	case float32:
		t.setFloat(float64(x))
	case float64:
		t.setFloat(x)
	case time.Time:
		t.setTime(x)
	case string:
		t.setString(x)
	case []byte:
		t.setString(string(x))
	default:
		t.setString(fmt.Sprintf("%v", x)) // 与 adapters.Coerce 兜底同款文本化
	}
}

func (t *watermarkTracker) setInt(v int64) {
	if t.has && t.kind == kindInt && t.maxInt >= v {
		return
	}
	t.kind, t.maxInt, t.has = kindInt, v, true
}

func (t *watermarkTracker) setFloat(v float64) {
	if t.has && t.kind == kindFloat && t.maxFlt >= v {
		return
	}
	t.kind, t.maxFlt, t.has = kindFloat, v, true
}

func (t *watermarkTracker) setTime(v time.Time) {
	if t.has && t.kind == kindTime && !v.After(t.maxTm) {
		return
	}
	t.kind, t.maxTm, t.has = kindTime, v, true
}

func (t *watermarkTracker) setString(v string) {
	if t.has && t.kind == kindString && v <= t.maxStr {
		return
	}
	t.kind, t.maxStr, t.has = kindString, v, true
}

// Value 返回本轮最大水位的字符串形式；无有效值（零行或全 NULL）返回 nil。
// 字符串化规则：整数 → 十进制（保 int64 精度）；float → 'g' 最短表示；
// time → UTC RFC3339Nano；string 原样。
func (t *watermarkTracker) Value() *string {
	if !t.has {
		return nil
	}
	var s string
	switch t.kind {
	case kindInt:
		s = strconv.FormatInt(t.maxInt, 10)
	case kindFloat:
		s = strconv.FormatFloat(t.maxFlt, 'g', -1, 64)
	case kindTime:
		s = t.maxTm.UTC().Format(time.RFC3339Nano)
	default:
		s = t.maxStr
	}
	return &s
}
