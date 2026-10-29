// Package rest 是通用 REST/JSON 源适配器（wire-rest-connector）：
//
// 云端下发**非敏感形状**（url/method/分页/投影/水位路径——认证凭据只在
// 连接器本地配置，ADR-0004），连接器解释执行：
//
//   - 投影：root_path 定位记录数组（点分路径，终态须为数组或单对象），
//     逐记录按 {path → column} 点分路径取值，类型显式声明或按样本推断
//     （抽样同 file 适配器 §3.4 口径：全 INTEGER→INTEGER，否则 REAL，否则 TEXT）；
//   - 分页：page 式（页码参数递增，空页/hasMore=false 终止）或 cursor 式
//     （响应路径取下一页游标，缺失终止）；max_pages 兜底防失控；
//   - 增量：客户端过滤（水位列值 > after，文本比较口径与数据库适配器的
//     WHERE 语义一致）+ 按水位列 ASC 排序（镜像 ORDER BY 语义）；无水位
//     路径的源不实现 IncrementalReader（回退全量，spec「文件源同语义」）；
//   - 错误诚实：非 2xx 带响应样例、JSON 解析失败、投影形状不命中、
//     429/5xx 有限退避（1s/2s/4s）后如实失败——绝不伪造空成功；
//   - 响应体上限（每页 50MB）与单轮行数上限（max_rows，缺省 500k 与
//     引擎单集上限同口径）。
//
// 凭据纪律：认证头只在请求时装配；错误文案与日志永不包含认证值。
package rest

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

const (
	// inferSampleRows 是列类型推断的抽样行数（契约 §3.4 同 file 口径）。
	inferSampleRows = 200
	// maxResponseBytes 是单页响应体上限。
	maxResponseBytes = 50 << 20
	// defaultMaxRows 是单轮行数上限（与引擎单数据集上限同口径）。
	defaultMaxRows = 500_000
	// maxPagesDefault 是分页单轮页数上限。
	maxPagesDefault = 100
	// retryDelays 是 429/5xx 的退避序列（有限次后如实失败）。
	retryDelays = 3
)

// Auth 是本地认证配置（镜像 config.RestAuth——适配器不 import config 包，
// 由工厂装配，测试可直构造）。
type Auth struct {
	Type     string // none | bearer | basic
	Token    string
	Username string
	Password string
}

// Config 是 REST 源形状（由工厂从 client.RestConfig 显式转换装配——本包
// 不 import client 包，测试可直构造）。
type Config struct {
	URL           string
	Method        string
	Headers       map[string]string
	RootPath      string
	Projection    []Projection
	Pagination    *Pagination
	WatermarkPath string
}

// Projection 是一个投影字段：JSON 路径 → 列名（Type 缺省按样本推断）。
type Projection struct {
	Path   string
	Column string
	Type   string
}

// Pagination 是分页声明（page 式或 cursor 式二选一；MaxPages 兜底防失控）。
type Pagination struct {
	Mode        string // page | cursor
	PageParam   string
	SizeParam   string
	StartPage   int
	CursorParam string
	CursorPath  string
	PageSize    int
	MaxPages    int
}

// Adapter 是 REST 源适配器（一个端点 = 一个数据集，source_ref 为端点别名）。
type Adapter struct {
	Config     Config
	Auth       *Auth
	BatchSize  int
	MaxRows    int
	HTTPClient *http.Client // 可注入（测试 httptest）；缺省 30s 超时
	Sleep      func(time.Duration) // 可注入（退避测试）
}

// New 建适配器；BatchSize<=0 用 2000，MaxRows<=0 用 500k。
func New(cfg Config, auth *Auth, batchSize int) *Adapter {
	if batchSize <= 0 {
		batchSize = 2000
	}
	maxRows := defaultMaxRows
	return &Adapter{
		Config:     cfg,
		Auth:       auth,
		BatchSize:  batchSize,
		MaxRows:    maxRows,
		Sleep:      time.Sleep,
	}
}

// SourceError 是形状/响应错误（文案含响应样例；永不包含认证值）。
type SourceError struct{ Msg string }

func (e *SourceError) Error() string { return e.Msg }

// columns 是推断后的列序（一次推断、快照复用）。
func (a *Adapter) columns(ctx context.Context, sample []map[string]any) ([]adapters.Column, error) {
	if len(a.Config.Projection) == 0 {
		return nil, &SourceError{Msg: "投影声明为空（projection 至少一个字段）"}
	}
	cols := make([]adapters.Column, 0, len(a.Config.Projection))
	for i, proj := range a.Config.Projection {
		name := strings.TrimSpace(proj.Column)
		if name == "" {
			return nil, &SourceError{Msg: fmt.Sprintf("投影[%d] 缺少 column", i)}
		}
		typ := strings.ToUpper(strings.TrimSpace(proj.Type))
		if typ == "" {
			typ = adapters.InferType(sampleValues(sample, proj.Path))
		}
		if typ != adapters.TypeInteger && typ != adapters.TypeReal {
			typ = adapters.TypeText
		}
		cols = append(cols, adapters.Column{Name: name, Type: typ})
	}
	return cols, nil
}

func sampleValues(sample []map[string]any, path string) []string {
	values := make([]string, 0, len(sample))
	for _, record := range sample {
		v, ok := walk(record, path)
		if !ok || v == nil {
			continue
		}
		values = append(values, fmt.Sprintf("%v", v))
		if len(values) >= inferSampleRows {
			break
		}
	}
	return values
}

// walk 按点分路径取值（数组下标用数字段；缺路径返回 ok=false）。
func walk(node any, path string) (any, bool) {
	if path == "" {
		return node, true
	}
	parts := strings.Split(path, ".")
	cur := node
	for _, part := range parts {
		switch node := cur.(type) {
		case map[string]any:
			next, ok := node[part]
			if !ok {
				return nil, false
			}
			cur = next
		case []any:
			idx, err := strconv.Atoi(part)
			if err != nil || idx < 0 || idx >= len(node) {
				return nil, false
			}
			cur = node[idx]
		default:
			return nil, false
		}
	}
	return cur, true
}

// records 解析一页响应 → 记录数组（root_path 定位；单对象包装为数组）。
func records(body []byte, rootPath string) ([]map[string]any, error) {
	var root any
	if err := json.Unmarshal(body, &root); err != nil {
		return nil, &SourceError{Msg: fmt.Sprintf("响应不是合法 JSON: %v；样例: %s", err, sampleOf(body))}
	}
	node, ok := walk(root, rootPath)
	if !ok {
		return nil, &SourceError{Msg: fmt.Sprintf("root_path %q 不在响应中；样例: %s", rootPath, sampleOf(body))}
	}
	switch node := node.(type) {
	case []any:
		records := make([]map[string]any, 0, len(node))
		for i, item := range node {
			obj, ok := item.(map[string]any)
			if !ok {
				return nil, &SourceError{Msg: fmt.Sprintf("root_path %q 的第 %d 项不是对象", rootPath, i)}
			}
			records = append(records, obj)
		}
		return records, nil
	case map[string]any:
		return []map[string]any{node}, nil
	default:
		return nil, &SourceError{Msg: fmt.Sprintf("root_path %q 指向的不是对象/数组；样例: %s", rootPath, sampleOf(body))}
	}
}

func sampleOf(body []byte) string {
	s := strings.TrimSpace(string(body))
	if len(s) > 200 {
		s = s[:200] + "…"
	}
	return s
}

// project 把一条记录按投影转成行（类型转换失败诚实报错）。
func (a *Adapter) project(record map[string]any, cols []adapters.Column) (adapters.Row, error) {
	row := adapters.Row{}
	for i, proj := range a.Config.Projection {
		raw, _ := walk(record, proj.Path)
		typ := cols[i].Type
		switch v := raw.(type) {
		case nil:
			row[proj.Column] = nil
		case string:
			converted, err := adapters.ConvertString(v, typ)
			if err != nil {
				return nil, &SourceError{Msg: fmt.Sprintf("列 %s 的值 %q 与声明类型 %s 不符", proj.Column, v, typ)}
			}
			row[proj.Column] = converted
		case float64:
			row[proj.Column] = adapters.Coerce(v, typ)
		case bool:
			if typ == adapters.TypeText {
				row[proj.Column] = strconv.FormatBool(v)
			} else {
				row[proj.Column] = v
			}
		default:
			row[proj.Column] = adapters.Coerce(fmt.Sprintf("%v", v), typ)
		}
	}
	return row, nil
}

// fetch 拉一页（认证 + 自定义头 + 退避 + 响应上限）。
func (a *Adapter) fetch(ctx context.Context, url string) ([]byte, error) {
	var lastErr error
	for attempt := 0; attempt <= retryDelays; attempt++ {
		if attempt > 0 {
			delay := time.Duration(1<<(attempt-1)) * time.Second // 1s/2s/4s
			if a.Sleep != nil {
				a.Sleep(delay)
			}
		}
		body, status, err := a.once(ctx, url)
		if err == nil {
			if status >= 200 && status < 300 {
				return body, nil
			}
			lastErr = &SourceError{Msg: fmt.Sprintf("上游 HTTP %d；样例: %s", status, sampleOf(body))}
			if status != 429 && status < 500 {
				return nil, lastErr // 4xx（除 429）不退避，如实失败
			}
			continue
		}
		lastErr = err
		// 网络/上下文错误：网络错误退避重试，上下文取消直接终止
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return nil, err
		}
	}
	return nil, lastErr
}

func (a *Adapter) once(ctx context.Context, url string) ([]byte, int, error) {
	method := strings.ToUpper(strings.TrimSpace(a.Config.Method))
	if method == "" {
		method = http.MethodGet
	}
	req, err := http.NewRequestWithContext(ctx, method, url, nil)
	if err != nil {
		return nil, 0, &SourceError{Msg: fmt.Sprintf("请求构造失败: %v", err)}
	}
	req.Header.Set("Accept", "application/json")
	for key, value := range a.Config.Headers {
		req.Header.Set(key, value)
	}
	// 认证头（本地凭据装配；错误与日志永不携带认证值）
	if a.Auth != nil {
		switch a.Auth.Type {
		case "bearer":
			req.Header.Set("Authorization", "Bearer "+a.Auth.Token)
		case "basic":
			req.SetBasicAuth(a.Auth.Username, a.Auth.Password)
		}
	}
	client := a.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return nil, resp.StatusCode, &SourceError{Msg: fmt.Sprintf("读取响应失败: %v", err)}
	}
	return body, resp.StatusCode, nil
}

// pageURL 构造第 page 页 / 携带 cursor 的请求地址。
func (a *Adapter) pageURL(base string, page int, cursor string) string {
	sep := "?"
	if strings.Contains(base, "?") {
		sep = "&"
	}
	pg := a.Config.Pagination
	if pg == nil {
		return base
	}
	// 追加一组 query 参数（跳过空参数名；首参用 sep，其后用 &）
	var sb strings.Builder
	sb.WriteString(base)
	appendParam := func(name, value string) {
		if name == "" || value == "" {
			return
		}
		sb.WriteString(sep)
		sb.WriteString(url.QueryEscape(name))
		sb.WriteString("=")
		sb.WriteString(url.QueryEscape(value))
		sep = "&"
	}
	size := ""
	if pg.PageSize > 0 {
		size = strconv.Itoa(pg.PageSize)
	}
	switch pg.Mode {
	case "page":
		start := pg.StartPage
		if start == 0 {
			start = 1
		}
		appendParam(pg.PageParam, strconv.Itoa(start+page))
		appendParam(pg.SizeParam, size)
	case "cursor":
		appendParam(pg.CursorParam, cursor)
		appendParam(pg.SizeParam, size)
	}
	return sb.String()
}

// readAll 分页拉全量并投影回调；返回末页游标（cursor 式）供遍历终止判定。
func (a *Adapter) readAll(ctx context.Context, fn adapters.SnapshotFunc) error {
	return a.readAllWith(ctx, fn, nil)
}

// readAllWith 同 readAll，但对每条记录额外回调 onRecord（原始记录 + 投影行）——
// 增量路径借此按声明的响应字段路径取水位值（该路径可能指向投影之外的字段，
// 投影行里没有它）。
func (a *Adapter) readAllWith(
	ctx context.Context,
	fn adapters.SnapshotFunc,
	onRecord func(record map[string]any, row adapters.Row),
) error {
	cols, err := a.columns(ctx, nil)
	if err != nil {
		return err
	}
	batch := adapters.NewBatcher(a.BatchSize, fn)
	maxPages := maxPagesDefault
	maxRows := a.MaxRows
	if maxRows <= 0 {
		maxRows = defaultMaxRows
	}
	// 首页样本推断类型（未显式声明时）
	first, err := a.fetch(ctx, a.pageURL(a.Config.URL, 0, ""))
	if err != nil {
		return err
	}
	firstRecords, err := records(first, a.Config.RootPath)
	if err != nil {
		return err
	}
	cols, err = a.columns(ctx, firstRecords)
	if err != nil {
		return err
	}
	cursor := ""
	page := 0
	total := 0
	for {
		var body []byte
		if page == 0 {
			body = first
		} else {
			if a.Config.Pagination == nil {
				break // 单页
			}
			body, err = a.fetch(ctx, a.pageURL(a.Config.URL, page, cursor))
			if err != nil {
				return err
			}
		}
		pageRecords, err := records(body, a.Config.RootPath)
		if err != nil {
			return err
		}
		for _, record := range pageRecords {
			row, err := a.project(record, cols)
			if err != nil {
				return err
			}
			total++
			if total > maxRows {
				return &SourceError{Msg: fmt.Sprintf("单轮行数超过上限 %d（诚实失败，不静默截断）", maxRows)}
			}
			if onRecord != nil {
				onRecord(record, row)
			}
			if err := batch.Add(row); err != nil {
				return err
			}
		}
		page++
		// 终止判定：无分页声明=单页；page 式空页终止；cursor 式无游标终止；
		// max_pages 兜底。
		if a.Config.Pagination == nil {
			break
		}
		switch a.Config.Pagination.Mode {
		case "page":
			if len(pageRecords) == 0 {
				return batch.Flush()
			}
		case "cursor":
			next, ok := walk(jsonToAny(body), a.Config.Pagination.CursorPath)
			if !ok || next == nil || fmt.Sprintf("%v", next) == "" {
				return batch.Flush()
			}
			cursor = fmt.Sprintf("%v", next)
		default:
			break
		}
		if page >= maxPages {
			return batch.Flush()
		}
	}
	return batch.Flush()
}

// jsonToAny 把已解码页体重新供 walk 使用（cursor 提取）。
func jsonToAny(body []byte) any {
	var root any
	_ = json.Unmarshal(body, &root)
	return root
}

// Columns 抽样推断列（首拉 + 投影）。
func (a *Adapter) Columns(ctx context.Context) ([]adapters.Column, error) {
	body, err := a.fetch(ctx, a.pageURL(a.Config.URL, 0, ""))
	if err != nil {
		return nil, err
	}
	firstRecords, err := records(body, a.Config.RootPath)
	if err != nil {
		return nil, err
	}
	return a.columns(ctx, firstRecords)
}

// NewFromClient 是工厂入口（形状由 main 显式转换）。

// 增量能力由**是否声明水位响应路径**决定（契约「REST 源适配器契约」）：
// 声明了 watermark_path 才实现 IncrementalReader；未声明时返回只做快照的
// 适配器——runner 按「源不支持增量」如实回退全量快照并在日志说明（与文件
// 源同语义），而不是悄悄按某个投影列过滤（那会与客户声明的口径不符）。
func NewFromClient(cfg Config, auth *Auth, batchSize int) adapters.Adapter {
	a := New(cfg, auth, batchSize)
	if strings.TrimSpace(cfg.WatermarkPath) == "" {
		return snapshotOnly{a: a}
	}
	return a
}

// snapshotOnly 隐藏 Incremental 能力（不嵌入 *Adapter——嵌入会把方法提升上来）。
type snapshotOnly struct{ a *Adapter }

func (s snapshotOnly) Columns(ctx context.Context) ([]adapters.Column, error) {
	return s.a.Columns(ctx)
}

func (s snapshotOnly) PrimaryKeys(ctx context.Context) ([]string, error) {
	return s.a.PrimaryKeys(ctx)
}

func (s snapshotOnly) Snapshot(ctx context.Context, fn adapters.SnapshotFunc) error {
	return s.a.Snapshot(ctx, fn)
}

// PrimaryKeys：REST 源无主键（无主键行走引擎行哈希锚定，file 源同语义）。
func (a *Adapter) PrimaryKeys(context.Context) ([]string, error) { return nil, nil }

// Snapshot 全量分页拉取。
func (a *Adapter) Snapshot(ctx context.Context, fn adapters.SnapshotFunc) error {
	return a.readAll(ctx, fn)
}

// Incremental 客户端过滤增量（水位值 > after，文本口径镜像数据库
// 「WHERE 水位列 > 水位值」）+ 按水位列 ASC 排序（镜像 ORDER BY）。
//
// 水位值来源：优先按**声明的响应字段路径**（``WatermarkPath``）从每条记录
// 提取——这正是「水位值经声明的响应字段路径从本轮数据提取」的落地；路径
// 在该记录里不命中时回退到投影出的水位列（两者同源时结果一致）。两者都
// 取不到的行如实跳过（该行没有可比较的水位，不能凭猜测纳入）。
func (a *Adapter) Incremental(ctx context.Context, column, after string, fn adapters.SnapshotFunc) error {
	path := strings.TrimSpace(a.Config.WatermarkPath)
	// 水位值随行收集：原始记录（供 watermark_path）+ 投影行一一对齐。
	type pending struct {
		row   adapters.Row
		value any
		ok    bool
	}
	var collected []pending
	err := a.readAllWith(ctx, func([]adapters.Row) error { return nil },
		func(record map[string]any, row adapters.Row) {
			entry := pending{row: row}
			if path != "" {
				if value, ok := walk(record, path); ok && value != nil {
					entry.value, entry.ok = value, true
				}
			}
			if !entry.ok {
				if value, ok := row[column]; ok && value != nil {
					entry.value, entry.ok = value, true
				}
			}
			collected = append(collected, entry)
		})
	if err != nil {
		return err
	}
	filtered := make([]pending, 0, len(collected))
	for _, entry := range collected {
		if !entry.ok {
			continue // 没有可比较的水位值：不凭猜测纳入
		}
		if greaterThan(entry.value, after) {
			filtered = append(filtered, entry)
		}
	}
	sort.SliceStable(filtered, func(i, j int) bool {
		return lessForSort(filtered[i].value, filtered[j].value)
	})
	batch := adapters.NewBatcher(a.BatchSize, fn)
	for _, entry := range filtered {
		if err := batch.Add(entry.row); err != nil {
			return err
		}
	}
	return batch.Flush()
}

// greaterThan 判断水位值是否晚于 after：两侧都能解析为数字时按数值比较
// （INTEGER/REAL 列的水位必须如此——"120" > "90" 的文本比较会错判为假），
// 否则回退文本比较（镜像数据库对文本列的 WHERE 语义）。
func greaterThan(v any, after string) bool {
	s := fmt.Sprintf("%v", v)
	if vf, verr := strconv.ParseFloat(s, 64); verr == nil {
		if af, aerr := strconv.ParseFloat(after, 64); aerr == nil {
			return vf > af
		}
	}
	return s > after
}

// lessForSort ASC 排序比较（数值优先，文本兜底）。
func lessForSort(aAny, bAny any) bool {
	as, bs := fmt.Sprintf("%v", aAny), fmt.Sprintf("%v", bAny)
	af, aerr := strconv.ParseFloat(as, 64)
	bf, berr := strconv.ParseFloat(bs, 64)
	if aerr == nil && berr == nil {
		return af < bf
	}
	return as < bs
}
