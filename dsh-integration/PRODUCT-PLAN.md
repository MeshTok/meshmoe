# meshmoe × DSH 双方向产品方案（2026-11-13，基于当日全链路验证）

## 0. 已验证的技术事实（全部实测，非推测）

| 事实 | 验证方式 |
|---|---|
| DSH CLI 可 headless 跑单任务并退出 | `dsh headless "任务"` exit 0 |
| DSH patch 层可注入自定义 provider | `--patch meshmoe-patch.yml`，llm-pi-ai 的 `providers` 字典 |
| keyless 本地路由可行（免注册） | `apiKeyEnv` 指向 dummy 环境变量，pi-ai 放行 |
| DSH agent 通过本地 NeoHorse-1-4B 完成带工具调用任务 | sandbox 真实创建 hello.txt ✅ |
| DSH agent 系统提示约 6110 token | llama-server 日志（上下文必须 ≥32K） |
| NeoHorse-1-4B 必须关 thinking 接入 DSH | `--reasoning-budget 0`，否则流式解析中断 |
| npm 生态真实存在 | `@deepseek-ai/dsh@0.2.2-rc.1`、插件目录 10581 条目 |

## 1. 两个方向

### 方向一：DSH 官方插件（npm 分发，装 DSH 的存量用户）
- 形态：标准 dsh 插件包（`dsh.bundle.patch` + host 代码），参照 dsh-pet/dsh-context 的 manifest
- `D:/meshmoe/meshmoe-dsh-plugin/` 已建骨架：
  - `cordis.patch.yml` — 注册 meshmoe-local 路由 + 切默认模型（patch 整块替换的局限已注明，v1 改运行时合并）
  - `lib/index.js` — 宿主侧 runtime 管理器（健康检查/自动拉起，mesh 节点注册预留）
- 发布：npm publish + `dsh-plugin` 话题进官方目录（10581 条目的 catalog）
- 目标用户：已装 DSH 的人 → 一装即得本地免费模型 + 成为 mesh 节点

### 方向二：meshmoe Desktop（自有打包，零门槛新用户）
- 布局（已在 `D:/meshmoe/dsh-integration/` 落地）：
  ```
  runtime/
    llama-bin/llama-server.exe   # llama.cpp b12340 CUDA 12.4
    models/NeoHorse-1-4B-Q4_K_M.gguf  # 2.71GB
  meshmoe-patch.yml               # provider 注入层
  run-meshmoe-dsh.cmd             # 启动器: serve | headless | desktop
  install.cmd                     # 安装器原型 (robocopy 部署 + 健康校验)
  ```
- 免注册链路：本地 llama-server 无鉴权 → DSH keyless 路由 → 开箱即用
- 生产化路径：NSIS/MSI 打包（runtime + 便携 DSH + 启动器），装完即"DSH + 本地模型 + mesh 节点"三合一

## 2. 剩余工程项（按优先级）

| 项 | 说明 |
|---|---|
| P0 mesh 节点接入 | edge_node v3：修 tool-calling 透传 + node_token 归属（owner 断链）+ qwen3_5 模型目录，然后由插件 host 代码或独立服务拉起 |
| P1 插件 v1 | patch 改为运行时合并（不覆盖用户已有 provider）；UI 面板（节点状态/积分） |
| P1 打包生产化 | NSIS 安装包 + 无 NVIDIA 卡时的 CPU 档（Q4 CPU 推理未实测，需补测） |
| P2 增量训练 | NeoHorse-1-4B + meshmoe 真实轨迹 → meshmoe-agent-4B，用 eval_all.py 回归 |

## 3. 关键风险

1. **DSH 版本兼容**：插件 manifest 有 `compatibility.dshReleases` 字段，DSH 升级可能改 patch 语义——每次发版前对官方 release 跑回归
2. **patch 整块替换**：会覆盖用户手动配置的其它 provider——v1 必须改为运行时合并
3. **desktop profile 被 Electron 独占**：CLI patch 只影响 headless/自建 profile；DSH 桌面版的 GUI 设置存储路径不同（Local Storage），桌面版内置需走"便携 DSH + 自有 profile"方案
4. **服务端口冲突**：8081 被占用时启动器需退让（选 8082 并同步改 patch baseURL——运行时生成 patch 即可解决）
