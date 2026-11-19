package client

import (
	"context"
	"time"
)

// DefaultHeartbeatInterval 是心跳与配置轮询的缺省周期（契约 §5：默认 60s）。
const DefaultHeartbeatInterval = 60 * time.Second

// minPollInterval / maxPollInterval 夹紧云端下发的轮询周期。
const (
	minPollInterval = 10 * time.Second
	maxPollInterval = time.Hour
)

// HeartbeatRequest 是 POST /api/v1/connector/heartbeat 的请求体（契约 §3.3）。
// Warnings 回报本地检测到的异常（如白名单数据集指向本地未配置的源）；
// 只含诊断文本，不含任何凭据。
type HeartbeatRequest struct {
	Version   string            `json:"version"`
	SyncState map[string]string `json:"sync_state,omitempty"`
	Warnings  []string          `json:"warnings,omitempty"`
}

// HeartbeatResponse 只需 poll_interval_seconds（其余字段容忍）。
type HeartbeatResponse struct {
	PollIntervalSeconds int    `json:"poll_interval_seconds"`
	Status              string `json:"status,omitempty"`
}

// Heartbeat 上报心跳与版本；返回云端下发的轮询周期。
func (c *Client) Heartbeat(ctx context.Context, req HeartbeatRequest) (*HeartbeatResponse, error) {
	if req.Version == "" {
		req.Version = c.version
	}
	var resp HeartbeatResponse
	if err := c.do(ctx, "POST", PathHeartbeat, req, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

// Interval 把云端下发的周期夹紧成可用的 time.Duration；缺失或非法取 fallback。
func (r *HeartbeatResponse) Interval(fallback time.Duration) time.Duration {
	if fallback <= 0 {
		fallback = DefaultHeartbeatInterval
	}
	if r == nil || r.PollIntervalSeconds <= 0 {
		return fallback
	}
	d := time.Duration(r.PollIntervalSeconds) * time.Second
	if d < minPollInterval {
		return minPollInterval
	}
	if d > maxPollInterval {
		return maxPollInterval
	}
	return d
}
