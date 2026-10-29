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

// TestEndToEndRestSourceTwoRounds 是 REST 源的端到端验收（wire-rest-connector
// 任务 3.1）：真上游 HTTP 服务（分页 + 需鉴权）+ 真 REST 适配器 + 真调度器，
// 走两次 `run --once`：
//
//   - 第一轮（云端无水位）：全量分页拉全 → begin/chunk/commit；
//   - 第二轮（云端水位=90）：只推水位之后的行 → commit 携带新水位；
//   - 云端下发的 source_config 来自 api（此处用假 api 复刻同形），
//     认证凭据只在本地配置里——假 api 收到的任何报文都不得含 token。
func TestEndToEndRestSourceTwoRounds(t *testing.T) {
	const bearer = "tok-local-only-must-not-leak"

	// 上游：两页订单，第二页空页终止；水位字段 meta.seq。
	var upstreamMu sync.Mutex
	var upstreamHits int
	var sawAuth string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamMu.Lock()
		upstreamHits++
		sawAuth = r.Header.Get("Authorization")
		upstreamMu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		page := r.URL.Query().Get("page")
		switch page {
		case "1":
			fmt.Fprint(w, `{"data":{"items":[
				{"id":10,"buyer":{"name":"甲"},"amount":12.5,"meta":{"seq":10}},
				{"id":20,"buyer":{"name":"乙"},"amount":7,"meta":{"seq":20}},
				{"id":90,"buyer":{"name":"丙"},"amount":3.25,"meta":{"seq":90}}]}}`)
		case "2":
			fmt.Fprint(w, `{"data":{"items":[
				{"id":120,"buyer":{"name":"丁"},"amount":1,"meta":{"seq":120}}]}}`)
		default:
			fmt.Fprint(w, `{"data":{"items":[]}}`)
		}
	}))
	defer upstream.Close()

	// 假 wire api：第一轮水位空，第二轮给 90。
	var mu sync.Mutex
	var roundOne, roundTwo bool
	var commits []map[string]any
	var wireBodies []string
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path != "/api/v1/connectors/pair" &&
			r.Header.Get("Authorization") != "Bearer fwc_rest" {
			t.Errorf("%s 缺少连接器 key 鉴权", r.URL.Path)
		}
		body := map[string]any{}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if blob, err := json.Marshal(body); err == nil {
			mu.Lock()
			wireBodies = append(wireBodies, string(blob))
			mu.Unlock()
		}
		switch {
		case r.URL.Path == "/api/v1/connector/heartbeat":
			fmt.Fprint(w, `{"poll_interval_seconds":10}`)
		case r.URL.Path == "/api/v1/connector/config":
			mu.Lock()
			watermark := ""
			if roundOne {
				watermark = "90"
				roundTwo = true
			}
			roundOne = true
			mu.Unlock()
			// 云端形状：URL/分页/投影/水位路径 —— 零凭据
			fmt.Fprintf(w, `{"datasets":[{
				"dataset_key":"orders_api",
				"name":"订单 API",
				"source":"orders-api",
				"source_ref":"orders",
				"schedule_cron":"*/5 * * * *",
				"sync_mode":%q,
				"watermark_column":%q,
				"watermark_value":%q,
				"source_config":{
					"url":%q,
					"method":"GET",
					"root_path":"data.items",
					"projection":[
						{"path":"id","column":"order_id","type":"INTEGER"},
						{"path":"buyer.name","column":"buyer_name","type":"TEXT"},
						{"path":"amount","column":"amount","type":"REAL"}],
					"pagination":{"mode":"page","page_param":"page",
						"size_param":"size","start_page":1,"page_size":2},
					"watermark_path":"meta.seq"}}]}`,
				"incremental", "order_id", watermark, upstream.URL)
		case r.URL.Path == "/api/v1/connector/syncs":
			fmt.Fprint(w, `{"sync_id":"sync-rest"}`)
		case strings.HasSuffix(r.URL.Path, "/chunks"):
			rows, _ := body["rows"].([]any)
			fmt.Fprintf(w, `{"accepted":%d}`, len(rows))
		case strings.HasSuffix(r.URL.Path, "/commit"):
			mu.Lock()
			commits = append(commits, body)
			mu.Unlock()
			fmt.Fprint(w, `{"status":"ok","rows":1}`)
		default:
			t.Errorf("意外请求 %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `{"detail":"no route"}`)
		}
	}))
	defer api.Close()

	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.json")
	// 凭据只在本地配置（0600）：bearer token 写在这里，云端下发的形状没有它。
	writeConfig(t, cfgPath, fmt.Sprintf(
		`{"api_url":%q,"connector_key":"fwc_rest","sources":[
			{"name":"orders-api","kind":"rest",
			 "auth":{"type":"bearer","token":%q}}]}`, api.URL, bearer))

	// 第一轮：全量（云端无水位）→ 4 行；首同步建基线并上报初始水位（全量里
	// 最大 seq=120，而不是第二页的 90——水位取本轮最大）。
	if err := run([]string{"run", "--config", cfgPath, "--once"}); err != nil {
		t.Fatalf("round 1: %v", err)
	}
	mu.Lock()
	if len(commits) != 1 {
		mu.Unlock()
		t.Fatalf("第一轮应提交 1 次，实际 %d", len(commits))
	}
	if got := fmt.Sprint(commits[0]["watermark_value"]); got != "120" {
		mu.Unlock()
		t.Fatalf("首轮 baseline 应上报本轮最大水位 120，实际 %q", got)
	}
	mu.Unlock()

	// 第二轮：云端水位 90 → 只推 seq>90 的 1 行（id=120），上报新水位
	if err := run([]string{"run", "--config", cfgPath, "--once"}); err != nil {
		t.Fatalf("round 2: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if !roundTwo {
		t.Fatal("第二轮 config 未被请求")
	}
	if len(commits) != 2 {
		t.Fatalf("两轮共应提交 2 次，实际 %d", len(commits))
	}
	if got := fmt.Sprint(commits[1]["watermark_value"]); got != "120" {
		t.Fatalf("第二轮水位应上报 120，实际 %q（完整 %#v）", got, commits[1])
	}

	// 凭据纪律：假 api 收到的全部报文不得含本地 bearer token
	for _, blob := range wireBodies {
		if strings.Contains(blob, bearer) {
			t.Fatalf("云端报文泄漏本地凭据: %s", blob)
		}
	}
	// 上游确实收到了本地凭据（适配器装配正确）
	if sawAuth != "Bearer "+bearer {
		t.Fatalf("上游未收到本地认证头: %q", sawAuth)
	}
	// 分页确实发生了（第一轮至少两页）
	if upstreamHits < 2 {
		t.Fatalf("分页未发生，上游命中 %d", upstreamHits)
	}
	_ = os.Getenv // 保持 os 导入（writeConfig 已用）
}
