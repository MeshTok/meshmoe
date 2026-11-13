// ============================================================================
// dsh-meshmoe — host-side runtime manager (v1.0)
//
// 职责:
//   1. 检测本地模型服务 (127.0.0.1:8081) 是否存活; 不存活则拉起 llama-server
//   2. patch 合并: 用户已有 provider 配置时生成合并版 patch (lib/merge.js)
//   3. 荣誉轮询: 每 5 分钟读 edge_node v3 的 /health, 播报焦耳累计
//   4. (预留) mesh 节点注册 — edge_node v3 就绪后在此接入
//
// Cordis 插件形态: name + inject + apply(ctx)。
// 注: 首次加载如遇 DSH 宿主 API 差异, 以 dsh-pet (npm) 为参照修正签名。
// ============================================================================

const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { mergePatch } = require("./merge");

const MESHMOE_PORT = 8081;
const EDGE_NODE_PORT = 4002;          // edge_node v3 (含焦耳计量)
const HEALTH_PATH = "/health";
const RETRY_MAX = 15;
const RETRY_INTERVAL_MS = 2000;
const HONOR_POLL_MS = 5 * 60 * 1000;  // 荣誉播报间隔

function findRuntimeRoot() {
  if (process.env.MESHMOE_RUNTIME_ROOT && fs.existsSync(process.env.MESHMOE_RUNTIME_ROOT)) {
    return process.env.MESHMOE_RUNTIME_ROOT;
  }
  const candidates = [
    path.join(process.env.PROGRAMFILES || "C:\\Program Files", "MeshMoe", "runtime"),
    path.join(process.env.LOCALAPPDATA || "", "MeshMoe", "runtime"),
    "D:\\meshmoe\\dsh-integration\\runtime", // 开发机布局
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

function fetchJson(port, p, timeoutMs = 2000) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port, path: p, timeout: timeoutMs },
      (res) => {
        let buf = "";
        res.on("data", (d) => (buf += d));
        res.on("end", () => {
          try { resolve({ ok: res.statusCode === 200, data: JSON.parse(buf) }); }
          catch { resolve({ ok: res.statusCode === 200, data: null }); }
        });
      }
    );
    req.on("error", () => resolve({ ok: false, data: null }));
    req.on("timeout", () => { req.destroy(); resolve({ ok: false, data: null }); });
  });
}

async function checkHealth() {
  const r = await fetchJson(MESHMOE_PORT, HEALTH_PATH);
  return r.ok;
}

async function waitForHealth(maxTries) {
  for (let i = 0; i < maxTries; i++) {
    if (await checkHealth()) return true;
    await new Promise((r) => setTimeout(r, RETRY_INTERVAL_MS));
  }
  return false;
}

function startLlamaServer(runtimeRoot) {
  const exe = path.join(runtimeRoot, "llama-bin", "llama-server.exe");
  const model = path.join(runtimeRoot, "models", "MeshMoe-4B-Q4_K_M.gguf");
  if (!fs.existsSync(exe) || !fs.existsSync(model)) {
    return { started: false, error: `runtime incomplete: exe=${fs.existsSync(exe)} model=${fs.existsSync(model)}` };
  }
  const child = spawn(
    exe,
    ["-m", model, "--port", String(MESHMOE_PORT), "-ngl", "99", "-c", "32768", "--reasoning-budget", "0", "--jinja", "--host", "127.0.0.1"],
    { detached: false, stdio: "ignore", windowsHide: true }
  );
  child.on("error", () => {});
  return { started: true, child };
}

// ---------------------------------------------------------------------------
// Cordis plugin definition
// ---------------------------------------------------------------------------
module.exports = {
  name: "dsh-meshmoe",
  inject: [],

  apply(ctx) {
    const log = (msg) => {
      try {
        ctx.logger?.info?.(`[meshmoe] ${msg}`);
      } catch {
        console.log(`[meshmoe] ${msg}`);
      }
    };

    // ---- 1. patch 合并: 保留用户已有 provider ----
    ctx.effect(() => {
      try {
        const userPatch = path.join(os.homedir(), ".dsh", "profiles", "desktop", "cordis.patch.yml");
        const ourPatch = path.join(__dirname, "..", "cordis.patch.yml");
        const root = findRuntimeRoot();
        const out = root
          ? path.join(root, "meshmoe-merged.patch.yml")
          : path.join(os.tmpdir(), "meshmoe-merged.patch.yml");
        const merged = mergePatch(userPatch, ourPatch, out);
        if (merged) {
          log(`patch merged (user providers preserved) -> ${merged}`);
        } else {
          log("patch merge skipped (no user patch or parse failed), using built-in patch");
        }
      } catch (e) {
        log(`patch merge error (non-fatal): ${e.message}`);
      }
    }, "meshmoe: merge patch with user config");

    // ---- 2. 确保本地模型服务 ----
    ctx.effect(async () => {
      const ok = await checkHealth();
      if (ok) {
        log(`local model server already running on :${MESHMOE_PORT}`);
        return;
      }
      const root = findRuntimeRoot();
      if (!root) {
        log("runtime not found; install meshmoe Desktop or set MESHMOE_RUNTIME_ROOT");
        return;
      }
      log(`starting local model server from ${root} ...`);
      const r = startLlamaServer(root);
      if (!r.started) {
        log(`failed to start: ${r.error}`);
        return;
      }
      const healthy = await waitForHealth(RETRY_MAX);
      log(healthy ? `local model ready on :${MESHMOE_PORT}` : "model server did not become healthy in time");
    }, "meshmoe: ensure local model server");

    // ---- 3. 荣誉轮询: 焦耳播报 (edge_node v3) ----
    ctx.effect(async () => {
      const timer = setInterval(async () => {
        // 优先读 edge_node v3 (有焦耳计量), 回退读裸模型服务
        const edge = await fetchJson(EDGE_NODE_PORT, HEALTH_PATH);
        if (edge.ok && edge.data) {
          const d = edge.data;
          log(`honor: ${d.joules_earned || 0} J burned | ${d.tasks_processed || 0} tasks | auth=${d.authenticated ? "yes" : "no token"}`);
          return;
        }
        const bare = await fetchJson(MESHMOE_PORT, HEALTH_PATH);
        if (bare.ok && bare.data && bare.data.joules_earned != null) {
          log(`honor: ${bare.data.joules_earned} J burned | ${bare.data.tasks_processed || 0} tasks`);
        }
      }, HONOR_POLL_MS);
      // Cordis dispose 由宿主管理; 兜底防止泄漏
      if (ctx.onDispose) ctx.onDispose(() => clearInterval(timer));
      return () => clearInterval(timer);
    }, "meshmoe: honor poller");

    // ---- (预留) mesh 节点注册 ----
    // edge_node v3 已含 node_token + joules_est 字段; 在此启动独立 edge_node
    // 进程 (python edge_node.py) 即完成"装机即入网"。等待 credentials 服务
    // 提供 node_token 的领取通道后启用。
  },
};
