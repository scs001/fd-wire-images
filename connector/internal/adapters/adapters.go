// Package adapters 定义源适配器统一接口与三型列映射（契约 §5）。
//
// 一期实现 postgres / mysql / file 三个子包；新源型只加子包，核心
// （配对、轮询、推送、心跳）无需变更（spec「新源型不改核心」）。
package adapters

import (
	"context"
	"fmt"
	"strconv"
	"strings"
)

// 引擎三型（契约 §1）；映射不确定一律 TEXT（保守）。
const (
	TypeInteger = "INTEGER"
	TypeReal    = "REAL"
	TypeText    = "TEXT"
)

// Column 是一列的声明（name + 引擎三型之一）。
type Column struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

// Row 是一行数据，键为列名。
type Row = map[string]any

// SnapshotFunc 接收一个批次的原始行；返回错误即中止快照。
type SnapshotFunc func(rows []Row) error

// Adapter 是源适配器统一接口（契约 §5 冻结签名）。
//
// 适配器只负责把源里的原始行按批次推给回调；行哈希由引擎在 ingest 时统一
// 计算（契约 §1），连接器不参与。
type Adapter interface {
	Columns(ctx context.Context) ([]Column, error)
	PrimaryKeys(ctx context.Context) ([]string, error)
	Snapshot(ctx context.Context, fn SnapshotFunc) error
}

// IncrementalReader 是适配器的可选增量读取能力（数据库源实现；文件源不实现）。
type IncrementalReader interface {
	// Incremental 按水位列有序（ASC）回调 col > after 的行；after 是字符串形式的水位值。
	Incremental(ctx context.Context, column, after string, fn SnapshotFunc) error
}

// ColumnNames 抽出列名列表。
func ColumnNames(cols []Column) []string {
	names := make([]string, 0, len(cols))
	for _, c := range cols {
		names = append(names, c.Name)
	}
	return names
}

// MapSQLType 把源库类型名映射到引擎三型；不确定一律 TEXT（契约 §1）。
// 入参可以是 pg 的 udt_name（int4/numeric/...），也可以是 mysql 的
// DatabaseTypeName（INT/DECIMAL/...）或 information_schema 的 data_type。
func MapSQLType(dbTypeName string) string {
	name := strings.ToUpper(strings.TrimSpace(dbTypeName))
	if i := strings.IndexAny(name, "( "); i > 0 { // NUMERIC(10,2) / DOUBLE PRECISION 保留全词
		if !strings.Contains(name, "DOUBLE PRECISION") && !strings.Contains(name, "CHARACTER VARYING") {
			name = strings.TrimSpace(name[:i])
		}
	}
	name = strings.TrimSuffix(name, " UNSIGNED")
	switch name {
	case "SMALLINT", "INTEGER", "INT", "INT2", "INT4", "INT8", "BIGINT", "MEDIUMINT",
		"TINYINT", "SERIAL", "SMALLSERIAL", "BIGSERIAL", "SERIAL2", "SERIAL4", "SERIAL8",
		"OID", "YEAR", "BIT", "INT32", "INT64", "SMALLINT UNSIGNED", "INTEGER UNSIGNED",
		"MEDIUMINT UNSIGNED", "TINYINT UNSIGNED":
		return TypeInteger
	case "REAL", "FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "NUMERIC",
		"DECIMAL", "MONEY", "SMALLMONEY":
		return TypeReal
	default:
		// BOOLEAN / DATE / TIME / TIMESTAMP / VARCHAR / TEXT / JSON / UUID / ...
		return TypeText
	}
}

// InferType 按 §3.4 规则对样本值推断列类型：
// 全部可解析 INTEGER → INTEGER；否则全部可解析 REAL → REAL；否则 TEXT。
// 空串视为缺失值，不影响判定；样本全空或无可解析值 → TEXT（保守）。
func InferType(sample []string) string {
	sawValue := false
	intOK, realOK := true, true
	for _, raw := range sample {
		v := strings.TrimSpace(raw)
		if v == "" {
			continue
		}
		sawValue = true
		if _, err := strconv.ParseInt(v, 10, 64); err != nil {
			intOK = false
		}
		if _, err := strconv.ParseFloat(v, 64); err != nil {
			realOK = false
		}
		if !intOK && !realOK {
			return TypeText
		}
	}
	if !sawValue {
		return TypeText
	}
	if intOK {
		return TypeInteger
	}
	if realOK {
		return TypeReal
	}
	return TypeText
}

// ConvertString 把文本值按目标三型转换；空串转 nil（NULL）。
// 数值型解析失败返回错误（诚实失败优于静默写脏数据）。
func ConvertString(value, columnType string) (any, error) {
	v := strings.TrimSpace(value)
	switch columnType {
	case TypeInteger:
		if v == "" {
			return nil, nil
		}
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("值 %q 无法解析为 INTEGER", value)
		}
		return n, nil
	case TypeReal:
		if v == "" {
			return nil, nil
		}
		f, err := strconv.ParseFloat(v, 64)
		if err != nil {
			return nil, fmt.Errorf("值 %q 无法解析为 REAL", value)
		}
		return f, nil
	default:
		if v == "" {
			return nil, nil
		}
		return value, nil
	}
}

// Coerce 把驱动返回的原生值收敛到引擎三型可接受的 JSON 值。
func Coerce(value any, columnType string) any {
	switch v := value.(type) {
	case nil:
		return nil
	case string:
		if columnType == TypeInteger || columnType == TypeReal {
			if out, err := ConvertString(v, columnType); err == nil {
				return out
			}
		}
		return v
	case []byte:
		return Coerce(string(v), columnType)
	case int:
		return int64(v)
	case int8:
		return int64(v)
	case int16:
		return int64(v)
	case int32:
		return int64(v)
	case int64:
		if columnType == TypeReal {
			return float64(v)
		}
		return v
	case uint:
		return int64(v)
	case uint8:
		return int64(v)
	case uint16:
		return int64(v)
	case uint32:
		return int64(v)
	case uint64:
		return int64(v)
	case float32:
		if columnType == TypeInteger {
			return int64(v)
		}
		return float64(v)
	case float64:
		if columnType == TypeInteger && v == float64(int64(v)) {
			return int64(v)
		}
		return v
	case bool:
		return strconv.FormatBool(v)
	default:
		return fmt.Sprintf("%v", v)
	}
}

// Batcher 把逐行数据聚成批次（默认 2000 行 = 契约 §3.3 单块上限）。
type Batcher struct {
	size int
	rows []Row
	fn   SnapshotFunc
}

// NewBatcher 建批次收集器；size<=0 时用 2000。
func NewBatcher(size int, fn SnapshotFunc) *Batcher {
	if size <= 0 {
		size = 2000
	}
	return &Batcher{size: size, fn: fn}
}

// Add 追加一行，积满一批即回调。
func (b *Batcher) Add(row Row) error {
	b.rows = append(b.rows, row)
	if len(b.rows) >= b.size {
		return b.Flush()
	}
	return nil
}

// Flush 回调剩余行（空缓冲不回调）。
func (b *Batcher) Flush() error {
	if len(b.rows) == 0 {
		return nil
	}
	batch := b.rows
	b.rows = nil
	return b.fn(batch)
}
