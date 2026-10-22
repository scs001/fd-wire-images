package client

import (
	"context"
	"errors"
	"fmt"
	"net/url"
)

// MaxChunkRows 是单块行数上限（契约 §3.3：rows ≤ 2000）。
const MaxChunkRows = 2000

// Column 是推送列声明（引擎三型）。
type Column struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

// SyncBeginRequest 是 POST /api/v1/connector/syncs 的请求体。
type SyncBeginRequest struct {
	DatasetKey string   `json:"dataset_key"`
	Columns    []Column `json:"columns"`
	PKColumns  []string `json:"pk_columns"`
	RowCount   *int     `json:"row_count,omitempty"`
}

// SyncBeginResponse 返回本次同步句柄。
type SyncBeginResponse struct {
	SyncID       string `json:"sync_id"`
	DatasetKey   string `json:"dataset_key,omitempty"`
	IngestID     string `json:"ingest_id,omitempty"`
	StagingTable string `json:"staging_table,omitempty"`
}

// SyncChunkRequest 是 POST /api/v1/connector/syncs/{sid}/chunks 的请求体。
type SyncChunkRequest struct {
	Index int              `json:"index"`
	Rows  []map[string]any `json:"rows"`
}

// SyncChunkResponse 返回实际接收行数。
type SyncChunkResponse struct {
	Accepted int `json:"accepted"`
}

// SyncCommitRequest 是 POST /api/v1/connector/syncs/{sid}/commit 的请求体。
type SyncCommitRequest struct {
	// WatermarkValue 上报本轮同步的最大水位（增量模式）；nil = 不上报
	// （全量模式 / 增量窗口无新行 / 源不支持增量），云端水位保持不动。
	WatermarkValue *string `json:"watermark_value,omitempty"`
}

// SyncCommitResponse 返回提交结果。
type SyncCommitResponse struct {
	Status string `json:"status"`
	Rows   int    `json:"rows"`
}

// SyncBegin 开启一次全量同步：api 侧建档 + 引擎 ingest_begin。
// 409 是 schema 漂移（api 不调引擎），429 是配额超限，均由调用方分类处理。
func (c *Client) SyncBegin(ctx context.Context, req SyncBeginRequest) (*SyncBeginResponse, error) {
	if req.DatasetKey == "" {
		return nil, errors.New("sync begin 缺少 dataset_key")
	}
	if len(req.Columns) == 0 {
		return nil, errors.New("sync begin 缺少列定义")
	}
	var resp SyncBeginResponse
	if err := c.do(ctx, "POST", PathSyncs, req, &resp); err != nil {
		return nil, err
	}
	if resp.SyncID == "" {
		return nil, errors.New("sync begin 响应缺少 sync_id")
	}
	return &resp, nil
}

// SyncChunk 推送一块数据（index 为起始行序，防重放校验）。
func (c *Client) SyncChunk(ctx context.Context, syncID string, index int, rows []map[string]any) (*SyncChunkResponse, error) {
	if syncID == "" {
		return nil, errors.New("sync chunk 缺少 sync_id")
	}
	if len(rows) == 0 {
		return &SyncChunkResponse{}, nil
	}
	if len(rows) > MaxChunkRows {
		return nil, fmt.Errorf("单块 %d 行超过上限 %d（契约 §3.3）", len(rows), MaxChunkRows)
	}
	var resp SyncChunkResponse
	path := syncPath(syncID, "chunks")
	if err := c.do(ctx, "POST", path, SyncChunkRequest{Index: index, Rows: rows}, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

// SyncCommit 提交同步（引擎按服务端语义整表替换或 merge）；req.WatermarkValue
// 非空时随 commit 上报本轮最大水位，云端以此推进增量游标。
func (c *Client) SyncCommit(ctx context.Context, syncID string, req SyncCommitRequest) (*SyncCommitResponse, error) {
	if syncID == "" {
		return nil, errors.New("sync commit 缺少 sync_id")
	}
	var resp SyncCommitResponse
	if err := c.do(ctx, "POST", syncPath(syncID, "commit"), req, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

// SyncAbort 中止同步并清理服务端中间态（失败路径必调）。
func (c *Client) SyncAbort(ctx context.Context, syncID string) error {
	if syncID == "" {
		return nil
	}
	return c.do(ctx, "POST", syncPath(syncID, "abort"), struct{}{}, nil)
}

func syncPath(syncID, action string) string {
	return PathSyncs + "/" + url.PathEscape(syncID) + "/" + action
}
