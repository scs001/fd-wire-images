// Package client 是 wire api 的连接器面客户端（契约 §3.3）。
//
// 纪律：
//   - TLS 强制 https；仅本机回环地址允许显式 http（本地开发），其余 scheme 直接拒绝。
//   - 5xx 与网络错误按指数退避重试；4xx 立即失败并记录原因（契约 §5）。
//   - 客户端只发送原始行与元数据，永不上传 DSN/凭据（云端零凭据）。
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// 连接器面端点（契约 §3.3 冻结路径）。
const (
	PathPair      = "/api/v1/connectors/pair"
	PathHeartbeat = "/api/v1/connector/heartbeat"
	PathConfig    = "/api/v1/connector/config"
	PathSyncs     = "/api/v1/connector/syncs"
)

const (
	// DefaultMaxAttempts 是含首次在内的最大尝试次数。
	DefaultMaxAttempts = 4
	defaultBaseBackoff = 500 * time.Millisecond
	defaultMaxBackoff  = 20 * time.Second
	defaultHTTPTimeout = 120 * time.Second
)

// Client 是连接器面 api 客户端。零值不可用，用 New 构造。
type Client struct {
	baseURL     string
	key         string
	version     string
	http        *http.Client
	maxAttempts int
	baseBackoff time.Duration
	maxBackoff  time.Duration
	sleep       func(context.Context, time.Duration)
	userAgent   string
}

// Option 定制客户端（测试用）。
type Option func(*Client)

// WithHTTPClient 替换底层 http.Client。
func WithHTTPClient(hc *http.Client) Option { return func(c *Client) { c.http = hc } }

// WithRetry 设置最大尝试次数与退避基数。
func WithRetry(maxAttempts int, base time.Duration) Option {
	return func(c *Client) {
		if maxAttempts > 0 {
			c.maxAttempts = maxAttempts
		}
		if base > 0 {
			c.baseBackoff = base
		}
	}
}

// WithSleep 替换休眠实现（测试免等待）。
func WithSleep(fn func(context.Context, time.Duration)) Option {
	return func(c *Client) {
		if fn != nil {
			c.sleep = fn
		}
	}
}

// WithVersion 设置上报版本（由 ldflags 注入后传入）。
func WithVersion(v string) Option { return func(c *Client) { c.version = v } }

// New 校验 api_url 并构造客户端。
func New(apiURL, key string, opts ...Option) (*Client, error) {
	raw := strings.TrimSpace(apiURL)
	if raw == "" {
		return nil, errors.New("api_url 不能为空")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("api_url 无效: %w", err)
	}
	switch u.Scheme {
	case "https":
	case "http":
		if !isLoopback(u.Hostname()) {
			return nil, fmt.Errorf("api_url %q 使用明文 http：仅本机回环地址可用于本地开发，其余地址必须 https（契约 §5 传输加密）", raw)
		}
	default:
		return nil, fmt.Errorf("api_url 必须为 http(s)，实际 %q", u.Scheme)
	}
	if u.Host == "" {
		return nil, fmt.Errorf("api_url %q 缺少主机名", raw)
	}
	c := &Client{
		baseURL:     strings.TrimRight(u.String(), "/"),
		key:         strings.TrimSpace(key),
		http:        &http.Client{Timeout: defaultHTTPTimeout},
		maxAttempts: DefaultMaxAttempts,
		baseBackoff: defaultBaseBackoff,
		maxBackoff:  defaultMaxBackoff,
		sleep:       sleepCtx,
		userAgent:   "fd-wire-connector",
	}
	for _, opt := range opts {
		opt(c)
	}
	return c, nil
}

// APIURL 返回规整后的基地址。
func (c *Client) APIURL() string { return c.baseURL }

// Version 返回上报版本。
func (c *Client) Version() string { return c.version }

// APIError 是 api 返回的非 2xx 响应。
type APIError struct {
	Status int
	Detail string
	Path   string
	Body   string
}

func (e *APIError) Error() string {
	detail := e.Detail
	if detail == "" {
		detail = strings.TrimSpace(e.Body)
	}
	if detail == "" {
		detail = http.StatusText(e.Status)
	}
	return fmt.Sprintf("api %s 返回 %d: %s", e.Path, e.Status, detail)
}

// DriftError 是 409（契约 §3.3：syncs/drift、chunks/wrong_index）。
type DriftError struct {
	*APIError
	Code        string
	Differences []string
}

func (e *DriftError) Unwrap() error { return e.APIError }

// QuotaError 是 429（存储/行数配额超限）。
type QuotaError struct {
	*APIError
	Reason string
}

func (e *QuotaError) Unwrap() error { return e.APIError }

// IsDrift、IsQuota、IsUnauthorized、IsNotFound 判定错误类别。
func IsDrift(err error) bool {
	var target *DriftError
	return errors.As(err, &target)
}

func IsQuota(err error) bool {
	var target *QuotaError
	return errors.As(err, &target)
}

func IsUnauthorized(err error) bool {
	var target *APIError
	if !errors.As(err, &target) {
		return false
	}
	return target.Status == http.StatusUnauthorized || target.Status == http.StatusForbidden
}

func IsNotFound(err error) bool {
	var target *APIError
	return errors.As(err, &target) && target.Status == http.StatusNotFound
}

// do 发一次带重试的 JSON 请求。payload 为 nil 时不带体；out 为 nil 时丢弃响应体。
func (c *Client) do(ctx context.Context, method, path string, payload, out any) error {
	var body []byte
	if payload != nil {
		var err error
		body, err = json.Marshal(payload)
		if err != nil {
			return fmt.Errorf("序列化 %s 请求失败: %w", path, err)
		}
	}
	var lastErr error
	for attempt := 1; attempt <= c.maxAttempts; attempt++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, bytes.NewReader(body))
		if err != nil {
			return fmt.Errorf("构造 %s 请求失败: %w", path, err)
		}
		req.Header.Set("Accept", "application/json")
		req.Header.Set("User-Agent", c.userAgent)
		if payload != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		if c.key != "" {
			// 连接器 key 只用于连接器面；api 侧保证它在 MCP 读端点一律无效（契约 §3.3）。
			req.Header.Set("Authorization", "Bearer "+c.key)
		}
		resp, err := c.http.Do(req)
		if err != nil {
			if ctxErr := ctx.Err(); ctxErr != nil {
				return ctxErr
			}
			lastErr = fmt.Errorf("请求 %s 失败（第 %d/%d 次）: %w", path, attempt, c.maxAttempts, err)
			if attempt < c.maxAttempts {
				c.backoff(ctx, attempt)
				continue
			}
			return lastErr
		}
		respBody, readErr := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		resp.Body.Close()
		if readErr != nil {
			lastErr = fmt.Errorf("读取 %s 响应失败（第 %d/%d 次）: %w", path, attempt, c.maxAttempts, readErr)
			if attempt < c.maxAttempts {
				c.backoff(ctx, attempt)
				continue
			}
			return lastErr
		}
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			if out == nil || len(bytes.TrimSpace(respBody)) == 0 {
				return nil
			}
			if err := json.Unmarshal(respBody, out); err != nil {
				return fmt.Errorf("解析 %s 响应失败: %w", path, err)
			}
			return nil
		}
		apiErr := newAPIError(path, resp.StatusCode, respBody)
		if resp.StatusCode >= 500 && attempt < c.maxAttempts {
			lastErr = apiErr
			c.backoff(ctx, attempt)
			continue
		}
		return apiErr
	}
	return lastErr
}

// backoff 指数退避（带抖动，封顶 maxBackoff）。
func (c *Client) backoff(ctx context.Context, attempt int) {
	delay := c.baseBackoff << (attempt - 1)
	if delay > c.maxBackoff || delay <= 0 {
		delay = c.maxBackoff
	}
	jitter := time.Duration(rand.Int63n(int64(delay/2 + 1)))
	c.sleep(ctx, delay/2+jitter)
}

func sleepCtx(ctx context.Context, d time.Duration) {
	if d <= 0 {
		return
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

func newAPIError(path string, status int, body []byte) error {
	detail, code, differences, reason := parseErrorBody(body)
	base := &APIError{Status: status, Detail: detail, Path: path, Body: string(body)}
	switch status {
	case http.StatusConflict:
		return &DriftError{APIError: base, Code: code, Differences: differences}
	case http.StatusTooManyRequests:
		return &QuotaError{APIError: base, Reason: reason}
	default:
		return base
	}
}

// parseErrorBody 容忍两种报文：契约 §1 的 {"detail":"..."} 与带结构化字段的
// {"detail":{"code":...,"message":...,"differences":[...],"reason":"..."}}。
func parseErrorBody(body []byte) (detail, code string, differences []string, reason string) {
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) == 0 {
		return "", "", nil, ""
	}
	var envelope struct {
		Detail json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal(trimmed, &envelope); err != nil || len(envelope.Detail) == 0 {
		return strings.TrimSpace(string(trimmed)), "", nil, ""
	}
	var text string
	if err := json.Unmarshal(envelope.Detail, &text); err == nil {
		detail = text
		if i := strings.Index(text, ":"); i > 0 && !strings.Contains(text[:i], " ") {
			code = strings.TrimSpace(text[:i])
		}
		return detail, code, nil, ""
	}
	var obj struct {
		Code        string   `json:"code"`
		Message     string   `json:"message"`
		Detail      string   `json:"detail"`
		Reason      string   `json:"reason"`
		Differences []string `json:"differences"`
	}
	if err := json.Unmarshal(envelope.Detail, &obj); err != nil {
		return strings.TrimSpace(string(envelope.Detail)), "", nil, ""
	}
	detail = obj.Message
	if detail == "" {
		detail = obj.Detail
	}
	if detail == "" {
		detail = strings.TrimSpace(string(envelope.Detail))
	}
	return detail, obj.Code, obj.Differences, obj.Reason
}

func isLoopback(host string) bool {
	switch host {
	case "127.0.0.1", "::1", "localhost":
		return true
	}
	return strings.HasPrefix(host, "127.")
}
