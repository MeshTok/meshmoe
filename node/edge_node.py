#!/usr/bin/env python3
"""
MeshMoE Edge Node Client v3
零配置: 自动检测硬件 → 选择模型 → 下载 → 注册 → 轮询任务 → 推理 → 燃烧焦耳
v3 变更:
  1. tool-calling 全链路透传 (agent 循环必需; 老版 llama-cpp 自动降级为文本)
  2. node_token 节点归属认证 (MESHMOE_NODE_TOKEN, 与 X-Internal-Key 正交)
  3. joule_est 焦耳计量 (J = 功率 × 时间, 按 GPU 档位基准估算)
  4. MODEL_CATALOG 收敛: MeshMoe-4B 全员底座 + Qwen3.8-27B 塔尖
支持NAT穿透: 通过轮询Router获取任务，无需公网IP或端口映射
"""
import json, time, sys, os, socket, threading, signal, platform
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError

# 🔑 加载部署 .env(2026-07-20):MESHMOE_INTERNAL_KEY 用于 verified 注册。
# 之前 edge 进程没读 .env → 注册发空 key → verified 恒 False(L2/L3 白名单失效实锤)。
_env_file = os.getenv("MESHMOE_ENV_FILE", "/opt/meshmoe/.env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                _k, _v = _k.strip(), _v.strip()
                if _k and _k not in os.environ:
                    os.environ[_k] = _v

# ============ 配置 ============
# 🔑 默认连公网 router(nginx /node/ → 4001)。同机部署可设 MESHMOE_ROUTER_URL=http://127.0.0.1:4001
ROUTER_URL = os.getenv("MESHMOE_ROUTER_URL", "https://api.meshmoe.com/node")
MODEL_DIR = os.getenv("MESHMOE_MODEL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))
PORT = int(os.getenv("MESHMOE_PORT", "4002"))
NODE_ID = os.getenv("MESHMOE_NODE_ID", socket.gethostname())
# 🔑 v3 节点归属 token: 用户登录 meshmoe 账号后领取, 绑定 owner_user_id。
# 与 X-Internal-Key (平台级防冒充, verified 标记) 职责正交, 两者都带。
NODE_TOKEN = os.getenv("MESHMOE_NODE_TOKEN", "")
# v3: credits 结算服务(签发/校验 node_token + 焦耳账本)。nginx /credits/ → 4005
CREDITS_URL = os.getenv("MESHMOE_CREDITS_URL", "https://api.meshmoe.com/credits")
# 节点状态(node_id + token)。主路径 ~/.meshmoe/node.json；不可写时依次降级，
# 避免"~/.meshmoe 是坏符号链接/只读盘"这类装机环境导致 token 永久丢失。
def _state_candidates():
    home = os.path.expanduser("~")
    cands = [os.path.join(home, ".meshmoe", "node.json")]
    local = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME")
    if local:
        cands.append(os.path.join(local, "MeshMoe", "node.json"))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".meshmoe-node.json"))
    return cands
NODE_STATE_FILE = _state_candidates()[0]
# 🔑 可达地址:router/其他节点直连本节点推理端点用。默认 127.0.0.1(同机部署)。
# 远程节点(NAT 外/VPS)设成公网 IP 或域名,router 才能直连做流式/探针。
NODE_HOST = os.getenv("MESHMOE_NODE_HOST", "127.0.0.1")
POLL_INTERVAL = int(os.getenv("MESHMOE_POLL_INTERVAL", "5"))  # seconds between polls
N_CTX = int(os.getenv("MESHMOE_N_CTX", "16384"))
# ⭐ MOCK_MODE: 1=不加载真模型(返回固定响应),0=真模型推理
# 用途:验证 gateway→router→edge→计费→分成 控制链路(无 GPU/HF 连不上时)
MOCK_MODE = os.getenv("MOCK_MODE", "0") == "1"
MOCK_MODEL = os.getenv("MOCK_MODEL", "GLM-4-9B-Q4_K_M")   # mock 模式假装跑的模型
MOCK_TIER = os.getenv("MOCK_TIER", "standard")
MOCK_EXPERT = os.getenv("MOCK_EXPERT", "general")

if MOCK_MODE:
    print("=" * 60)
    print("  MOCK MODE ENABLED - no real model loaded")
    print(f"  Pretending: {MOCK_MODEL} ({MOCK_TIER}/{MOCK_EXPERT})")
    print("  Set MOCK_MODE=0 for real inference (requires GPU + model)")
    print("=" * 60)

# ============ 模型库 ============
# v3 收敛为双档（VISION-STRATEGY-2026-11 决策）:
#   全员底座 MeshMoe-4B (NeoHorse-1-4B / Qwen3.5 改训, Apache 2.0) — GPU/CPU 通吃
#   塔尖档 Qwen3.8-27B — 24GB+ 显存
MODEL_CATALOG = {
    "MeshMoe-4B-Q4_K_M": {
        "gguf_file": "NeoHorse-1-4B-Q4_K_M.gguf",   # ModelScope 官方文件名
        "size_mb": 2710,
        "min_ram_gb": 6,
        "gpu_vram_mb": 0,          # 0 = GPU 可选, 纯 CPU 也能跑(约5.5 t/s)
        "tier": "standard",
        "expert_type": "general",
        "description": "MeshMoe-4B 全员底座 - agent 工具调用特化(实测95.8%), 8GB显存全量上GPU",
        "modelscope_org": "TokenRhythm",
        "modelscope_repo": "NeoHorse-1-4B-GGUF",
        "huggingface_repo": "TokenRhythm/NeoHorse-1-4B-GGUF",
    },
    "Qwen3.8-27B-Q4_K_M": {
        "gguf_file": "Qwen3.8-27B-Q4_K_M.gguf",
        "size_mb": 17800,
        "min_ram_gb": 24,
        "gpu_vram_mb": 18000,
        "tier": "heavy",
        "expert_type": "reasoning",
        "description": "Qwen3.8-27B 塔尖档 - 硬任务/长上下文/视觉, 焦耳3倍计量",
        "modelscope_org": "Qwen",
        "modelscope_repo": "Qwen3.8-27B-GGUF",
        "huggingface_repo": "Qwen/Qwen3.8-27B-GGUF",
    },
}


# 词档 -> 数档（服务端 nodes.tier 是 smallint；早期版本客户端传过字符串导致 500）
_TIER_WORDS = {"light": 1, "standard": 2, "medium": 2, "heavy": 3, "ultra": 4}

def tier_int(value, default=1) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if 0 <= value <= 4 else default
    if isinstance(value, str):
        s = value.strip().lower()
        if s.isdigit():
            n = int(s)
            return n if 0 <= n <= 4 else default
        return _TIER_WORDS.get(s, default)
    return default

# ============ 焦耳计量 (v3) ============
# J = 功率(W) × 时间(s)。按 GPU 名称匹配满载功耗基准; 无 GPU 用整机均值。
# 基准表只需量级正确——荣誉计量重趋势与排名，不追求电表级精度。
GPU_POWER_TABLE = [
    ("5090", 575), ("4090", 450), ("3090", 350), ("4080", 320),
    ("3080", 320), ("5070", 250), ("4070", 220), ("3070", 220),
    ("5060", 180), ("3060", 170), ("2080", 215), ("2070", 175),
    ("2060", 160), ("3050", 80), ("2050", 75),
]

def estimate_gpu_watts(gpu_name: str) -> int:
    """从 GPU 名称粗匹配满载功耗(W)。没有 NVIDIA 卡 → 按整机均值 65W。"""
    name = (gpu_name or "").upper()
    for token, watts in GPU_POWER_TABLE:
        if token in name:
            return watts
    return 65

def joules_for(gpu_name: str, latency_ms: int) -> int:
    """一次推理的能量估算(焦耳)。"""
    return max(1, int(estimate_gpu_watts(gpu_name) * latency_ms / 1000.0))


# ============ 硬件检测 ============
def detect_hardware():
    """自动检测硬件配置，返回推荐模型列表"""
    info = {
        "cpu_cores": os.cpu_count() or 4,
        "ram_gb": 0,
        "gpu_name": "",
        "gpu_vram_mb": 0,
        "platform": f"{platform.system()} {platform.machine()}",
    }

    # RAM（跨平台：优先 psutil；无 psutil 时按系统 API 兜底）
    try:
        import psutil
        info["ram_gb"] = psutil.virtual_memory().total // 1073741824
    except Exception:
        try:
            if platform.system() == "Windows":
                import ctypes

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    info["ram_gb"] = stat.ullTotalPhys // 1073741824
            else:
                info["ram_gb"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 1073741824
        except Exception:
            pass

    # GPU
    try:
        import subprocess
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            line = result.stdout.strip().split("\n")[0]
            parts = line.split(",")
            info["gpu_name"] = parts[0].strip()
            vram_str = parts[1].strip().split()[0] if len(parts) > 1 else "0"
            info["gpu_vram_mb"] = int(float(vram_str))
    except:
        pass

    # 推荐模型
    recommended = []
    for name, cfg in MODEL_CATALOG.items():
        if cfg["gpu_vram_mb"] > 0 and info["gpu_vram_mb"] >= cfg["gpu_vram_mb"]:
            recommended.append(name)  # GPU够了
        elif cfg["gpu_vram_mb"] == 0 and info["ram_gb"] >= cfg["min_ram_gb"]:
            recommended.append(name)  # CPU模型, RAM够

    return info, recommended


# ============ 模型下载 ============
def download_model(model_name, model_dir):
    """从ModelScope下载模型，失败则尝试HuggingFace"""
    cfg = MODEL_CATALOG[model_name]
    gguf_file = cfg["gguf_file"]
    model_path = os.path.join(model_dir, gguf_file)

    if os.path.exists(model_path) and os.path.getsize(model_path) > 1000000:
        print(f"[Download] Model already exists: {model_path}")
        return model_path

    os.makedirs(model_dir, exist_ok=True)

    # 各源：ModelScope(优先,国内) → HF-Mirror(国内) → HuggingFace 直连(海外)
    # 注意 ModelScope 默认分支是 master，HF 是 main；分别构造。
    org = cfg["modelscope_org"]
    repo = cfg["modelscope_repo"]
    hf_repo = cfg["huggingface_repo"]
    ms_url = f"https://modelscope.cn/models/{org}/{repo}/resolve/master/{gguf_file}"
    hf_url = f"https://hf-mirror.com/{hf_repo}/resolve/main/{gguf_file}"
    hf_direct = f"https://huggingface.co/{hf_repo}/resolve/main/{gguf_file}"

    tmp_path = model_path + ".tmp"
    MAX_RETRIES = 4  # 每个源对瞬时错误(TLS/断流)重试次数

    def _download(url, label):
        """单源下载：支持 Range 断点续传 + 瞬时错误重试。失败返回 False。"""
        for attempt in range(1, MAX_RETRIES + 1):
            # 已下载的字节数（断点续传）
            have = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
            headers = {"User-Agent": "MeshMoE-Edge/2.0"}
            if have > 0:
                headers["Range"] = f"bytes={have}-"
            try:
                req = Request(url, headers=headers)
                with urlopen(req, timeout=60) as resp:
                    # 200 = 全量；206 = 续传成功；其它(404/403)抛错
                    if resp.status not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status}")
                    total = int(resp.headers.get("Content-Length", 0))
                    if resp.status == 206:
                        total += have  # Content-Length 是剩余部分
                    mode = "ab" if resp.status == 206 else "wb"
                    if resp.status == 200 and have > 0:
                        have = 0  # 服务端不支持续传，重头
                        mode = "wb"
                    downloaded = have
                    with open(tmp_path, mode) as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = downloaded * 100 // total
                                size_mb = downloaded // 1048576
                                sys.stdout.write(f"\r[Download] {label} {pct}% ({size_mb}MB)")
                                sys.stdout.flush()
                print()
                # 完整性校验：若服务端给了 total，检查是否下满
                if total > 0 and downloaded < total:
                    raise RuntimeError(f"incomplete {downloaded}/{total}")
                os.replace(tmp_path, model_path)
                print(f"[Download] Complete via {label}: {model_path}")
                return True
            except Exception as e:
                print(f"\n[Download] {label} attempt {attempt}/{MAX_RETRIES} failed: {e}")
                # 保留 .tmp 以便续传，不删除
                time.sleep(2 * attempt)
        return False

    for label, url in [("ModelScope", ms_url), ("HF-Mirror", hf_url), ("HuggingFace", hf_direct)]:
        print(f"[Download] Trying {label}: {url}")
        if _download(url, label):
            return model_path
        print(f"[Download] {label} exhausted, trying next source...")

    # 清理失败的 .tmp
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    print(f"[Download] All sources failed. Manual download:")
    print(f"  curl -L -C - -o {model_path} {ms_url}")
    print(f"  Or: curl -L -C - -o {model_path} {hf_url}")
    return None


# ============ 本地服务自动发现（兼容性：用户已有的 Ollama/LM Studio/llama-server… 都能当节点） ============
# 任何版本、任何后端，只要会说 OpenAI 协议就被发现 —— 不强制捆绑自己的 llama-cpp-python。
LOCAL_SERVER_PROBES = [
    ("MeshMoe-4B",     "http://127.0.0.1:8081"),     # 客户端自带运行时默认端口
    ("Ollama",         "http://127.0.0.1:11434"),   # Ollama 原生 API，也兼容 /v1
    ("LM-Studio",      "http://127.0.0.1:1234"),
    ("llama-server",   "http://127.0.0.1:8080"),
    ("koboldcpp",      "http://127.0.0.1:5001"),
    ("text-gen-webui", "http://127.0.0.1:5000"),
    ("GPT4All",        "http://127.0.0.1:4891"),
    ("vLLM",           "http://127.0.0.1:8000"),
    ("MeshMoE-edge",   "http://127.0.0.1:4002"),     # 自己的 edge_node 也算一个后端
    ("MeshMoE-desktop","http://127.0.0.1:4199"),
]


def discover_local_servers(timeout=0.8):
    """扫描本机已有的 OpenAI 兼容推理服务，返回 [(name, base_url, [model_ids...])]。
    兼容性优先：不挑版本、不挑后端，能回 /v1/models 或 Ollama /api/tags 即被发现。
    并行探测：串行最坏要 30s+，会让客户端启动像卡死。"""
    from concurrent.futures import ThreadPoolExecutor

    def _probe_base(item):
        name, base = item
        for probe in (f"{base}/v1/models", f"{base}/api/tags"):  # Ollama 用 /api/tags
            try:
                req = Request(probe, headers={"User-Agent": "MeshMoE-Edge/3.0"})
                with urlopen(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        continue
                    data = json.loads(resp.read())
                models = []
                if isinstance(data, dict):
                    for m in (data.get("data") or data.get("models") or []):
                        mid = m.get("id") or m.get("name") or m.get("model")
                        if mid:
                            models.append(mid)
                return (name, base, models)
            except Exception:
                continue
        return None

    found = []
    with ThreadPoolExecutor(max_workers=len(LOCAL_SERVER_PROBES) or 1) as pool:
        for res in pool.map(_probe_base, LOCAL_SERVER_PROBES):
            if res:
                found.append(res)
    # 保持探测表顺序（自带的 MeshMoe-4B 排第一，优先被选用）
    order = {base: i for i, (_, base) in enumerate(LOCAL_SERVER_PROBES)}
    found.sort(key=lambda x: order.get(x[1], 99))
    return found


class ConnectorEdgeNode:
    """连接到已发现的本地 OpenAI 兼容服务（不捆绑自己的 llama-cpp，版本无关）。
    与 EdgeNode 同接口（model_name/model_loaded/infer/tasks_processed/…），可无缝替换。"""

    def __init__(self, backend_name, base_url, model_id, expert_type="general", tier="standard", gpu_watts=65):
        self.model_name = model_id
        self.backend = backend_name
        self.base_url = base_url.rstrip("/")
        self.expert_type = expert_type
        self.tier = tier
        self.gpu_watts = gpu_watts
        self.llm = None
        self.model_loaded = True
        self.tasks_processed = 0
        self.tokens_earned = 0
        self.joules_earned = 0
        self.running = True

    def load_model(self):
        # 后端已由用户自己的服务承载，无需加载；只做一次连通性确认。
        try:
            req = Request(f"{self.base_url}/v1/models", headers={"User-Agent": "MeshMoE-Edge/2.0"})
            with urlopen(req, timeout=5) as resp:
                ok = resp.status == 200
        except Exception:
            ok = False
        if not ok:
            print(f"[Connector] 后端 {self.backend} ({self.base_url}) 不可达")
            self.model_loaded = False
            return False
        print(f"[Connector] 连接 {self.backend} @ {self.base_url}，模型 {self.model_name}（用户自有后端，版本无关）")
        return True

    def infer(self, messages, max_tokens=512, tools=None, tool_choice=None):
        t0 = time.time()
        body = {
            "model": self.model_name,
            "messages": messages,   # v3: connector 直透全部消息结构(tool 角色原样)
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        try:
            req = Request(f"{self.base_url}/v1/chat/completions", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer local",
                                   "User-Agent": "MeshMoE-Edge/3.0"}, method="POST")
            with urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
            elapsed = time.time() - t0
            joules = max(1, int(self.gpu_watts * elapsed))
            msg_out = result.get("choices", [{}])[0].get("message", {})
            self.tasks_processed += 1
            self.tokens_earned += result.get("usage", {}).get("completion_tokens", 0)
            self.joules_earned += joules
            return {
                "choices": [{"message": msg_out,
                             "finish_reason": result.get("choices", [{}])[0].get("finish_reason", "stop")}],
                "model": f"meshmoe-edge/{self.model_name}",
                "usage": result.get("usage", {}),
                "latency_ms": round(elapsed * 1000),
                "joules_est": joules,
            }
        except Exception as e:
            elapsed = time.time() - t0
            return {"error": str(e), "latency_ms": round(elapsed * 1000)}


# ============ 推理引擎 ============
class EdgeNode:
    def __init__(self, model_name, model_path, expert_type="general", gpu_watts=65):
        self.model_name = model_name
        self.model_path = model_path
        self.expert_type = expert_type
        self.gpu_watts = gpu_watts
        self.llm = None
        self.model_loaded = False
        self.tasks_processed = 0
        self.tokens_earned = 0
        self.joules_earned = 0
        self.running = True

    def load_model(self):
        if MOCK_MODE:
            print(f"[EdgeNode] MOCK MODE - skipping real model load")
            print(f"[EdgeNode] Pretending to run {self.model_name}")
            self.model_loaded = True
            return True
        try:
            from llama_cpp import Llama
        except ImportError:
            print("[EdgeNode] ERROR: llama-cpp-python 未安装")
            print("[EdgeNode] Run: pip install llama-cpp-python")
            return False
        except (RuntimeError, OSError) as e:
            # 已装但 DLL 加载失败：常见为 CUDA 版 wheel 缺 CUDA 运行时(cudart64_12.dll 等)
            print(f"[EdgeNode] ERROR: llama-cpp 加载失败: {e!r}")
            print("[EdgeNode] 若用的是 CUDA 版 wheel，需装 CUDA 12 运行时(cudart64_12.dll/cublas)，见 https://developer.nvidia.com/cuda-downloads")
            print("[EdgeNode] 或改装 CPU 版 wheel。")
            return False

        if not os.path.exists(self.model_path):
            print(f"[EdgeNode] ERROR: Model not found: {self.model_path}")
            return False

        print(f"[EdgeNode] Loading {self.model_name}...")
        n_threads = min(os.cpu_count() or 4, 8)

        # GPU offloading: 仅在 nvidia-smi 可用时尝试；CUDA 运行时缺失则回退 CPU
        # (光有驱动不够，GPU 推理还需 CUDA 12 运行时 cudart64_12.dll / cublas 等)
        have_nvidia = False
        try:
            import subprocess
            _r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
            have_nvidia = (_r.returncode == 0)
        except Exception:
            pass

        # 兼容旧版 llama-cpp-python(<0.3.5)：不认识 DeepSeek-R1-distill 的
        # 'deepseek-r1-qwen' pre-tokenizer 类型。其 tokenizer 实为 Qwen2，覆盖回 qwen2 即可加载。
        # 新版 llama-cpp 原生支持后可移除此 kv_overrides。
        kv_overrides = None
        if "DeepSeek-R1-Distill" in self.model_name:
            kv_overrides = {"tokenizer.ggml.pre": "qwen2"}

        def _build(ngl):
            return Llama(
                model_path=self.model_path,
                n_ctx=N_CTX,
                n_threads=n_threads,
                n_gpu_layers=ngl,
                verbose=False,
                kv_overrides=kv_overrides,
            )

        try:
            if have_nvidia:
                print("[EdgeNode] NVIDIA 驱动在，尝试 GPU 卸载(n_gpu_layers=-1)...")
                self.llm = _build(-1)
                print("[EdgeNode] GPU offload OK")
            else:
                self.llm = _build(0)
        except Exception as e:
            if have_nvidia:
                # 多半是缺 CUDA 运行时(cudart64_12.dll/cublas)；回退纯 CPU
                print(f"[EdgeNode] GPU 卸载失败: {e!r}")
                print("[EdgeNode] 回退 CPU 推理(n_gpu_layers=0)。提示:GPU 推理需 CUDA 12 运行时")
                print("[EdgeNode]   (cudart64_12.dll / cublasLt64_12.dll / cublas64_12.dll)，见 https://developer.nvidia.com/cuda-downloads")
                try:
                    self.llm = _build(0)
                    print("[EdgeNode] CPU 回退 OK")
                except Exception as e2:
                    print(f"[EdgeNode] ERROR: 模型加载失败(CPU): {e2!r}")
                    return False
            else:
                print(f"[EdgeNode] ERROR: 模型加载失败: {e!r}")
                return False

        self.model_loaded = True
        print(f"[EdgeNode] Model loaded! Ready to process tasks.")
        return True

    def _norm_messages(self, messages):
        """v3: 规范化 messages 且【保留 tool 协议结构】(role=tool / tool_calls / tool_call_id)。
        agent 循环依赖 tool-calling, 这里不再降级。老版 llama-cpp 若不认这些结构,
        infer() 会捕获异常并走 _degrade_messages 兜底。
        返回 (messages, tools, tool_choice)。"""
        out = []
        for m in messages or []:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("text"))
            if role not in ("system", "user", "assistant", "tool"):
                role = "user"
            entry = {"role": role, "content": content if content is not None else ""}
            if role == "tool" and m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            if m.get("name"):
                entry["name"] = m["name"]
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
            out.append(entry)
        return out

    def _degrade_messages(self, messages):
        """降级: 把 tool 协议平铺为纯文本(老版 llama-cpp / stream+tools 兜底)。"""
        out = []
        for m in messages or []:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            if m.get("tool_calls"):
                calls = ", ".join(
                    (tc.get("function", {}) or {}).get("name", "?")
                    for tc in m["tool_calls"] if isinstance(tc, dict))
                content = (content or "") + f"\n[assistant requested tools: {calls}]"
            if role not in ("system", "user", "assistant"):
                role = "user"
            out.append({"role": role, "content": content})
        return out

    def infer(self, messages, max_tokens=512, tools=None, tool_choice=None):
        if MOCK_MODE:
            t0 = time.time()
            last_user = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    last_user = msg.get("content", "")
                    break
            mock_text = f"[MOCK:{self.model_name}] Echo: {last_user[:80]} | replied at {time.strftime('%H:%M:%S')}"
            mock_tokens = min(max_tokens, max(8, len(mock_text) // 4))
            time.sleep(0.1)
            elapsed = time.time() - t0
            self.tasks_processed += 1
            self.tokens_earned += mock_tokens
            self.joules_earned += max(1, int(self.gpu_watts * elapsed))
            return {
                "choices": [{"message": {"role": "assistant", "content": mock_text}}],
                "model": f"meshmoe-edge/{self.model_name}",
                "usage": {"prompt_tokens": len(last_user) // 4, "completion_tokens": mock_tokens, "total_tokens": (len(last_user) // 4) + mock_tokens},
                "latency_ms": round(elapsed * 1000),
                "joules_est": max(1, int(self.gpu_watts * elapsed)),
            }

        if not self.model_loaded:
            return {"error": "model not loaded"}

        t0 = time.time()
        # v3: 透传 tool 协议; llama-cpp chat template (qwen3_5/qwen2) 原生支持 tools。
        # 老版 llama-cpp 不认 → 捕获后降级为文本重试, 保证永不出错。
        normed = self._norm_messages(messages)
        kwargs = {"messages": normed, "max_tokens": max_tokens, "temperature": 0.7}
        degraded = False
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        try:
            result = self.llm.create_chat_completion(**kwargs)
        except Exception as e_tools:
            if tools:
                # tools 参数不被支持 → 降级重试(去 tools + 平铺消息)
                degraded = True
                kwargs = {"messages": self._degrade_messages(messages),
                          "max_tokens": max_tokens, "temperature": 0.7}
                try:
                    result = self.llm.create_chat_completion(**kwargs)
                except Exception as e:
                    return {"error": str(e), "latency_ms": round((time.time() - t0) * 1000)}
            else:
                # 无 tools 仍失败(如模板不认 tool 角色) → 平铺重试
                try:
                    result = self.llm.create_chat_completion(
                        messages=self._degrade_messages(messages),
                        max_tokens=max_tokens, temperature=0.7)
                except Exception as e2:
                    return {"error": str(e2), "latency_ms": round((time.time() - t0) * 1000)}

        elapsed = time.time() - t0
        msg_out = result["choices"][0]["message"] if result.get("choices") else {}
        text = msg_out.get("content", "") or ""
        tokens = result.get("usage", {}).get("completion_tokens", 0)
        joules = max(1, int(self.gpu_watts * elapsed))

        self.tasks_processed += 1
        self.tokens_earned += tokens
        self.joules_earned += joules

        # 重建标准 OpenAI 响应; 保留 tool_calls(agent 循环必需)与思考链
        out_msg = {"role": "assistant", "content": text}
        if msg_out.get("tool_calls"):
            out_msg["tool_calls"] = msg_out["tool_calls"]
        out_resp = {
            "choices": [{"message": out_msg, "finish_reason": result["choices"][0].get("finish_reason", "stop")}],
            "model": f"meshmoe-edge/{self.model_name}",
            "usage": result.get("usage", {}),
            "latency_ms": round(elapsed * 1000),
            "joules_est": joules,
        }
        if msg_out.get("reasoning_content"):
            out_resp["reasoning_content"] = msg_out["reasoning_content"]
        if degraded:
            out_resp["degraded"] = True   # 提示网关: 本节点 tool 协议不可用, 供路由降权
        return out_resp

    def infer_stream(self, messages, max_tokens=512):
        """真流式推理 generator:逐 token yield。首字延迟 ≈ 首 token 生成时间。
        yield: {"delta": "..."} / {"finish_reason": "stop", "usage": {...}} / {"error": "..."}"""
        if MOCK_MODE:
            last_user = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    last_user = msg.get("content", "")
                    break
            mock_text = f"[MOCK:{self.model_name}] Echo: {last_user[:80]}"
            for i in range(0, len(mock_text), 4):
                yield {"delta": mock_text[i:i+4]}
            self.tasks_processed += 1
            self.tokens_earned += max(8, len(mock_text) // 4)
            yield {"finish_reason": "stop", "usage": {"completion_tokens": max(8, len(mock_text) // 4)}}
            return

        if not self.model_loaded:
            yield {"error": "model not loaded"}
            return

        try:
            stream = self.llm.create_chat_completion(
                messages=self._norm_messages(messages),
                max_tokens=max_tokens, temperature=0.7, stream=True,
            )
        except Exception as e_first:
            # v3: 流式下模板不认 tool 角色等 → 平铺降级重试
            try:
                stream = self.llm.create_chat_completion(
                    messages=self._degrade_messages(messages),
                    max_tokens=max_tokens, temperature=0.7, stream=True,
                )
            except Exception as e:
                yield {"error": str(e)}
                return
        try:
            completion_tokens = 0
            t0 = time.time()
            for chunk in stream:
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta", {}) or {}
                content = delta.get("content")
                finish = choice.get("finish_reason")
                if content:
                    completion_tokens += 1
                    yield {"delta": content}
                if finish:
                    yield {"finish_reason": finish, "usage": {"completion_tokens": completion_tokens}}
            self.tasks_processed += 1
            self.tokens_earned += completion_tokens
            elapsed = time.time() - t0
            joules = max(1, int(self.gpu_watts * elapsed))
            self.joules_earned += joules
            yield {"joules_est": joules}
        except Exception as e:
            yield {"error": str(e)}


# ============ Router通信 ============
def _load_node_state():
    """读取 node.json (node_id + token 持久化)，逐个候选路径尝试"""
    for path in _state_candidates():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return None

def _save_node_state(state):
    """写入 node.json；主路径不可写时自动降级到 LOCALAPPDATA / 程序目录"""
    for path in _state_candidates():
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            return path
        except Exception:
            continue
    return None

def self_register_token(hardware_info, expert_type, tier):
    """v3: 无 token 时向 credits 服务匿名自助注册, 领取 node_token。
    流程: 本地缓存 -> credits /register_node -> 持久化 token。
    匿名入网零门槛; 用户日后在官网 claim_node 绑定账号, 焦耳随 owner 走。"""
    global NODE_TOKEN, NODE_ID
    if NODE_TOKEN:
        return True
    state = _load_node_state()
    if state and state.get("node_token") and state.get("node_id") == NODE_ID:
        NODE_TOKEN = state["node_token"]
        return True
    data = json.dumps({
        "node_id": NODE_ID,
        "gpu_name": hardware_info.get("gpu_name") or "none",
        "expert_type": expert_type,
        "tier": tier_int(tier),
        "protocol": 3,
    }).encode()
    for base in [CREDITS_URL, "http://127.0.0.1:4005"]:
        try:
            req = Request(f"{base.rstrip('/')}/register_node", data=data, headers={
                "Content-Type": "application/json",
                "User-Agent": f"MeshMoE-Edge/3.0 ({NODE_ID})",
                "X-Internal-Key": os.getenv("MESHMOE_INTERNAL_KEY", ""),
            }, method="POST")
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            token = result.get("node_token", "")
            if token:
                NODE_TOKEN = token
                saved = _save_node_state({"node_id": NODE_ID, "node_token": token,
                                          "credits_url": base})
                print(f"[Auth] node_token acquired via {base} (anonymous node)"
                      + (f", saved: {saved}" if saved else " — WARNING: not persisted"))
                return True
        except Exception as e:
            continue
    print("[Auth] token issuance unavailable — running unauthenticated")
    return False

def _auth_headers():
    """v3: 所有 Router 通信统一携带两层凭证:
    - X-Node-Token: 节点归属认证 → credits 记焦耳给正确 owner (用户登录领取)
    - X-Internal-Key: 平台级防冒充 → verified 标记 (部署 .env 注入)"""
    return {
        "X-Node-Token": NODE_TOKEN,
        "Authorization": f"Bearer {NODE_TOKEN}" if NODE_TOKEN else "",
        "X-Internal-Key": os.getenv("MESHMOE_INTERNAL_KEY", ""),
    }

def register_with_router(hardware_info, model_name, expert_type, tier):
    """向Router注册节点"""
    cfg = MODEL_CATALOG.get(model_name, {})
    data = json.dumps({
        "peer_id": NODE_ID,
        "expert_type": expert_type,
        "model_name": model_name,
        "host": NODE_HOST,
        "port": PORT,
        "tier": tier_int(tier),
        "hardware": hardware_info,
        "protocol": 3,          # v3: 声明支持 tool-calling 透传
        "tools_supported": True,
    }).encode()

    url = f"{ROUTER_URL}/api/nodes"  # Nginx proxies /api/nodes to Router
    # Also try direct Router URL
    for register_url in [url, f"{ROUTER_URL}/register"]:
        try:
            req = Request(register_url, data=data, headers={
                "Content-Type": "application/json",
                "User-Agent": f"MeshMoE-Edge/3.0 ({NODE_ID})",
                **{k: v for k, v in _auth_headers().items() if v},
            }, method="POST")
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                print(f"[Router] Registered: {result}")
                return True
        except Exception as e:
            continue

    print("[Router] Registration failed (will retry)")
    return False


def poll_task():
    """从Router轮询任务"""
    url = f"{ROUTER_URL}/task/poll/{NODE_ID}"
    # Also try via Nginx proxy
    for poll_url in [url, f"{ROUTER_URL}/api/task/poll/{NODE_ID}"]:
        try:
            req = Request(poll_url, headers={"User-Agent": f"MeshMoE-Edge/3.0 ({NODE_ID})",
                                             **{k: v for k, v in _auth_headers().items() if v}})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except:
            continue
    return None


def submit_result(task_id, result, latency_ms, success=True):
    """向Router提交推理结果 (v3: payload 携带 joules_est 供荣誉记账)"""
    data = json.dumps({
        "task_id": task_id,
        "peer_id": NODE_ID,
        "result": result,
        "success": success,
        "latency_ms": latency_ms,
        "joules_est": result.get("joules_est", 0) if isinstance(result, dict) else 0,
    }).encode()

    for url in [f"{ROUTER_URL}/task/result", f"{ROUTER_URL}/api/task/result"]:
        try:
            req = Request(url, data=data, headers={
                "Content-Type": "application/json",
                "User-Agent": f"MeshMoE-Edge/3.0 ({NODE_ID})",
                **{k: v for k, v in _auth_headers().items() if v},
            }, method="POST")
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except:
            continue
    return None


# 🔑 流式任务回传上下文(stream_push_task 读用,由 worker_loop 设置)
_current_stream_messages = []
_current_stream_max_tokens = 512


def stream_push_task(task_id):
    """流式任务回传(NAT 中继):用 chunked POST + NDJSON 逐 token 推送给 Router。
    节点出站长连接,符合 NAT 穿透。"""
    import http.client
    from urllib.parse import urlparse
    messages = _current_stream_messages
    max_tokens = _current_stream_max_tokens
    parsed = urlparse(ROUTER_URL)
    host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        conn = http.client.HTTPSConnection(host, port, timeout=300) if parsed.scheme == "https" else http.client.HTTPConnection(host, port, timeout=300)
        conn.putrequest("POST", "/task/stream_push")
        conn.putheader("Content-Type", "application/x-ndjson")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.putheader("User-Agent", f"MeshMoE-Edge/2.0 ({NODE_ID})")
        conn.endheaders()
        finish_sent = False
        for ev in edge.infer_stream(messages, max_tokens):
            line = None
            if "error" in ev:
                line = json.dumps({"task_id": task_id, "type": "error", "error": ev["error"]}, ensure_ascii=False)
                finish_sent = True
            elif "delta" in ev and ev["delta"]:
                line = json.dumps({"task_id": task_id, "type": "delta", "token": ev["delta"]}, ensure_ascii=False)
            elif "joules_est" in ev:
                line = json.dumps({"task_id": task_id, "type": "joule", "joules": ev["joules_est"]}, ensure_ascii=False)
            elif ev.get("finish_reason"):
                line = json.dumps({"task_id": task_id, "type": "finish",
                                   "finish_reason": ev["finish_reason"],
                                   "usage": ev.get("usage", {})}, ensure_ascii=False)
                finish_sent = True
            if line:
                payload = (line + "\n").encode("utf-8")
                conn.send(b"%x\r\n%s\r\n" % (len(payload), payload))
        if not finish_sent:
            payload = b'{"task_id":"' + task_id.encode() + b'","type":"finish","finish_reason":"stop"}\n'
            conn.send(b"%x\r\n%s\r\n" % (len(payload), payload))
        conn.send(b"0\r\n\r\n")
        conn.getresponse().read()
        conn.close()
    except Exception as e:
        print(f"[Worker] stream_push failed: {e}")


def _process_stream_task(task_id, messages, max_tokens):
    """处理流式任务:设全局上下文 + 调 stream_push_task 回传。"""
    global _current_stream_messages, _current_stream_max_tokens
    _current_stream_messages = messages
    _current_stream_max_tokens = max_tokens
    t0 = time.time()
    stream_push_task(task_id)
    elapsed = time.time() - t0
    print(f"[Worker] Stream task {task_id} relayed ({round(elapsed*1000)}ms)")


# ============ 本地健康检查 ============
class HealthHandler(BaseHTTPRequestHandler):
    """本地 :PORT 端点 — /health 查看状态;POST /v1/chat/completions 边缘推理(E2-1 支持流式)"""
    def do_GET(self):
        if self.path == "/health":
            data = {
                "status": "ok",
                "version": "3.0",
                "node_id": NODE_ID,
                "model": edge.model_name if edge else "not loaded",
                "model_loaded": edge.model_loaded if edge else False,
                "tools_supported": True,
                "tasks_processed": edge.tasks_processed if edge else 0,
                "tokens_earned": edge.tokens_earned if edge else 0,
                "joules_earned": edge.joules_earned if edge else 0,
                "joules_unit": "J",
                "authenticated": bool(NODE_TOKEN),
                "uptime": round(time.time() - start_time),
            }
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/v1/models":
            # OpenAI 兼容：暴露已加载模型，便于桌面端/其它节点自动发现本节点
            data = {
                "object": "list",
                "data": [{"id": edge.model_name if edge else "none",
                          "object": "model", "owned_by": "meshmoe-edge"}],
            }
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """E2-1:边缘推理端点(router 本地/远程直连)。stream=true → 真流式 SSE;false → JSON。"""
        if self.path != "/v1/chat/completions":
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_response(400); self.end_headers(); return
        messages = body.get("messages", [])
        max_tokens = body.get("max_tokens", 512)
        stream = body.get("stream", False)
        tools = body.get("tools")          # v3: tool-calling 透传
        tool_choice = body.get("tool_choice")

        if not stream:
            result = (edge.infer(messages, max_tokens, tools=tools, tool_choice=tool_choice)
                      if edge else {"error": "model not loaded"})
            data = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # ── 真流式 SSE:消费 infer_stream() 逐 token 推送 ──
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send_chunk(delta=None, finish_reason=None, usage=None):
            frame = {"id": "edge-chat", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": delta or {}}]}
            if finish_reason:
                frame["choices"][0]["finish_reason"] = finish_reason
            elif usage is not None:
                frame["choices"] = []
                frame["usage"] = usage
            self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            for ev in edge.infer_stream(messages, max_tokens):
                if "error" in ev:
                    send_chunk(delta={"content": f"[edge error] {ev['error']}"})
                    break
                if "delta" in ev and ev["delta"]:
                    send_chunk(delta={"content": ev["delta"]})
                elif ev.get("finish_reason"):
                    send_chunk(finish_reason=ev["finish_reason"])
                    if ev.get("usage"):
                        send_chunk(usage=ev["usage"])
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception as e:
            err = {"id": "edge-chat", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"content": f"[edge error] {e}"}}]}
            try:
                self.wfile.write(f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def log_message(self, *args): pass


# ============ 主循环 ============
edge = None
start_time = time.time()

def worker_loop():
    """主工作循环: 轮询任务 → 推理 → 回报"""
    global edge

    # 注册
    hw_info, _ = detect_hardware()
    cfg = MODEL_CATALOG.get(edge.model_name, {})
    self_register_token(hw_info, cfg.get("expert_type", "general"), cfg.get("tier", "light"))
    register_with_router(hw_info, edge.model_name, cfg.get("expert_type", "general"), cfg.get("tier", "light"))

    # 定时重新注册 (每5分钟)
    last_register = time.time()

    while edge.running:
        # 重新注册
        if time.time() - last_register > 300:
            register_with_router(hw_info, edge.model_name, cfg.get("expert_type", "general"), cfg.get("tier", "light"))
            last_register = time.time()

        # 轮询任务
        try:
            task = poll_task()
        except Exception as e:
            print(f"[Worker] Poll error: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        if not task or task.get("status") == "idle":
            time.sleep(POLL_INTERVAL)
            continue

        # 有任务! 开始推理
        task_id = task.get("task_id", "")
        messages = task.get("messages", [])
        max_tokens = task.get("max_tokens", 512)
        is_stream = task.get("stream", False)

        print(f"[Worker] Task {task_id}{' [STREAM]' if is_stream else ''}: {messages[-1].get('content', '')[:60]}...")

        if is_stream:
            # 🔑 流式任务:NAT 中继回传,边算边经 /task/stream_push 逐 token 推给 Router
            _process_stream_task(task_id, messages, max_tokens)
        else:
            # 非流式任务:整段推理后提交结果
            result = edge.infer(messages, max_tokens)
            latency = result.get("latency_ms", 0)
            success = "error" not in result
            submit_result(task_id, result, latency, success)
            if success:
                print(f"[Worker] Task {task_id} done ({latency}ms, {result.get('usage',{}).get('completion_tokens',0)} tokens)")
            else:
                print(f"[Worker] Task {task_id} failed: {result.get('error', 'unknown')}")

        # 短暂休息避免过热
        time.sleep(1)


def main():
    global edge, start_time
    start_time = time.time()

    print("=" * 50)
    print("  MeshMoE Edge Node v3")
    print("  Share compute, burn Joules, earn honor")
    print("=" * 50)

    # 1. 检测硬件
    hw_info, recommended = detect_hardware()
    print(f"\n[Hardware] CPU: {hw_info['cpu_cores']} cores")
    print(f"[Hardware] RAM: {hw_info['ram_gb']}GB")
    if hw_info['gpu_name']:
        print(f"[Hardware] GPU: {hw_info['gpu_name']} ({hw_info['gpu_vram_mb']}MB VRAM)")
    else:
        print("[Hardware] GPU: None (CPU-only mode)")

    # 2. 兼容性优先：先扫描用户已有的本地推理服务（Ollama/LM Studio/llama-server…）
    #    任何版本/后端只要说 OpenAI 协议就连 —— 不强制捆绑自己的 llama-cpp-python。
    #    设 MESHMOE_CONNECTOR=0 可禁用，回退纯内置加载。
    edge = None
    cfg = {}
    use_connector = False
    if not MOCK_MODE and os.getenv("MESHMOE_CONNECTOR", "1") != "0":
        servers = discover_local_servers()
        # 显式指定 MESHMOE_CONNECTOR_URL 时强制纳入（即使不在探测列表、或探测超时漏掉）
        want = (os.getenv("MESHMOE_CONNECTOR_URL") or "").strip().rstrip("/")
        if want and not any(bu.rstrip("/") == want for _, bu, _ in servers):
            try:
                req = Request(f"{want}/v1/models", headers={"User-Agent": "MeshMoE-Edge/3.0"})
                with urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                ms = []
                for m in (data.get("data") or data.get("models") or []):
                    mid = m.get("id") or m.get("name") or m.get("model")
                    if mid:
                        ms.append(mid)
                servers.insert(0, ("custom", want, ms))
            except Exception:
                pass
        if servers:
            print(f"\n[Connector] 发现 {len(servers)} 个本地推理服务：")
            for nm, bu, ms in servers:
                print(f"  - {nm} @ {bu}  模型: {', '.join(ms[:5]) if ms else '(未知)'}")
            want = (os.getenv("MESHMOE_CONNECTOR_URL") or "").strip().rstrip("/")
            picked = None
            for nm, bu, ms in servers:
                if want and bu.rstrip("/") == want:
                    picked = (nm, bu, ms)
                    break
            if not picked:
                picked = servers[0]
            nm, bu, ms = picked
            model_id = (os.getenv("MESHMOE_MODEL") or "").strip() or (ms[0] if ms else "local")
            edge = ConnectorEdgeNode(nm, bu, model_id, expert_type="general", tier="standard",
                                     gpu_watts=estimate_gpu_watts(hw_info.get("gpu_name", "")))
            cfg = {"tier": "standard", "expert_type": "general",
                   "description": f"connector→{nm}", "size_mb": 0}
            use_connector = True
            print(f"[Connector] 选用 {nm} @ {bu}，模型 {model_id}")
        else:
            print("\n[Connector] 未发现已有本地推理服务，回退内置 llama-cpp 加载")

    # 3. 内置加载路径（仅当未用 connector）
    if not use_connector:
        chosen = (os.getenv("MESHMOE_MODEL") or "").strip() or None

        if MOCK_MODE:
            chosen = MOCK_MODEL
            print(f"\n[Model] MOCK MODE - using: {chosen} (no real download/load)")
        elif not chosen:
            if recommended:
                chosen = recommended[0]
                print(f"\n[Model] Auto-selected: {chosen}")
                print(f"  {MODEL_CATALOG[chosen]['description']}")
            else:
                chosen = "MeshMoe-4B-Q4_K_M"
                print(f"\n[Model] Defaulting to base model: {chosen}")

        if MOCK_MODE:
            cfg = {"tier": MOCK_TIER, "expert_type": MOCK_EXPERT, "description": "MOCK - no real model", "size_mb": 0}
            model_path = "/tmp/mock_model.gguf"
        else:
            if chosen not in MODEL_CATALOG:
                print(f"[Model] Unknown model: {chosen}")
                print(f"[Model] Available: {', '.join(MODEL_CATALOG.keys())}")
                sys.exit(1)

            cfg = MODEL_CATALOG[chosen]
            print(f"[Model] {chosen} ({cfg['description']})")
            print(f"[Model] Tier: {cfg['tier']}, Expert: {cfg['expert_type']}, Size: ~{cfg['size_mb']}MB")

            # 下载模型
            model_path = download_model(chosen, MODEL_DIR)
            if not model_path:
                sys.exit(1)
        edge = EdgeNode(chosen, model_path, cfg.get("expert_type", "general"),
                        gpu_watts=estimate_gpu_watts(hw_info.get("gpu_name", "")))

    # 4. 加载（connector 模式下 load_model 只做连通性确认）
    if not edge.load_model():
        sys.exit(1)

    # 5. 启动本地健康检查服务
    try:
        from socketserver import ThreadingMixIn
        class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True
        health_server = ThreadedHTTPServer(("127.0.0.1", PORT), HealthHandler)
        health_server.allow_reuse_address = True
        threading.Thread(target=health_server.serve_forever, daemon=True).start()
        print(f"[Health] Local status at http://127.0.0.1:{PORT}/health")
    except Exception as e:
        print(f"[Health] Could not start health server: {e}")

    # 6. 启动工作循环
    print(f"\n[Worker] Starting... Polling {ROUTER_URL} for tasks")
    print(f"[Worker] Node ID: {NODE_ID}")

    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()

    # 7. 主线程等待退出信号
    def shutdown(sig, frame):
        print(f"\n[MeshMoE] Shutting down...")
        edge.running = False
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while edge.running:
            time.sleep(1)
            # 打印状态
            if edge.tasks_processed > 0 and int(time.time()) % 60 == 0:
                uptime_min = int((time.time() - start_time) / 60)
                print(f"[Status] Uptime: {uptime_min}min | Tasks: {edge.tasks_processed} | Tokens: {edge.tokens_earned} | Joules: {edge.joules_earned}")
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
