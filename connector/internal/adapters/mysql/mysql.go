// Package mysql 是 MySQL 源适配器（go-sql-driver/mysql）：列/主键取自 information_schema，
// 全量拉取逐行流式读取（database/sql 按需取包，不在客户端整表进内存）。
package mysql

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"

	_ "github.com/go-sql-driver/mysql" // 注册 mysql 驱动

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

// Adapter 读取 dsn 指向库中的 Table（可写 "db.table"）。
type Adapter struct {
	DSN       string
	Table     string
	BatchSize int
}

// New 建 MySQL 适配器；BatchSize<=0 时用 2000。
func New(dsn, table string, batchSize int) *Adapter {
	if batchSize <= 0 {
		batchSize = 2000
	}
	return &Adapter{DSN: dsn, Table: strings.TrimSpace(table), BatchSize: batchSize}
}

func (a *Adapter) databaseTable() (database, table string, err error) {
	if a.Table == "" {
		return "", "", fmt.Errorf("mysql 适配器缺少 source_ref（应为表名，可带库名前缀）")
	}
	parts := strings.Split(a.Table, ".")
	switch len(parts) {
	case 1:
		return "", parts[0], nil
	case 2:
		return parts[0], parts[1], nil
	default:
		return "", "", fmt.Errorf("source_ref %q 不是合法的表名", a.Table)
	}
}

func (a *Adapter) open(ctx context.Context) (*sql.DB, error) {
	db, err := sql.Open("mysql", a.DSN)
	if err != nil {
		return nil, fmt.Errorf("初始化 MySQL 连接失败: %w", err)
	}
	// 单连接串行读取：整表快照不需要连接池，也避免长事务占住多个连接。
	db.SetMaxOpenConns(1)
	db.SetConnMaxLifetime(0)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("连接 MySQL 失败: %w", err)
	}
	return db, nil
}

// Columns 从 information_schema 读列名与类型并映射到引擎三型。
func (a *Adapter) Columns(ctx context.Context) ([]adapters.Column, error) {
	database, table, err := a.databaseTable()
	if err != nil {
		return nil, err
	}
	db, err := a.open(ctx)
	if err != nil {
		return nil, err
	}
	defer db.Close()

	const q = `SELECT column_name, data_type
	           FROM information_schema.columns
	           WHERE table_name = ?
	             AND table_schema = COALESCE(NULLIF(?, ''), DATABASE())
	           ORDER BY ordinal_position`
	rows, err := db.QueryContext(ctx, q, table, database)
	if err != nil {
		return nil, fmt.Errorf("查询 %s 列定义失败: %w", a.Table, err)
	}
	defer rows.Close()
	var cols []adapters.Column
	for rows.Next() {
		var name, dataType string
		if err := rows.Scan(&name, &dataType); err != nil {
			return nil, fmt.Errorf("读取列定义失败: %w", err)
		}
		cols = append(cols, adapters.Column{Name: name, Type: adapters.MapSQLType(dataType)})
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("读取列定义失败: %w", err)
	}
	if len(cols) == 0 {
		return nil, fmt.Errorf("表 %s 不存在或无可见列（受限账号请确认库级权限）", a.Table)
	}
	return cols, nil
}

// PrimaryKeys 读主键列（无主键返回空表，由引擎行走哈希锚）。
func (a *Adapter) PrimaryKeys(ctx context.Context) ([]string, error) {
	database, table, err := a.databaseTable()
	if err != nil {
		return nil, err
	}
	db, err := a.open(ctx)
	if err != nil {
		return nil, err
	}
	defer db.Close()

	const q = `SELECT column_name
	           FROM information_schema.columns
	           WHERE table_name = ?
	             AND table_schema = COALESCE(NULLIF(?, ''), DATABASE())
	             AND column_key = 'PRI'
	           ORDER BY ordinal_position`
	rows, err := db.QueryContext(ctx, q, table, database)
	if err != nil {
		return nil, fmt.Errorf("查询 %s 主键失败: %w", a.Table, err)
	}
	defer rows.Close()
	var keys []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, fmt.Errorf("读取主键失败: %w", err)
		}
		keys = append(keys, name)
	}
	return keys, rows.Err()
}

// Snapshot 全量拉取：SELECT * 逐行扫描，按批次回调。
func (a *Adapter) Snapshot(ctx context.Context, fn adapters.SnapshotFunc) error {
	database, table, err := a.databaseTable()
	if err != nil {
		return err
	}
	db, err := a.open(ctx)
	if err != nil {
		return err
	}
	defer db.Close()
	return a.streamQuery(ctx, db, "SELECT * FROM "+quoteQualified(database, table), nil, fn)
}

// Incremental 增量拉取：只读水位列 > after 的行，按水位列 ASC 有序回调。
// MySQL 字符串参数与列比较走隐式类型转换，无需显式 CAST。
func (a *Adapter) Incremental(ctx context.Context, column, after string, fn adapters.SnapshotFunc) error {
	database, table, err := a.databaseTable()
	if err != nil {
		return err
	}
	db, err := a.open(ctx)
	if err != nil {
		return err
	}
	defer db.Close()
	return a.streamQuery(ctx, db, incrementalQuery(database, table, column), []any{after}, fn)
}

// incrementalQuery 构造增量拉取 SQL；表/列标识符反引号引用（与 Snapshot 同一转义方式）。
func incrementalQuery(database, table, column string) string {
	col := quoteIdent(column)
	return "SELECT * FROM " + quoteQualified(database, table) + " WHERE " + col + " > ? ORDER BY " + col
}

// streamQuery 流式扫描一条查询并按批次回调；行值 → 引擎三型的收敛与
// Snapshot/Incremental 完全一致。
func (a *Adapter) streamQuery(ctx context.Context, db *sql.DB, query string, args []any, fn adapters.SnapshotFunc) error {
	rows, err := db.QueryContext(ctx, query, args...)
	if err != nil {
		return fmt.Errorf("查询 %s 失败: %w", a.Table, err)
	}
	defer rows.Close()

	names, err := rows.Columns()
	if err != nil {
		return fmt.Errorf("读取 %s 列名失败: %w", a.Table, err)
	}
	types, err := rows.ColumnTypes()
	if err != nil {
		return fmt.Errorf("读取 %s 列类型失败: %w", a.Table, err)
	}
	declared := make([]string, len(names))
	for i, ct := range types {
		declared[i] = adapters.MapSQLType(ct.DatabaseTypeName())
	}

	batch := adapters.NewBatcher(a.BatchSize, fn)
	raw := make([]sql.RawBytes, len(names))
	dest := make([]any, len(names))
	for i := range raw {
		dest[i] = &raw[i]
	}
	for rows.Next() {
		if err := rows.Scan(dest...); err != nil {
			return fmt.Errorf("读取 %s 数据行失败: %w", a.Table, err)
		}
		row := make(adapters.Row, len(names))
		for i, name := range names {
			if raw[i] == nil {
				row[name] = nil // NULL
				continue
			}
			row[name] = adapters.Coerce(string(raw[i]), declared[i])
		}
		if err := batch.Add(row); err != nil {
			return err
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("读取 %s 数据失败: %w", a.Table, err)
	}
	return batch.Flush()
}

// NormalizeValue 供测试与驱动兜底使用：把原生值收敛为 JSON 可编码值。
func NormalizeValue(value any) any {
	switch v := value.(type) {
	case time.Time:
		return v.UTC().Format(time.RFC3339Nano)
	default:
		return adapters.Coerce(value, adapters.TypeText)
	}
}

// quoteIdent 反引号引用单个标识符（表名/列名共用；内部反引号加倍，杜绝注入）。
func quoteIdent(part string) string {
	return "`" + strings.ReplaceAll(part, "`", "``") + "`"
}

// quoteQualified 组合反引号表名（标识符自己转义，杜绝注入）。
func quoteQualified(parts ...string) string {
	quoted := make([]string, 0, len(parts))
	for _, p := range parts {
		if strings.TrimSpace(p) == "" {
			continue
		}
		quoted = append(quoted, quoteIdent(p))
	}
	return strings.Join(quoted, ".")
}
