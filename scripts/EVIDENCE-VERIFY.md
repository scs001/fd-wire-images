# 证据包离线核验指引（wire-provenance-ledger 任务 7.2）

## 这是什么

`evidence_verify.py` 是自包含的证据包核验脚本：离开柏讯平台后，客户凭
**证据包 + 本脚本**即可独立、永久地核验数据变更历史的完整性与未被篡改。
仅依赖 Python 3 标准库（检查点验签可选装 `cryptography`）。

## 证据包从哪来

控制台「我的数据」→ 数据集详情 → 溯源区块 →「下载证据包」，或直接调
`GET /api/v1/provenance/evidence-package`（zip）。解开后的布局：

```
ledger.jsonl                  # 全量信封事件（含锚定回执回写事件）
checkpoints/epoch-<N>.json    # 客户自持检查点（anchor-customer 模式，带平台签名）
anchors/                      # 外部锚定凭据（如 OTS 文件；本脚本不联网核验，供复核）
```

## 怎么核验

```bash
# 基础核验（链连续 + 区间 Merkle + 回执匹配）
python3 evidence_verify.py <解开的包目录>

# 带检查点验签（需要平台公钥，可向平台索取或从任一检查点内 pubKeyPem 提取）
python3 evidence_verify.py <包目录> --pubkey <公钥.pem>
```

结论只有两类：`通过`（退出码 0）或 `发现篡改或不完整`（退出码 1，输出
指出断裂位置——事件 seq / epoch 区间 / 回执根）。公链（OpenTimestamps）
锚定的回执，其外部核验只需 BTC 公开区块数据，无需信任平台。

## 防篡改语义（三层，各拦一类伪装）

1. **链连续**：逐事件重算哈希与 prev_hash 链接——拦"改一条"。
2. **区间构成**：重算每个 epoch 的 Merkle 根与回执比对——拦"整段重写
   还伪造自洽链"。
3. **外部锚定**：回执里的根已被链上/检查点固化——拦"连回执一起重造"，
   这层必须外部凭证（BTC 数据或客户自存检查点）参与才能兑现。

脚本自检：`python3 evidence_verify.py --selftest`（含强伪装用例）。
