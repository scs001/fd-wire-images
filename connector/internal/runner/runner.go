// Package runner 编排单次同步：begin → chunk(≤2000 行) → commit，
// 任一环节失败即 abort（契约 §5）。行哈希不在连接器计算（引擎 ingest 时统一算）。
// 数据集配了增量模式时改走适配器增量读取（只拉水位之后的行），并在 commit
// 上报本轮最大水位；引擎侧 merge 语义由服务端负责，begin 请求不变。
package runner

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
	"github.com/FindDataTechnology/fd-wire/connector/internal/client"
)

// abortTimeout 是失败路径 abort 调用的独立超时（父 ctx 已取消时仍要尽力清理）。
const abortTimeout = 20 * time.Second

// AdapterFactory 由云端数据集解析出本地适配器；源不存在时返回错误。
type AdapterFactory func(ds client.DatasetConfig) (adapters.Adapter, error)

// Runner 执行单数据集同步。
type Runner struct {
	Client  *client.Client
	Factory AdapterFactory

	// BatchSize 是单块行数上限；<=0 或 >2000 时用 2000（契约 §3.3）。
	BatchSize int

	Logf func(format string, args ...any)
}

// Result 是一次同步的结果。
type Result struct {
	Rows       int // 实际推送行数
	Chunks     int // 块数
	CommitRows int // 引擎侧提交行数（commit 响应）
}

// Sync 执行一次同步（全量或增量，由云端 sync_mode 决定）。任何错误都会
// 尽力 abort 服务端中间态后返回。
func (r *Runner) Sync(ctx context.Context, ds client.DatasetConfig) (result Result, err error) {
	if r.Client == nil {
		return result, errors.New("runner 缺少 api 客户端")
	}
	if r.Factory == nil {
		return result, errors.New("runner 缺少适配器工厂")
	}
	adapter, err := r.Factory(ds)
	if err != nil {
		return result, err
	}
	r.logf("数据集 %s：读取源 %s/%s", ds.Key, ds.Source, ds.SourceRef)
	columns, err := adapter.Columns(ctx)
	if err != nil {
		return result, fmt.Errorf("读取源结构失败: %w", err)
	}
	primaryKeys, err := adapter.PrimaryKeys(ctx)
	if err != nil {
		return result, fmt.Errorf("读取源主键失败: %w", err)
	}
	begin, err := r.Client.SyncBegin(ctx, client.SyncBeginRequest{
		DatasetKey: ds.Key,
		Columns:    toClientColumns(columns),
		PKColumns:  nonNilStrings(primaryKeys),
	})
	if err != nil {
		return result, err
	}
	syncID := begin.SyncID
	committed := false
	defer func() {
		if committed {
			return
		}
		// 尽力清理：独立于已取消的父 ctx，避免服务端留下悬挂的 staging。
		abortCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), abortTimeout)
		defer cancel()
		if abortErr := r.Client.SyncAbort(abortCtx, syncID); abortErr != nil {
			r.logf("数据集 %s：abort %s 失败（服务端将自行过期清理）: %v", ds.Key, syncID, abortErr)
		}
	}()

	batchSize := r.BatchSize
	if batchSize <= 0 || batchSize > client.MaxChunkRows {
		batchSize = client.MaxChunkRows
	}
	index := 0
	pushBatch := func(rows []adapters.Row) error {
		for start := 0; start < len(rows); start += batchSize {
			end := start + batchSize
			if end > len(rows) {
				end = len(rows)
			}
			chunk := rows[start:end]
			if _, err := r.Client.SyncChunk(ctx, syncID, index, chunk); err != nil {
				return fmt.Errorf("推送第 %d 行起 %d 行失败: %w", index, len(chunk), err)
			}
			index += len(chunk)
			result.Chunks++
		}
		return nil
	}

	// 增量判定：数据集配了 incremental + 水位列，且适配器实现了增量读取。
	inc := ds.SyncMode == "incremental" && ds.WatermarkColumn != ""
	reader, canIncremental := adapter.(adapters.IncrementalReader)
	var tracker *watermarkTracker
	tracked := func(fn adapters.SnapshotFunc) adapters.SnapshotFunc {
		return func(rows []adapters.Row) error {
			tracker.Observe(rows)
			return fn(rows)
		}
	}

	var readErr error
	switch {
	case inc && canIncremental && ds.WatermarkValue != "":
		// a. 增量窗口拉取：只读水位之后的行，同时追踪新水位。
		tracker = newWatermarkTracker(ds.WatermarkColumn)
		r.logf("数据集 %s：增量拉取 %s > %s", ds.Key, ds.WatermarkColumn, ds.WatermarkValue)
		readErr = reader.Incremental(ctx, ds.WatermarkColumn, ds.WatermarkValue, tracked(pushBatch))
	case inc && canIncremental:
		// b. 首同步建基线：云端尚无水位，全量拉一遍并记录最大水位。
		tracker = newWatermarkTracker(ds.WatermarkColumn)
		r.logf("数据集 %s：增量模式首同步（云端无水位），全量建基线", ds.Key)
		readErr = adapter.Snapshot(ctx, tracked(pushBatch))
	case inc:
		// c. 源不支持增量读取（如文件源）：回退全量快照，同步照常成功；
		//    不上报新水位（云端水位不动）。
		r.logf("数据集 %s：源 %s 不支持增量读取，回退全量快照（云端水位保持不变）", ds.Key, ds.Source)
		readErr = adapter.Snapshot(ctx, pushBatch)
	default:
		// d. 全量模式：完全现状。
		readErr = adapter.Snapshot(ctx, pushBatch)
	}
	result.Rows = index
	if readErr != nil {
		return result, readErr
	}

	commitReq := client.SyncCommitRequest{}
	if tracker != nil {
		commitReq.WatermarkValue = tracker.Value() // 零行/全 NULL → nil，云端水位不动
	}
	commit, err := r.Client.SyncCommit(ctx, syncID, commitReq)
	if err != nil {
		return result, err
	}
	committed = true
	result.CommitRows = commit.Rows
	if inc {
		if commitReq.WatermarkValue != nil {
			r.logf("数据集 %s：同步完成，推送 %d 行 / %d 块，水位更新至 %s", ds.Key, result.Rows, result.Chunks, *commitReq.WatermarkValue)
		} else {
			r.logf("数据集 %s：同步完成，推送 %d 行 / %d 块，无新水位（云端水位不动）", ds.Key, result.Rows, result.Chunks)
		}
		return result, nil
	}
	r.logf("数据集 %s：同步完成，推送 %d 行 / %d 块", ds.Key, result.Rows, result.Chunks)
	return result, nil
}

func toClientColumns(cols []adapters.Column) []client.Column {
	out := make([]client.Column, 0, len(cols))
	for _, c := range cols {
		out = append(out, client.Column{Name: c.Name, Type: c.Type})
	}
	return out
}

// nonNilStrings 保证 JSON 里是 [] 而不是 null（api 侧按数组处理 pk_columns）。
func nonNilStrings(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

func (r *Runner) logf(format string, args ...any) {
	if r.Logf != nil {
		r.Logf(format, args...)
	}
}
