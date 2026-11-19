package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// TestEndToEndRunOnceSyncsFileSource 是全链集成测试：
// 真配置文件 + 真 file 适配器 + 真调度器/心跳 + 假 wire api，
// 走 `run --once` 验证 心跳 → 配置轮询 → begin → chunk → commit 全序列。
func TestEndToEndRunOnceSyncsFileSource(t *testing.T) {
	var mu sync.Mutex
	var paths []string
	var heartbeats []map[string]any
	var beginBody map[string]any
	var chunkBody map[string]any
	var committed bool

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		auth := r.Header.Get("Authorization")
		if r.URL.Path != "/api/v1/connectors/pair" && auth != "Bearer fwc_e2e" {
			t.Errorf("%s 缺少连接器 key 鉴权: %q", r.URL.Path, auth)
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		switch {
		case r.URL.Path == "/api/v1/connector/heartbeat":
			mu.Lock()
			heartbeats = append(heartbeats, body)
			mu.Unlock()
			fmt.Fprint(w, `{"poll_interval_seconds":10,"status":"online"}`)
		case r.URL.Path == "/api/v1/connector/config":
			fmt.Fprintf(w, `{"connector_id":"conn-e2e","poll_interval_seconds":10,`+
				`"min_sync_interval_seconds":900,"limits":{"max_rows":500000},"datasets":[
				{"dataset_key":"sales_orders","name":"订单","source":"reports","source_ref":"sales.csv","schedule_cron":"*/5 * * * *"},
				{"dataset_key":"legacy","source":"missing_db","source_ref":"public.legacy"}]}`)
		case r.URL.Path == "/api/v1/connector/syncs":
			mu.Lock()
			beginBody = body
			mu.Unlock()
			fmt.Fprint(w, `{"sync_id":"sync-e2e"}`)
		case strings.HasSuffix(r.URL.Path, "/chunks"):
			mu.Lock()
			chunkBody = body
			mu.Unlock()
			fmt.Fprint(w, `{"accepted":3}`)
		case strings.HasSuffix(r.URL.Path, "/commit"):
			mu.Lock()
			committed = true
			mu.Unlock()
			fmt.Fprint(w, `{"status":"ok","rows":3}`)
		default:
			t.Errorf("意外请求 %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `{"detail":"no route"}`)
		}
	}))
	defer srv.Close()

	dir := t.TempDir()
	site := filepath.Join(dir, "site")
	if err := os.MkdirAll(site, 0o700); err != nil {
		t.Fatal(err)
	}
	csv := "id,name,amount\n1,甲,12.5\n2,乙,7\n3,丙,3.25\n"
	if err := os.WriteFile(filepath.Join(site, "sales.csv"), []byte(csv), 0o600); err != nil {
		t.Fatal(err)
	}
	cfgPath := filepath.Join(dir, "config.json")
	writeConfig(t, cfgPath, fmt.Sprintf(
		`{"api_url":%q,"connector_key":"fwc_e2e","sources":[{"name":"reports","kind":"file","path":%q}]}`,
		srv.URL, site))

	if err := run([]string{"run", "--config", cfgPath, "--once"}); err != nil {
		t.Fatalf("run --once: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	want := []string{
		"/api/v1/connector/heartbeat",
		"/api/v1/connector/config",
		"/api/v1/connector/syncs",
		"/api/v1/connector/syncs/sync-e2e/chunks",
		"/api/v1/connector/syncs/sync-e2e/commit",
		"/api/v1/connector/heartbeat", // --once 的收尾心跳（带出本轮状态与告警）
	}
	if fmt.Sprint(paths) != fmt.Sprint(want) {
		t.Fatalf("调用序列 = %v\nwant %v", paths, want)
	}
	if len(heartbeats) != 2 || heartbeats[0]["version"] == "" || heartbeats[0]["version"] == nil {
		t.Fatalf("心跳应上报版本: %#v", heartbeats)
	}
	// 收尾心跳带出本轮结果：sales_orders 同步成功 + legacy 源缺失告警。
	final := heartbeats[len(heartbeats)-1]
	if state, ok := final["sync_state"].(map[string]any); !ok || state["sales_orders"] != "ok" {
		t.Fatalf("收尾心跳应带同步状态: %#v", final)
	}
	if warnings, ok := final["warnings"].([]any); !ok || len(warnings) != 1 || !strings.Contains(fmt.Sprint(warnings[0]), "missing_db") {
		t.Fatalf("收尾心跳应带源缺失告警: %#v", final)
	}
	if blob, _ := json.Marshal(heartbeats); strings.Contains(string(blob), "dsn") || strings.Contains(string(blob), site) {
		t.Fatalf("心跳不得含客户源凭据/路径: %s", blob)
	}
	if !committed {
		t.Fatal("未提交")
	}
	columns, _ := json.Marshal(beginBody["columns"])
	if !strings.Contains(string(columns), `"type":"INTEGER"`) || !strings.Contains(string(columns), `"type":"REAL"`) {
		t.Fatalf("begin 列类型推断异常: %s", columns)
	}
	if pk, ok := beginBody["pk_columns"].([]any); !ok || len(pk) != 0 {
		t.Fatalf("文件源无主键，pk_columns 应为 []: %#v", beginBody["pk_columns"])
	}
	if got := beginBody["dataset_key"]; got != "sales_orders" {
		t.Fatalf("dataset_key = %#v", got)
	}
	rows, _ := chunkBody["rows"].([]any)
	if len(rows) != 3 {
		t.Fatalf("chunk 行数 = %d, want 3", len(rows))
	}
	first, _ := rows[0].(map[string]any)
	if first["name"] != "甲" || first["id"] != float64(1) {
		t.Fatalf("行内容异常: %#v", first)
	}
	if idx, ok := chunkBody["index"].(float64); !ok || idx != 0 {
		t.Fatalf("chunk index = %#v, want 0", chunkBody["index"])
	}
}

// TestEndToEndSkipsDatasetWithMissingLocalSource 覆盖「白名单源本地不存在 → 跳过并在心跳回报」。
func TestEndToEndSkipsDatasetWithMissingLocalSource(t *testing.T) {
	var mu sync.Mutex
	var synced []string
	var lastHeartbeat map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		switch {
		case r.URL.Path == "/api/v1/connector/heartbeat":
			mu.Lock()
			lastHeartbeat = body
			mu.Unlock()
			fmt.Fprint(w, `{"poll_interval_seconds":10}`)
		case r.URL.Path == "/api/v1/connector/config":
			fmt.Fprint(w, `{"datasets":[{"dataset_key":"legacy","source":"missing_db","source_ref":"public.legacy"}]}`)
		case r.URL.Path == "/api/v1/connector/syncs":
			mu.Lock()
			synced = append(synced, body["dataset_key"].(string))
			mu.Unlock()
			fmt.Fprint(w, `{"sync_id":"sync-x"}`)
		default:
			fmt.Fprint(w, `{"detail":"no route"}`)
		}
	}))
	defer srv.Close()

	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.json")
	writeConfig(t, cfgPath, fmt.Sprintf(`{"api_url":%q,"connector_key":"fwc_e2e","sources":[]}`, srv.URL))
	if err := run([]string{"run", "--config", cfgPath, "--once"}); err != nil {
		t.Fatalf("run --once: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(synced) != 0 {
		t.Fatalf("源缺失的数据集不应推送: %v", synced)
	}
	if lastHeartbeat == nil {
		t.Fatal("应发生过心跳")
	}
	warnings, ok := lastHeartbeat["warnings"].([]any)
	if !ok || len(warnings) != 1 || !strings.Contains(fmt.Sprint(warnings[0]), "missing_db") {
		t.Fatalf("收尾心跳应回报本地未配置的源: %#v", lastHeartbeat)
	}
}
