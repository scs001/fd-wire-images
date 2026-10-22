package client

import (
	"context"
	"fmt"
	"time"
)

// DefaultMinSyncMinutes 是云端未下发时的最小同步间隔（与 api 侧
// FD_WIRE_DATASET_MIN_SYNC_MINUTES 默认一致，契约 §3.1）。
const DefaultMinSyncMinutes = 15

// ColumnSpec 是云端声明的列（首同步时为已知列；同步时以适配器实测列为准）。
type ColumnSpec struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

// DatasetConfig 是 GET /api/v1/connector/config 下发的单个数据集（契约 §3.3）。
// 云端响应断言零凭据字段；Source 只是本地 sources[].name 的映射键，
// SourceRef 是该源内的表名/文件名（连接器拉数对象）。
type DatasetConfig struct {
	Key            string       `json:"dataset_key"`
	Name           string       `json:"name,omitempty"`
	Source         string       `json:"source"`
	SourceRef      string       `json:"source_ref"`
	Columns        []ColumnSpec `json:"columns,omitempty"`
	PKColumns      []string     `json:"pk_columns,omitempty"`
	SyncCron       string       `json:"schedule_cron,omitempty"`
	SyncEnabled    *bool        `json:"sync_enabled,omitempty"`
	MinSyncMinutes int          `json:"min_sync_minutes,omitempty"`
	MaxRows        int64        `json:"max_rows,omitempty"`

	SyncMode        string `json:"sync_mode,omitempty"`        // ""|"snapshot"|"incremental"
	WatermarkColumn string `json:"watermark_column,omitempty"` // 增量水位列（sync_mode=incremental 时生效）
	WatermarkValue  string `json:"watermark_value,omitempty"`  // 字符串水位（保 int64 精度）；空 = 尚无基线
}

// RemoteLimits 是云端下发的全局上限/用量（契约 §3.3 config 响应）。
type RemoteLimits struct {
	MaxRows           int64 `json:"max_rows,omitempty"`
	StorageLimitBytes int64 `json:"storage_limit_bytes,omitempty"`
	StorageUsedBytes  int64 `json:"storage_used_bytes,omitempty"`
}

// RemoteConfig 是连接器配置轮询响应。
type RemoteConfig struct {
	ConnectorID            string          `json:"connector_id,omitempty"`
	PollIntervalSeconds    int             `json:"poll_interval_seconds,omitempty"`
	MinSyncIntervalSeconds int             `json:"min_sync_interval_seconds,omitempty"`
	MinSyncMinutes         int             `json:"min_sync_minutes,omitempty"` // 兼容旧字段
	Limits                 RemoteLimits    `json:"limits,omitempty"`
	Datasets               []DatasetConfig `json:"datasets"`
	Connector              struct {
		ID   string `json:"id,omitempty"`
		Name string `json:"name,omitempty"`
	} `json:"connector,omitempty"`
}

// Enabled 判断该数据集是否应参与调度（缺省启用）。
func (d DatasetConfig) Enabled() bool {
	return d.SyncEnabled == nil || *d.SyncEnabled
}

// MinSync 返回该数据集的最小同步间隔：数据集覆盖 > 云端全局 > 缺省 15 分钟。
func (d DatasetConfig) MinSync(global time.Duration) time.Duration {
	if d.MinSyncMinutes > 0 {
		return time.Duration(d.MinSyncMinutes) * time.Minute
	}
	if global > 0 {
		return global
	}
	return DefaultMinSyncMinutes * time.Minute
}

// MinSync 返回云端全局最小同步间隔（min_sync_interval_seconds 优先，秒制）；
// 缺省 15 分钟。
func (rc *RemoteConfig) MinSync() time.Duration {
	if rc != nil {
		if rc.MinSyncIntervalSeconds > 0 {
			return time.Duration(rc.MinSyncIntervalSeconds) * time.Second
		}
		if rc.MinSyncMinutes > 0 {
			return time.Duration(rc.MinSyncMinutes) * time.Minute
		}
	}
	return DefaultMinSyncMinutes * time.Minute
}

// Dataset 按键查找数据集。
func (rc *RemoteConfig) Dataset(key string) (DatasetConfig, bool) {
	if rc == nil {
		return DatasetConfig{}, false
	}
	for _, d := range rc.Datasets {
		if d.Key == key {
			return d, true
		}
	}
	return DatasetConfig{}, false
}

// FetchConfig 拉取白名单与调度配置。响应永不含凭据字段（api 侧有断言，契约 §3.5）。
func (c *Client) FetchConfig(ctx context.Context) (*RemoteConfig, error) {
	var rc RemoteConfig
	if err := c.do(ctx, "GET", PathConfig, nil, &rc); err != nil {
		return nil, err
	}
	for _, d := range rc.Datasets {
		if d.Key == "" {
			return nil, fmt.Errorf("云端配置含缺少 key 的数据集")
		}
	}
	return &rc, nil
}
