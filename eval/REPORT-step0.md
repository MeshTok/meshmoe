# NeoHorse-1-4B Step 0 实测报告

日期: 2026-11-13 | 机器: RTX 3050 Laptop 8GB / Driver 552.22 | 引擎: llama.cpp b12340 (CUDA 12.4, 官方预编译)

## 一、结论速览

| 问题 | 实测答案 |
|---|---|
| NeoHorse-1-4B 是否真实可用？ | ✅ ModelScope 直连下载成功，llama.cpp 识别为 `qwen35 4B`，开箱即跑 |
| Q4 量化是否毁掉 agent 微调？ | **没有**。Q4_K_M 与 Q5_K_M 函数调用准确率完全相同 (95.8%)，agent 任务 Q4 反而略优 |
| 装机门槛 | Q4_K_M 2.71GB / Q5_K_M 3.07GB，8GB 显卡全量上 GPU，45 t/s 实时交互无忧 |
| 需要修什么 | 模型自身无硬伤；失败集中在"心算替代调工具"和 1 个边界负例 |

## 二、量化对比（24 例函数调用 + 6 例 agent 任务，temperature=0）

| 指标 | Q4_K_M (2.71GB) | Q5_K_M (3.07GB) |
|---|---|---|
| 套件A 函数调用准确率 | **95.8%** (23/24) | **95.8%** (23/24) |
| ─ 正例 (14) | 14/14 | 14/14 |
| ─ 负例"不该调工具" (8) | 7/8 | 7/8 |
| 套件B agent 任务 | **66.7%** (4/6) | 50.0% (3/6) |
| 生成速度 tg128 | 45.2 t/s | 42.4 t/s |
| 预填速度 pp512 | 1715 t/s | 665 t/s |

**关键发现：Q4 与 Q5 差距在评测噪声以内。** Aroxa 的"Q4 丢 78% 微调"警告是其自身 LoRA 幅度极小 (ρ=0.013) 的特例；NeoHorse 的后训练对 Q4 量化鲁棒。**meshmoe 分发默认档可以直接用 Q4_K_M**——装机门槛最低，质量无实测损失。

## 三、失败模式分析（增量训练的靶子）

1. **A15（两个量化档同败）**: "如果我明天去北京出差，一般需要注意什么？" → 误调 get_weather。属边界负例（查天气也算合理辅助），真实 BFCL irrelevance 判分下算错。增量数据里要补"请求未明确要数据时不主动调工具"。
2. **B2/B5（核心失败模式）**: 涉及简单算术换算时模型**心算而不调工具**（1200 USD→CNY 直接给答案，不调 convert_currency；查库后自己乘汇率）。→ 对 meshmoe 的启示：**网关侧对确定性工具可下发 `tool_choice: required/指定函数`**，不完全依赖模型自觉；增量训练用"该调必须调"的可验证奖励修这个习惯。
3. **B4 不稳定**: "请用计算器算温差" Q5 没调工具、Q4 调了——说明指令跟随有随机性（同 seed 不同量化），agent 场景需要在 system prompt 里强化"数值计算一律用工具"。

## 四、对 meshmoe 工程栈的直接结论

1. **edge_node 必须换推理引擎**: 现有 llama-cpp-python 0.3.34 不支持 qwen3_5 架构。推荐改为 **llama-server.exe sidecar 进程**（本次验证的 b12340 版）：OpenAI 兼容 API + `--jinja` 原生 tool-calling 全部开箱即用，edge_node 只做中继。
2. **reasoning_content 需处理**: 模型输出带独立思维链字段（`reasoning_content`），边缘中继要决定透传给 DSH 还是剥离（影响 token 计费与延迟体感）。
3. **模型卡口径**: Q4_K_M 实测 2.71GB（仓库标注 2.52GB 是 GiB/GB 口径差）。**纯 CPU 路径已补测（2026-11-15）**：`llama-server -ngl 0 -c 4096 --jinja --reasoning-budget 0`，模型 2.6s 加载完成，短回复约 **3.9 tok/s**，tool-calling 正确返回 `list_files`（延迟 5.6s）。结论：无 N 卡可用，`MESHMOE_NO_GPU=1` 走 `-ngl 0` 是可用的 CPU 档，只是慢一个数量级，适合轻量/夜间任务。

## 五、产物清单

- `D:/meshmoe/eval/models/NeoHorse-1-4B-Q4_K_M.gguf` / `Q5_K_M.gguf` — 模型
- `D:/meshmoe/eval/tools/eval_all.py` — 评测脚本（套件A/B，可复用于 meshmoe-agent-4B 每次迭代的回归基线）
- `D:/meshmoe/eval/tools/llama-bin/` — llama.cpp b12340 CUDA 12.4 运行时
- `D:/meshmoe/eval/results/Q4_K_M.json` / `Q5_K_M.json` — 逐例结果

## 六、下一步

1. **DSH 真机接入**: llama-server 已是 OpenAI 兼容端点 (127.0.0.1:8081/v1)，可直接在 DSH 自定义模型里填入跑真实 agent 会话（套件B 的 mock 工具换成 DSH 真实工具集）
2. **增量训练起点确认**: 以 NeoHorse-1-4B 为底座 + meshmoe 真实轨迹 → 产出 meshmoe-agent-4B，每次迭代用本评测集回归
3. **补测**: ~~CPU-only 速度~~ ✅ 已补测（见上）；长上下文 (262K) 实际可用深度、Q8_0 对照仍待做（阶段二）。

---

## 附录：NeoHorse-1-9B 中坚档实测（2026-11-13）

| 指标 | MeshMind-4B (Q4_K_M, 2.71GB) | NeoHorse-1-9B (Q4_K_M, 5.63GB) |
|---|---|---|
| 函数调用准确率（24 例） | **95.8%** | 91.7% |
| agent 多步任务（6 例） | **66.7%** | 66.7% |
| 生成速度（RTX 3050） | **45.2 t/s** | 29.7 t/s |
| 权重体积 | 2.71 GiB | 5.23 GiB（8GB 显存可全量跑） |

**结论：在工具调用维度，9B 没有跑赢 4B。** 失败模式：A09 并行调用只发了一个、A14 误调 calculate、B2/B5 心算替代换算工具（与 4B 同款病灶）。样本量小（24+6，temp=0），1-2 例差异在噪声内，但至少可以确认 **9B 在 agent 工具调用上没有优势**。

**中坚档定位修正**：NeoHorse-1-9B（= Qwen3.5-9B + NeoHorse agent 后训练）的价值不在 tool-calling，而在**知识密度和复杂推理**（长文档、多语言、难题）。路由策略建议：
- 简单/中等 agent 任务 → MeshMind-4B 节点（更快、质量不输）
- 知识密集/长上下文/复杂推理 → 9B 节点
- 是否上架 9B 中坚档，建议先用一批真实 meshmoe 任务（而非本玩具评测）验证 9B 的增量价值再定
