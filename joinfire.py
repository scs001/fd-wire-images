import json, urllib.request, time
key = open('/opt/cell-manager/secrets/wgk-drillcust001-customer.txt').read().strip()
url = "https://mcp.finddatatech.cloud/fd-wire-cell-drillcust001/mcp"
sid = None
def rpc(method, params, notify=False):
    global sid
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if not notify: body["id"] = 1
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {key}", **({"Mcp-Session-Id": sid} if sid else {})})
    with urllib.request.urlopen(req, timeout=110) as r:
        sid = sid or r.headers.get("Mcp-Session-Id")
        raw = r.read().decode()
    if not raw.strip(): return None
    for line in raw.splitlines():
        if line.startswith("data:"): raw = line[5:].strip(); break
    return json.loads(raw) if raw.strip() else None
def call(name, args):
    r = rpc("tools/call", {"name": name, "arguments": args})
    return json.loads(r.get("result", {}).get("content", [{}])[0].get("text", "null"))
rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "join-fire", "version": "1"}})
rpc("notifications/initialized", {}, notify=True)
t0 = time.time()
r = call("customer_dataset_join_indicators", {
    "child_key": "smoke_with_pmi",
    "parent_key": "smoke",
    "indicators": [{"concept_id": 5, "as": "pmi"}],
    "date_column": "date",
})
print("join(%.1fs) ->" % (time.time()-t0), json.dumps({k: r.get(k) for k in ("dataset_key","rows","kind","unresolved_regions","error")}, ensure_ascii=False))
q = call("customer_dataset_query", {"dataset_key": "smoke_with_pmi", "sort": [{"column": "date", "dir": "asc"}]})
print("child rows:", json.dumps(q.get("rows"), ensure_ascii=False)[:500])
