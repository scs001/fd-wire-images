package client

import (
	"context"
	"errors"
	"fmt"
	"strings"
)

// PairRequest 是 POST /api/v1/connectors/pair 的请求体（契约 §3.3）。
type PairRequest struct {
	Token   string `json:"token"`
	Name    string `json:"name"`
	Version string `json:"version"`
}

// PairResponse 是配对成功响应；connector_key 仅此一次明文返回。
type PairResponse struct {
	ConnectorKey string `json:"connector_key"`
	ConnectorID  string `json:"connector_id"`
	Status       string `json:"status,omitempty"`
}

// Pair 用一次性配对令牌换取连接器 key（fwc_）。
// 不携带既有 key（本调用本身就是取得 key 的方式）。
func Pair(ctx context.Context, apiURL, token, name, version string, opts ...Option) (*PairResponse, error) {
	token = strings.TrimSpace(token)
	if !strings.HasPrefix(token, "fwp_") {
		return nil, errors.New("配对令牌应以 fwp_ 开头（控制台「配对向导」生成）")
	}
	if strings.TrimSpace(name) == "" {
		name = "fd-wire-connector"
	}
	c, err := New(apiURL, "", opts...)
	if err != nil {
		return nil, err
	}
	var resp PairResponse
	if err := c.do(ctx, "POST", PathPair, PairRequest{Token: token, Name: name, Version: version}, &resp); err != nil {
		return nil, err
	}
	if !strings.HasPrefix(resp.ConnectorKey, "fwc_") {
		return nil, fmt.Errorf("配对响应缺少合法 connector_key（得到 %q）", resp.ConnectorKey)
	}
	if resp.ConnectorID == "" {
		return nil, errors.New("配对响应缺少 connector_id")
	}
	return &resp, nil
}
