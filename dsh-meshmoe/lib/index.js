// ============================================================================
// dsh-meshmoe — host-side runtime manager (v1.0)
//
// 职责:
//   1. 检测本地模型服务 (127.0.0.1:8081) 是否存活; 不存活则拉起 llama-server
//   2. patch 合并: 用户已有 provider 配置时生成合并版 patch (lib/merge.js)
//   3. 荣誉轮询: 每 5 分钟读 edge_node v3 的 /health, 播报焦耳累计
//   4. 装机即入网: 检测 :4002 的 edge_node, 未运行则拉起 (connector 指向 :8081)
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
// 装机即入网: 找到 edge_node.py 并拉起。优先级:
//   1. MESHMOE_NODE_SCRIPT  显式路径
//   2. MESHMOE_HOME/node/edge_node.py   (独立安装包)
//   3. %LOCALAPPDATA%\MeshMoe\node\edge_node.py
//   4. 开发机 D:\meshmoe\node\edge_node.py
// 只装插件、没装节点包的用户: 返回 null, 由调用方给出安装提示(不报错)。
// ---------------------------------------------------------------------------
function findEdgeNodeScript() {
  const explicit = process.env.MESHMOE_NODE_SCRIPT;
  if (explicit && fs.existsSync(explicit)) return explicit;
  const cands = [
    process.env.MESHMOE_HOME && path.join(process.env.MESHMOE_HOME, "node", "edge_node.py"),
    path.join(os.homedir(), "AppData", "Local", "MeshMoe", "node", "edge_node.py"),
    "D:\\meshmoe\\node\\edge_node.py",
  ];
  for (const c of cands) {
    if (c && fs.existsSync(c)) return c;
  }
  return null;
}

function spawnEdgeNode(script) {
  const py = process.env.MESHMOE_PYTHON || "python";
  const env = Object.assign({}, process.env, {
    MESHMOE_CONNECTOR_URL: `http://127.0.0.1:${MESHMOE_PORT}`,
    MESHMOE_MODEL: "MeshMoe-4B-Q4_K_M",
    MESHMOE_PORT: String(EDGE_NODE_PORT),
    MESHMOE_NODE_ID: process.env.MESHMOE_NODE_ID || os.hostname(),
    PYTHONUNBUFFERED: "1",
  });
  const child = spawn(py, ["-u", script], {
    env,
    detached: false,
    stdio: "ignore",
    windowsHide: true,
  });
  child.on("error", () => {});
  return child;
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

    // ---- 4. 装机即入网: 拉起 edge_node (本地模型就绪之后) ----
    ctx.effect(async () => {
      const edge = await fetchJson(EDGE_NODE_PORT, HEALTH_PATH);
      if (edge.ok) {
        log(`mesh node already online on :${EDGE_NODE_PORT}`);
        return;
      }
      const script = findEdgeNodeScript();
      if (!script) {
        log("mesh node package not installed; local model still works. "
          + "Install MeshMoe-Node-Setup.exe or set MESHMOE_NODE_SCRIPT to join the mesh.");
        return;
      }
      // 等本地模型服务就绪(最多 ~30s), 否则节点会退化到内置引擎
      await waitForHealth(RETRY_MAX);
      log(`joining mesh: starting edge node from ${script} ...`);
      const child = spawnEdgeNode(script);
      if (ctx.onDispose) ctx.onDispose(() => { try { child.kill(); } catch {} });
      // 给节点 6s 完成注册, 然后回读健康状态确认
      await new Promise((r) => setTimeout(r, 6000));
      const check = await fetchJson(EDGE_NODE_PORT, HEALTH_PATH);
      log(check.ok && check.data
        ? `mesh node online: auth=${check.data.authenticated ? "yes" : "pending"} joules=${check.data.joules_earned || 0}`
        : "mesh node started but /health not ready yet (see node log)");
    }, "meshmoe: join mesh (auto-start edge node)");
  },
};
