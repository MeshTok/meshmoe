// ============================================================================
// dsh-meshmoe merge.js — patch 合并器 (v1.0)
//
// 问题: DSH 的 patch entry 是整块替换 —— 如果用户已在 GUI 配了其它 provider,
// 我们的 cordis.patch.yml 会把它们全部覆盖。
// 方案: 读用户现有 patch, 把 MeshMoe provider 合并进 llm-pi-ai 的 providers
// 字典(其余 provider 原样保留), 输出合并后的 patch 文件。
// agent-default-model 仅在用户未自定义默认模型时才接管。
// ============================================================================

const fs = require("node:fs");
const path = require("node:path");
const YAML = require("yaml");

const LLM_PLUGIN_NAME = "@deepseek-ai/dsh-llm-pi-ai";
const DEFAULT_MODEL_PLUGIN_NAME = "@deepseek-ai/dsh-agent-default-model";

/**
 * 合并 patch。userPatchPath 可不存在(全新用户)。
 * @returns {string|null} 合并输出文件路径; 失败返回 null(调用方回退用我们自己的 patch)
 */
function mergePatch(userPatchPath, ourPatchPath, outPath) {
  let ours;
  try {
    ours = YAML.parse(fs.readFileSync(ourPatchPath, "utf8"));
  } catch (e) {
    return null;
  }
  if (!Array.isArray(ours)) return null;

  let merged = ours;
  let user = null;
  try {
    if (fs.existsSync(userPatchPath)) {
      user = YAML.parse(fs.readFileSync(userPatchPath, "utf8"));
    }
  } catch (e) {
    user = null; // 用户文件损坏 → 不合并, 保底用我们自己的
  }

  if (Array.isArray(user)) {
    const ourLlm = ours.find((e) => e && e.name === LLM_PLUGIN_NAME);
    const userLlm = user.find((e) => e && e.name === LLM_PLUGIN_NAME);
    const ourProviders = ourLlm?.config?.providers || {};

    if (userLlm) {
      // 合并 providers 字典: 用户已有的保留, MeshMoe 条目以我们为准(同名则覆盖)
      const userProviders = (userLlm.config && userLlm.config.providers) || {};
      userLlm.config = userLlm.config || {};
      userLlm.config.providers = Object.assign({}, userProviders, ourProviders);
      merged = user; // 以用户配置为主体
      // 从 merged 里拿掉我们的 llm 条目(已并入用户条目)
      merged = merged.filter((e) => !(e && e.name === LLM_PLUGIN_NAME && e !== userLlm));
    } else {
      // 用户 patch 里没有 llm 条目 → 追加我们的
      merged = user.concat(ours);
    }

    // agent-default-model: 用户已有自定义默认模型则不接管
    const userDefault = user.find((e) => e && e.name === DEFAULT_MODEL_PLUGIN_NAME);
    if (userDefault) {
      merged = merged.filter((e) => !(e && e.name === DEFAULT_MODEL_PLUGIN_NAME && e !== userDefault));
    }
  }

  try {
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, YAML.stringify(merged), "utf8");
    return outPath;
  } catch (e) {
    return null;
  }
}

module.exports = { mergePatch };
