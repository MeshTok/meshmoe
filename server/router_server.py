"""
MeshMoE 智能路由器 v2
双层门控: 语义分类 + 节点调度 (信誉+负载+延迟)
端口: 4001
"""
import json, time, os, sys, threading, subprocess, re, hashlib, hmac
from collections import defaultdict
from urllib.request import Request, urlopen
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# Load .env if available
_env_file = "/www/wwwroot/meshmoe.com/meshtok/.env"
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v

sys.path.insert(0, os.path.dirname(__file__))
from semantic import get_semantic_router
try:
    from context_compressor import compress_context
    from capability_router import get_channel
    COMPRESS_OK = True
except Exception as _e:
    COMPRESS_OK = False
    print('[Router] context_compressor/capability_router load FAILED:', _e)
try:
    from capability_router import classify_capability, classify_capability_qwen, select_model, select_moa_proposers, select_aggregator, _record_call, estimate_difficulty
    from feedback_flywheel import log_dispatch
    CAP_ROUTER_OK = True
    print('[Router] capability_router loaded OK')
except Exception as _e:
    CAP_ROUTER_OK = False
    print('[Router] capability_router load FAILED:', _e)

LITELLM_URL = "http://127.0.0.1:4000/v1/chat/completions"
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")
EDGE_URL = "http://127.0.0.1:4002/v1/chat/completions"
BOOTSTRAP_URL = os.getenv("MESHMOE_BOOTSTRAP_URL", "http://127.0.0.1:4003")

# ========== Node Scheduler ==========
def get_region_by_ip(ip):
    """从IP粗略推断region(asia/europe/americas)。生产可换geoip2库精确判断。
    简单规则: 国内IP段→asia, 否则按私有/未知归other。"""
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return "local"
    # 中国大陆常见段(粗略, 精确需geoip)
    parts = ip.split(".")
    try:
        first = int(parts[0])
        if first == 113 or first == 114 or first == 119 or first == 120 or first == 122 or first == 175 or first == 180 or first == 182 or first == 183 or first == 220 or first == 221 or first == 222 or first == 223 or first == 36 or first == 39 or first == 42 or first == 49 or first == 58 or first == 59 or first == 60 or first == 61:
            return "asia"  # 多为中国/亚太
    except (ValueError, IndexError):
        pass
    return "other"


class NodeScheduler:
    """节点调度器 — 信誉分 + 负载 + 延迟加权轮询"""

    def __init__(self):
        self.nodes = {}          # peer_id -> node_info
        self.stats = defaultdict(lambda: {
            'reputation': 50,    # 0-100, start neutral
            'active_tasks': 0,
            'total_tasks': 0,
            'success_tasks': 0,
            'avg_latency': 500,  # ms
            'last_seen': 0,
            'online_duration': 0,
            'tier': 1,
            'consecutive_failures': 0,  # 连续失败次数(Health因子)
            'health': 100,  # 健康度0-100(100=健康, 连续失败降低, 成功恢复)
        })
        self.lock = threading.Lock()
        self._state_file = os.path.join(os.path.dirname(__file__), 'node_state.json')
        self._load_state()

    def _load_state(self):
        """Load node stats from disk on startup."""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file) as f:
                    data = json.load(f)
                for pid, info in data.get('nodes', {}).items():
                    self.nodes[pid] = info
                for pid, stats in data.get('stats', {}).items():
                    if pid not in self.stats:
                        self.stats[pid] = self.stats[pid].__class__()  # trigger default
                    self.stats[pid].update(stats)
                print(f"[Scheduler] Loaded {len(self.nodes)} nodes from disk")
        except Exception as e:
            print(f"[Scheduler] Failed to load state: {e}")

    def _save_state(self):
        """Persist node stats to disk."""
        try:
            data = {'nodes': self.nodes, 'stats': {k: dict(v) for k, v in self.stats.items()}}
            tmp = self._state_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, self._state_file)
        except Exception as e:
            print(f"[Scheduler] Failed to save state: {e}")

    def update_node(self, peer_id, **kwargs):
        with self.lock:
            s = self.stats[peer_id]
            for k, v in kwargs.items():
                if k in s:
                    s[k] = v
            s['last_seen'] = time.time()
        self._save_state()

    def on_task_start(self, peer_id):
        with self.lock:
            self.stats[peer_id]['active_tasks'] += 1

    def on_task_done(self, peer_id, latency_ms, success=True):
        with self.lock:
            s = self.stats[peer_id]
            s['active_tasks'] = max(0, s['active_tasks'] - 1)
            s['total_tasks'] += 1
            if success:
                s['success_tasks'] += 1
                # Update avg latency (EMA)
                s['avg_latency'] = s['avg_latency'] * 0.7 + latency_ms * 0.3
                # Reputation increases on success
                s['reputation'] = min(100, s['reputation'] + 1)
            else:
                s['reputation'] = max(0, s['reputation'] - 5)
                # Health: 连续失败降健康度
                s['consecutive_failures'] = s.get('consecutive_failures', 0) + 1
                s['health'] = max(0, 100 - s['consecutive_failures'] * 20)  # 每次失败-20
            # 成功时恢复health
            if success:
                s['consecutive_failures'] = 0
                s['health'] = min(100, s.get('health', 100) + 10)  # 成功+10
            # Decay inactive nodes
            elapsed = time.time() - s['last_seen']
            if elapsed > 300:
                s['reputation'] = max(0, s['reputation'] - int(elapsed / 300))

    def select_node(self, available_nodes, prefer_region=None, prefer_expert=None):
        """加权选择最佳节点
        Weight = reputation(40%) + load(30%) + latency(20%) + online(10%)
        v3: + expert match (+20) — 网络级 MoE: 任务派给最专业的那台电脑
        """
        if not available_nodes:
            return None

        with self.lock:
            scored = []
            for node in available_nodes:
                pid = node.get('peer_id', '')
                s = self.stats[pid]

                # 🔑 Health熔断: health<20的节点跳过(连续失败5次以上)
                if s.get('health', 100) < 20:
                    continue

                # 🔑 质量门槛兜底(#7): reputation极低或延迟极高的节点直接淘汰
                # reputation<10 = 几乎全是失败调用(刚启动=50, 连续失败降到10需15次失败)
                if s['reputation'] < 10:
                    continue
                # 延迟>5秒 = 节点网络严重劣化(正常<500ms)
                if s.get('avg_latency', 0) > 5000:
                    continue

                # 🔑 v3 专业匹配: 节点 expert_type 与任务需求一致 +20
                expert_score = 0
                if prefer_expert:
                    node_info = self.nodes.get(pid) or {}
                    if node_info.get('expert_type') == prefer_expert:
                        expert_score = 20

                # Health score (0-25) — 学OmniRoute, 健康度直接映射
                health = s.get('health', 100) / 100 * 25

                # Reputation score (0-30, 从40降到30给health让位)
                rep = s['reputation'] / 100 * 30

                # Load score (0-20)
                load = max(0, 20 - s['active_tasks'] * 7)

                # Latency score (0-15)
                lat = max(0, 15 - s['avg_latency'] / 60)

                # Online score (0-10)
                elapsed = time.time() - s['last_seen']
                online = 10 if elapsed < 60 else max(0, 10 - elapsed / 60)

                # 🔑 地理就近(2026-07-25): 同region节点+15分(降低跨区延迟)
                region_score = 0
                if prefer_region:
                    node_region = s.get('region', 'other')
                    if node_region == prefer_region:
                        region_score = 15  # 同区加分
                total = health + rep + load + lat + online + region_score + expert_score
                scored.append((total, node, pid))

            scored.sort(key=lambda x: x[0], reverse=True)

            if not scored:
                return None  # 所有节点被质量门槛淘汰, 上层会 fallback 到云端

            # Add some randomness to avoid always picking the same node
            # Pick from top 3 with probability proportional to score
            top = scored[:min(3, len(scored))]
            import random
            weights = [s[0] for s in top]
            total_w = sum(weights)
            if total_w > 0:
                r = random.uniform(0, total_w)
                cumulative = 0
                for w, node, pid in top:
                    cumulative += w
                    if r <= cumulative:
                        return node, pid
            return scored[0][1], scored[0][2]

    def get_stats(self):
        with self.lock:
            return {pid: dict(s) for pid, s in self.stats.items()}

scheduler = NodeScheduler()

# ========== Task Queue (NAT-traversal polling) ==========
class TaskQueue:
    """Task queue for edge node polling — nodes behind NAT pull tasks"""
    def __init__(self):
        self.tasks = {}        # task_id -> task_data
        self.pending = {}      # peer_id -> [task_ids]
        self.results = {}      # task_id -> result_data
        self.lock = threading.Lock()
        self._next_id = 1
        self._task_event = threading.Event()  # signaled when new task enqueued

    def enqueue(self, messages, model="meshmoe-edge", prefer_peer=None):
        """Enqueue a task, optionally targeting a specific peer"""
        with self.lock:
            task_id = f"task-{self._next_id}"
            self._next_id += 1
            task = {
                "task_id": task_id,
                "messages": messages,
                "model": model,
                "created_at": time.time(),
                "status": "pending"
            }
            self.tasks[task_id] = task
            if prefer_peer:
                self.pending.setdefault(prefer_peer, []).append(task_id)
        self._task_event.set()  # wake up any polling threads
        return task_id

    def poll(self, peer_id, timeout=30):
        """Edge node polls for a task. Returns task or None after timeout.
        Uses Event instead of busy-wait — no CPU spinning."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                # Check peer-specific queue first
                if peer_id in self.pending and self.pending[peer_id]:
                    task_id = self.pending[peer_id].pop(0)
                    if task_id in self.tasks:
                        self.tasks[task_id]["status"] = "processing"
                        self.tasks[task_id]["assigned_to"] = peer_id
                        return self.tasks[task_id]
                # Check for any unassigned task
                for tid, t in self.tasks.items():
                    if t["status"] == "pending" and "assigned_to" not in t:
                        t["status"] = "processing"
                        t["assigned_to"] = peer_id
                        return t
            # Wait for new task signal instead of busy-spinning
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._task_event.clear()
            self._task_event.wait(timeout=min(remaining, 5))
        return None

    def submit_result(self, task_id, result):
        """Edge node submits inference result"""
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id]["status"] = "completed"
                self.tasks[task_id]["completed_at"] = time.time()
                self.results[task_id] = result
                return True
            return False

    def get_result(self, task_id, timeout=60):
        """Router waits for edge result"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if task_id in self.results:
                    return self.results.pop(task_id)
            time.sleep(0.5)
        # Timed out
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id]["status"] = "timeout"
        return None

    def cleanup(self, max_age=600):
        """Remove old completed/timeout tasks"""
        with self.lock:
            now = time.time()
            expired = [tid for tid, t in self.tasks.items() 
                      if t["status"] in ("completed", "timeout") and now - t.get("completed_at", t.get("created_at", now)) > max_age]
            for tid in expired:
                self.tasks.pop(tid, None)
                self.results.pop(tid, None)

task_queue = TaskQueue()

# ========== Semantic Classifier ==========
SIMPLE_PATTERNS = {
    "translate": ["翻译","translate","翻成","译成"],
    "summarize": ["总结","摘要","概括","summarize","summary"],
    "greet": ["你好","hello","hi","早上好"],
    "qa_simple": ["是什么","什么是","怎么","如何","多少"],
    "classify": ["分类","classify","属于"],
    "extract": ["提取","抽出","找出","extract"],
    "rewrite": ["改写","润色","改一下","rewrite"],
}

COMPLEX_PATTERNS = {
    "code": ["写代码","编程","代码","bug","函数","算法","code","python"],
    "reasoning": ["推理","逻辑","证明","reasoning","为什么","分析"],
    "creative": ["写文章","创作","故事","小说","诗","文案"],
    "math": ["计算","数学","公式","方程","math","calculate"],
}

class Router:
    def discover_nodes(self):
        try:
            with urlopen(f"{BOOTSTRAP_URL}/nodes", timeout=5) as resp:
                data = json.loads(resp.read())
                return data.get("nodes", [])
        except Exception as e:
            print(f"[Router] Node discovery failed: {e}")
            return []

    def classify(self, messages):
        user_content = ""
        total_length = 0
        for msg in messages:
            if msg.get("role") == "user":
                user_content += msg.get("content", "") + " "
            total_length += len(msg.get("content", ""))

        sr = get_semantic_router()
        task_type, difficulty = sr.classify(user_content.strip())

        if total_length > 2000:
            difficulty = "complex"
        if total_length > 500 and difficulty == "simple":
            difficulty = "complex"

        return (difficulty, task_type)

    # 🔑 结果缓存(#6): 相同请求5分钟内返回缓存(省重复调用成本)
    # key = model + 用户消息hash, value = (result, timestamp)
    _response_cache = {}
    _CACHE_TTL = 300  # 5分钟

    def route_to_llm(self, messages, model_name="deepseek-v4-flash"):
        # 🔑 结果缓存检查(#6): 短消息+非流式才缓存(长对话不缓存, 避免存大context)
        import hashlib as _hl
        _cache_key = None
        if messages and sum(len(str(m.get("content",""))) for m in messages) < 2000:
            _msg_hash = _hl.md5(json.dumps({"m":messages,"model":model_name}, ensure_ascii=False).encode()).hexdigest()
            _cache_key = _msg_hash
            _cached = Router._response_cache.get(_cache_key)
            if _cached:
                _result, _ts = _cached
                if _t.time() - _ts < Router._CACHE_TTL:
                    print(f"[Router] 缓存命中({model_name}), 跳过LLM调用")
                    return _result
                else:
                    del Router._response_cache[_cache_key]  # 过期清除

        # 🔑 去掉litellm: 压缩上下文 + 直接调厂商API(CHANNELS表)
        result = None
        # 1. 压缩上下文(省42-90%: thinking裁剪+RTK去噪+Caveman精简)
        if COMPRESS_OK and len(messages) > 2:
            messages, comp_stats = compress_context(messages, "stacked")
            if comp_stats:
                total_saved = sum(comp_stats.values())
                print(f"[Router] 上下文压缩省了{total_saved}字符: {comp_stats}")
        # 2. 查渠道(去litellm, 直接调厂商)
        if COMPRESS_OK:
            ch = get_channel(model_name)
            if ch:
                data = json.dumps({"model": ch["model"], "messages": messages, "stream": False}).encode()
                req = Request(ch["base_url"].rstrip("/") + "/chat/completions", data=data, headers={
                    "Authorization": f"Bearer {ch['api_key']}",
                    "Content-Type": "application/json"
                })
                try:
                    with urlopen(req, timeout=60) as resp:
                        result = json.loads(resp.read())
                except Exception as e:
                    print(f"[Router] 直接调厂商{model_name}失败({e}), fallback到litellm")
            else:
                print(f"[Router] 模型{model_name}无渠道配置, fallback到litellm")
        # 3. fallback: litellm(兼容期)
        if result is None:
            data = json.dumps({"model": model_name, "messages": messages, "stream": False}).encode()
            req = Request(LITELLM_URL, data=data, headers={
                "Authorization": f"Bearer {LITELLM_KEY}",
                "Content-Type": "application/json"
            })
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        # 🔑 记录调用(动态价格惩罚: 贵模型频繁调用→惩罚加强→自动降级)
        try:
            _record_call(model_name)
        except Exception:
            pass
        # 🔑 存结果缓存(#6)
        if _cache_key and result:
            Router._response_cache[_cache_key] = (result, _t.time())
            # 顺带清理过期缓存(防内存增长)
            if len(Router._response_cache) > 500:
                _now = _t.time()
                for k in list(Router._response_cache.keys()):
                    if _now - Router._response_cache[k][1] > Router._CACHE_TTL:
                        del Router._response_cache[k]
        return result

    def route_to_edge(self, messages):
        """Route to edge node via task queue — nodes poll for tasks"""
        # Check if any nodes are online (seen in last 2 minutes)
        nodes = self.discover_nodes()
        online_nodes = []
        for n in nodes:
            pid = n.get("peer_id", "")
            stats = scheduler.get_stats().get(pid, {})
            elapsed = time.time() - stats.get('last_seen', 0)
            if elapsed < 120:  # seen in last 2 min
                online_nodes.append(n)
        
        if not online_nodes:
            # Check registered nodes from scheduler
            for pid, s in scheduler.get_stats().items():
                if time.time() - s.get('last_seen', 0) < 120:
                    online_nodes.append({"peer_id": pid})
        
        if not online_nodes:
            print("[Router] No online edge nodes, fallback to cloud")
            cloud = self.route_to_llm(messages, "deepseek-v4-flash")
            cloud["_routed_to"] = "cloud-fallback-no-node"
            return cloud

        # Select best node
        selected = scheduler.select_node(online_nodes)
        if not selected:
            cloud = self.route_to_llm(messages, "deepseek-v4-flash")
            cloud["_routed_to"] = "cloud-fallback-no-select"
            return cloud
        
        node, pid = selected
        # Queue task for this node
        task_id = task_queue.enqueue(messages, prefer_peer=pid)
        scheduler.on_task_start(pid)
        print(f"[Router] Task {task_id} queued for {pid}")

        # 同步等 edge 响应(15s 超时,体验阈值)
        result = task_queue.get_result(task_id, timeout=15)
        if result:
            result["_routed_to"] = f"edge-{pid}"
            result["_task_id"] = task_id
            return result

        # 超时 — fallback 云端,标 timeout 让 gateway 知道
        print(f"[Router] Task {task_id} timed out (15s), fallback to cloud")
        scheduler.on_task_done(pid, 15000, success=False)
        cloud = self.route_to_llm(messages, "deepseek-v4-flash")
        cloud["_routed_to"] = "cloud-fallback-timeout"
        return cloud

    def route_to_multi_expert(self, messages, task_type):
        """Multi-expert aggregation — 多专家并行推理 + 结果融合"""
        nodes = self.discover_nodes()
        if not nodes or len(nodes) < 2:
            print("[Router] Not enough nodes for multi-expert, falling back to single edge")
            return self.route_to_edge(messages)

        # Select top 2-3 nodes
        selected = []
        for _ in range(min(3, len(nodes))):
            s = scheduler.select_node(nodes)
            if s and s not in [(x[1], x[2]) for x in selected]:
                selected.append(s)

        if len(selected) < 2:
            return self.route_to_edge(messages)

        # Parallel inference
        results = []
        user_msg = next((m["content"] for m in messages if m.get("role") == "user"), "")
        
        for node, pid in selected:
            node_url = f"http://127.0.0.1:{node.get('port', 4002)}/v1/chat/completions"
            scheduler.on_task_start(pid)
            t0 = time.time()
            try:
                data = json.dumps({"model": "meshmoe-edge", "messages": messages, "stream": False}).encode()
                req = Request(node_url, data=data, headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read())
                text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                results.append({"expert": pid, "text": text})
                latency = (time.time() - t0) * 1000
                scheduler.on_task_done(pid, latency, success=True)
                print(f"[Router] Expert {pid}: {text[:60]}...")
            except Exception as e:
                latency = (time.time() - t0) * 1000
                scheduler.on_task_done(pid, latency, success=False)
                print(f"[Router] Expert {pid} failed: {e}")

        if not results:
            print("[Router] All experts failed, fallback to LLM")
            return self.route_to_llm(messages, "deepseek-v4-pro")

        # Aggregate results using local Hermes
        aggregated = self._aggregate(results, user_msg, task_type)
        return aggregated

    def _aggregate(self, expert_results, question, task_type):
        """融合多个专家结果 — 使用LiteLLM做聚合"""
        # Build aggregation prompt
        experts_text = ""
        for i, r in enumerate(expert_results):
            experts_text += f"\n专家{i+1} ({r['expert']}): {r['text']}\n"

        agg_prompt = f"""你是一个AI结果聚合器。以下是{len(expert_results)}个专家模型对同一个问题的回答。请综合所有专家的观点，给出一个完整、准确的最终回答。

问题: {question}

{experts_text}

请综合以上专家的回答，给出最终结果。如果专家回答相互矛盾，请指出差异并给出最合理的答案。"""

        # Use LiteLLM for aggregation (deepseek-v4-flash)
        try:
            result = self.route_to_llm(
                [{"role": "user", "content": agg_prompt}],
                model_name="deepseek-v4-flash"
            )
            text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if text:
                print(f"[Router] Aggregated from {len(expert_results)} experts via LiteLLM")
                return {"choices": [{"message": {"role": "assistant", "content": text}}], "model": "meshmoe-aggregated", "_experts": len(expert_results), "_aggregator": "litellm-deepseek-v4-flash"}
        except Exception as e:
            print(f"[Router] Aggregation failed ({e}), using best expert")

        # Fallback: use longest expert response
        best = max(expert_results, key=lambda x: len(x['text']))
        return {"choices": [{"message": {"role": "assistant", "content": best['text']}}], "model": "meshmoe-expert", "_expert": best['expert'], "_fallback": True}

    @staticmethod
    def _sanitize_prompt(text):
        """Sanitize user input before passing to subprocess — strip control chars, limit length."""
        if not isinstance(text, str):
            return ""
        # Remove null bytes and ASCII control characters (except newline/tab)
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
        # Limit length to prevent excessively long arguments
        MAX_PROMPT_LEN = 8192
        text = text[:MAX_PROMPT_LEN]
        return text.strip()

    def route_to_hermes(self, messages):
        try:
            user_msgs = [m["content"] for m in messages if m.get("role") == "user"]
            if not user_msgs:
                return self.route_to_llm(messages, "deepseek-v4-pro")
            prompt = self._sanitize_prompt(user_msgs[-1])
            if not prompt:
                return self.route_to_llm(messages, "deepseek-v4-pro")
            # Use list form (no shell=True) to prevent command injection.
            # --yolo removed to avoid bypassing safety checks.
            result = subprocess.run(
                ["hermes", "-z", prompt],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "HOME": os.environ.get("HOME", "/root")}
            )
            output = result.stdout.strip() or result.stderr.strip() or "Hermes returned no output"
            return {"choices": [{"message": {"role": "assistant", "content": output}}], "model": "hermes-agent"}
        except subprocess.TimeoutExpired:
            return self.route_to_llm(messages, "deepseek-v4-pro")
        except Exception as e:
            print(f"[Router] Hermes failed: {e}")
            return self.route_to_llm(messages, "deepseek-v4-pro")

    def _dispatch_to_edge(self, messages):
        """Fire-and-forget: dispatch task to an edge node for credit earning.
        User already got cloud result; edge does same task in parallel for participation.
        """
        try:
            online_nodes = []
            for pid, s in scheduler.get_stats().items():
                if time.time() - s.get('last_seen', 0) < 120:
                    online_nodes.append({"peer_id": pid})
            if not online_nodes:
                return
            selected = scheduler.select_node(online_nodes)
            if not selected:
                return
            node, pid = selected
            task_id = task_queue.enqueue(messages, prefer_peer=pid)
            scheduler.on_task_start(pid)
            print(f"[Router] Shadow task {task_id} dispatched to edge {pid}")
            # Don't wait for result — fire and forget
            # Edge will submit result later, we just record credit
            threading.Thread(target=self._await_shadow_result, args=(task_id, pid), daemon=True).start()
        except Exception as e:
            print(f"[Router] Shadow dispatch failed: {e}")

    def _await_shadow_result(self, task_id, peer_id):
        """Wait for edge shadow task result (background), record credit on success"""
        result = task_queue.get_result(task_id, timeout=120)
        if result:
            scheduler.on_task_done(peer_id, 0, success=True)
            print(f"[Router] Shadow task {task_id} completed by {peer_id}")
        else:
            scheduler.on_task_done(peer_id, 120000, success=False)
            print(f"[Router] Shadow task {task_id} timed out for {peer_id}")

    def route_to_heavy_node(self, messages):
        """Route to heavy-tier edge node. Returns result or None."""
        heavy_nodes = []
        for pid, s in scheduler.get_stats().items():
            if time.time() - s.get('last_seen', 0) < 120:
                node_info = scheduler.nodes.get(pid, {})
                if node_info.get("tier") == "heavy":
                    heavy_nodes.append({"peer_id": pid})
        if not heavy_nodes:
            return None

        selected = scheduler.select_node(heavy_nodes)
        if not selected:
            return None

        node_info, pid = selected
        task_id = task_queue.enqueue(messages, prefer_peer=pid)
        scheduler.on_task_start(pid)
        print(f"[Router] MoE Pro task {task_id} queued for heavy node {pid}")

        # Wait for result (heavy node, allow more time)
        result = task_queue.get_result(task_id, timeout=90)
        if result:
            result["_routed_to"] = f"heavy-{pid}"
            result["_task_id"] = task_id
            scheduler.on_task_done(pid, 0, success=True)
            return result

        scheduler.on_task_done(pid, 90000, success=False)
        print(f"[Router] MoE Pro task {task_id} timed out for {pid}")
        return None

    def handle_capability(self, body_obj, prefer_local=False):
        """能力调度入口: mesh-free/pro/max 三档, 按能力选模型"""
        messages = body_obj.get("messages", [])
        requested_model = body_obj.get("model", "mesh-pro")
        user_msg = next((m.get("content","") for m in messages if m.get("role")=="user"), "")

        # ①能力识别(<10ms规则)
        capability = classify_capability_qwen(user_msg) if CAP_ROUTER_OK else "general"  # Qwen3调度大脑(失败回退规则)
        difficulty = estimate_difficulty(user_msg)  # 🔑 二维调度第二维: 难度
        # 🔑 会话粘性(保前缀缓存省成本): 同会话+同档+非agent → 复用首次模型
        import time as _t
        sh = _session_hash(messages)
        if sh and difficulty != "agent":
            cached = _session_model.get(sh)
            if cached and cached.get("tier") == requested_model and (_t.time() - cached.get("ts", 0) < _SESSION_TTL):
                cached_model = cached["model"]
                print(f"[STICKY] session={sh} reuse {cached_model} (保前缀缓存)")
                result = self.route_to_llm(messages, cached_model)
                result["_routed_to"] = "sticky-" + cached_model
                result["model"] = requested_model
                result["_capability"] = capability
                return result

        # ②节点探测(免费档优先用节点)
        has_node = False
        try:
            nodes = self.discover_nodes()
            stats = scheduler.get_stats()
            for n in nodes:
                pid = n.get("peer_id","")
                if pid in stats and time.time() - stats[pid].get('last_seen',0) < 120:
                    has_node = True
                    break
        except Exception:
            pass

        print(f"[MeshMoE-CAP] {requested_model}/cap={capability}/node={has_node}: {user_msg[:60]}")
        # 🔑 反馈飞轮: 记录每次调度(用于持续学习)
        try:
            log_dispatch(user_msg, capability, capability, capability, routed_to=requested_model)
        except Exception:
            pass  # 飞轮失败不影响正常调度

        # ③三档分流
        if requested_model == "mesh-free":
            # 免费档: 节点优先, 无节点glm-4-flash兜底
            if has_node:
                edge = self.route_to_edge(messages)
                if edge and edge.get("_routed_to","").startswith("edge-"):
                    edge["model"] = "mesh-free"
                    edge["_capability"] = capability
                    return edge
            model = select_model(capability, "mesh-free", has_node, difficulty) if CAP_ROUTER_OK else "glm-4-flash"
            if model.startswith("node-"):
                # 节点模型但无在线节点 → 降级
                model = "glm-4-flash"
            result = self.route_to_llm(messages, model)
            result["_routed_to"] = "free-" + model
            result["model"] = "mesh-free"
            result["_capability"] = capability
            return result

        elif requested_model == "mesh-max":
            # 🔑 max = 旗舰池按能力选最强(2026-07-24, 用户拍板, 像pro一样选模型, 区别是池子是旗舰级)
            # select_model的max分支已改: 直接选该能力最强的旗舰
            #   推理/数学 → r1(10) | 编程/中文 → GLM-5.2(10) | 通用 → v4-pro(9)
            # max永远比pro强(旗舰池), 且比用户死调单模型强(我们选对该任务最强的旗舰)
            model = select_model(capability, "mesh-max", has_node, difficulty) if CAP_ROUTER_OK else "deepseek-v4-pro"
            if sh: _session_model[sh] = {"model": model, "tier": "mesh-max", "ts": _t.time()}  # 记粘性
            print(f"[MeshMoE-MAX] flagship by capability: {model} (cap={capability})")
            result = self.route_to_llm(messages, model)
            result["_routed_to"] = "max-" + model
            result["model"] = "mesh-max"
            result["_capability"] = capability
            return result

        elif requested_model == "mesh-research":
            # 深度研究档: 才触发MoA + agentic loop(重大课题/决策)
            # MoA: 能力互补proposers + 强aggregator, 多轮迭代
            # 🔑 总超时兜底(#5): MoA+refine 最坏180s, 超时降级为 mesh-max 单模型
            _research_start = _t.time()
            _RESEARCH_TOTAL_TIMEOUT = 120  # 总超时120s, 超时降级
            if CAP_ROUTER_OK:
                proposers = select_moa_proposers(capability, n=3)
                agg = select_aggregator()
                print(f"[MeshMoE-RESEARCH] MoA proposers={proposers} agg={agg}")
                from concurrent.futures import ThreadPoolExecutor
                results = [None]*len(proposers)
                def _call_p(idx, p):
                    try:
                        # 🔑 单proposer超时也缩短(40s, 留时间给聚合+refine)
                        _remaining = _RESEARCH_TOTAL_TIMEOUT - (_t.time() - _research_start)
                        if _remaining < 10:
                            return idx, None  # 时间不够了, 跳过
                        r = self.route_to_llm(messages, p)
                        text = r.get("choices",[{}])[0].get("message",{}).get("content","")
                        return idx, {"expert": p, "text": text}
                    except Exception as e:
                        print(f"[MeshMoE-RESEARCH] proposer {p} failed: {e}")
                        return idx, None
                with ThreadPoolExecutor(max_workers=len(proposers)) as ex:
                    futs = [ex.submit(_call_p, i, p) for i, p in enumerate(proposers)]
                    for fut in futs:
                        idx, res = fut.result()
                        if res: results[idx] = res
                results = [r for r in results if r]
                # 🔑 超时检查: 如果已经超了总时限, 直接用已有结果聚合(不再refine)
                _elapsed = _t.time() - _research_start
                if results and _elapsed < _RESEARCH_TOTAL_TIMEOUT - 30:
                    # 时间够: 聚合 + refine
                    agg_result = self._aggregate_by_model(results, next((m["content"] for m in messages if m.get("role")=="user"), ""), agg, capability)
                    return self._agent_refine_loop(agg_result, messages, agg, capability)
                elif results:
                    # 时间紧: 只聚合不refine
                    print(f"[RESEARCH] 时间紧({_elapsed:.0f}s), 跳过refine直接聚合")
                    return self._aggregate_by_model(results, next((m["content"] for m in messages if m.get("role")=="user"), ""), agg, capability)
                else:
                    # 所有proposer失败: 降级到mesh-max
                    print(f"[RESEARCH] 所有proposer失败, 降级mesh-max")
            model = select_model(capability, "mesh-max", has_node, difficulty) if CAP_ROUTER_OK else "deepseek-v4-pro"
            if sh: _session_model[sh] = {"model": model, "tier": "mesh-max", "ts": _t.time()}  # 记粘性
            result = self.route_to_llm(messages, model)
            result["_routed_to"] = "research-" + model
            result["model"] = "mesh-research"
            result["_capability"] = capability
            return result

        else:  # mesh-pro (默认)
            model = select_model(capability, "mesh-pro", has_node, difficulty) if CAP_ROUTER_OK else "deepseek-v4-flash"
            if sh: _session_model[sh] = {"model": model, "tier": "mesh-pro", "ts": _t.time()}  # 记粘性
            result = self.route_to_llm(messages, model)
            result["_routed_to"] = "pro-" + model
            result["model"] = "mesh-pro"
            result["_capability"] = capability
            return result

    def _agent_refine_loop(self, agg_result, messages, agg_model, capability):
        """agent self-refine: 让aggregator审查并精炼自己的答案(补漏/纠错/深化)。1轮即可。"""
        try:
            draft = agg_result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not draft or len(draft) < 50:
                return agg_result
            user_q = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
            refine_prompt = (
                "你刚才对一个研究问题给出了初步答案。请审查它, 找出遗漏、错误或不深入之处, "
                "然后给出更完整、准确、深入的最终版本。\n\n原始问题: " + user_q + "\n\n"
                "你的初步答案: " + draft[:3000] + "\n\n请直接输出精炼后的最终答案。"
            )
            refined = self.route_to_llm([{"role": "user", "content": refine_prompt}], agg_model)
            rtext = refined.get("choices", [{}])[0].get("message", {}).get("content", "")
            if rtext and len(rtext) > len(draft) * 0.5:
                refined["_routed_to"] = "research-refined"
                refined["_refined"] = True
                print(f"[RESEARCH] refine done: {len(draft)}->{len(rtext)} chars")
                return refined
        except Exception as e:
            print(f"[RESEARCH] refine failed: {e}")
        return agg_result

    def _aggregate_by_model(self, expert_results, question, agg_model, task_type):
        """MoA聚合 - 用指定强模型(glm-5.2)聚合"""
        import json as _json
        experts_text = ""
        for i, r in enumerate(expert_results):
            experts_text += f"\n专家{i+1} ({r['expert']}): {r['text'][:1500]}\n"
        agg_prompt = f"你是AI结果聚合器。以下是{len(expert_results)}个专家对同一问题的回答,请综合给出最佳答案。\n\n问题: {question}\n{experts_text}\n请综合给出最终结果,矛盾处指出差异。"
        try:
            result = self.route_to_llm([{"role":"user","content":agg_prompt}], agg_model)
            text = result.get("choices",[{}])[0].get("message",{}).get("content","")
            if text:
                return {"choices":[{"message":{"role":"assistant","content":text}}], "model":"mesh-max", "_aggregator":agg_model, "_experts":[r["expert"] for r in expert_results], "_capability":task_type}
        except Exception as e:
            print(f"[MeshMoE-MAX] agg {agg_model} failed: {e}")
        best = max(expert_results, key=lambda x: len(x["text"]))
        return {"choices":[{"message":{"role":"assistant","content":best["text"]}}], "model":"mesh-max", "_expert":best["expert"], "_fallback":True}

    def handle(self, request_body, prefer_local=False):
        body = json.loads(request_body)
        messages = body.get("messages", [])
        requested_model = body.get("model", "moe-lite")
        # === mesh-* 走能力调度(新), moe-* 走原逻辑 ===
        if requested_model.startswith("mesh-"):
            return self.handle_capability(body, prefer_local=prefer_local)
        difficulty, task_type = self.classify(messages)

        print(f"[MeshMoE] {requested_model}/{difficulty}/{task_type}: {messages[-1].get('content','')[:80]}")

        if difficulty == "agent":
            result = self.route_to_hermes(messages)
            result["_routed_to"] = "hermes-agent"
            result["model"] = "moe-pro"

        elif requested_model == "moe-pro":
            # MoE Pro: 复杂任务,重量节点优先(P2 才有),否则云端开源旗舰
            heavy_result = self.route_to_heavy_node(messages)
            if heavy_result:
                result = heavy_result
                result["model"] = "moe-pro"
            else:
                # 无重量节点,云端开源旗舰兜底(符合"复杂任务走云端"决策)
                print("[Router] No heavy node, fallback to cloud open-source flagship")
                result = self.route_to_llm(messages, "deepseek-v4-pro")
                result["_routed_to"] = "cloud-fallback-pro"
                result["model"] = "moe-pro"

        else:
            # MoE Lite: try edge node FIRST (同步等 edge 响应,符合"边缘命中=半价"决策)
            edge_result = self.route_to_edge(messages)
            if edge_result and "error" not in edge_result and edge_result.get("_routed_to", "").startswith("edge-"):
                # Edge 命中
                result = edge_result
                result["model"] = "moe-lite"
                print(f"[Router] Edge hit: {result.get('_routed_to')}")
            else:
                # Edge 未命中或超时,云端兜底(开源模型,meshtok 价)
                result = self.route_to_llm(messages, "deepseek-v4-flash")
                result["_routed_to"] = "cloud-fallback"
                result["model"] = "moe-lite"
                print("[Router] Cloud fallback (no edge or timeout)")

        result["_difficulty"] = difficulty
        result["_task_type"] = task_type
        return result

# ========== 会话级粘性路由(2026-07-25, 保前缀缓存省成本) ==========
# 同一会话复用首次选的模型(避免切模型毁厂商前缀缓存, 省59-85%输入)
# 会话标识: 用首轮用户消息的hash(同会话首句相同)
_session_model = {}  # session_hash -> {"model": x, "tier": y, "ts": z}
_SESSION_TTL = 1800  # 30分钟(超过重选, 模型可能更新)

def _session_hash(messages):
    """用首轮user消息内容hash作会话标识(同会话历史前缀相同)"""
    for m in messages:
        if m.get("role") == "user":
            import hashlib
            return hashlib.md5(m.get("content", "")[:200].encode()).hexdigest()[:12]
    return None

router = Router()

def cleanup_loop():
    """Periodically clean up old tasks"""
    while True:
        try:
            task_queue.cleanup()
        except:
            pass
        time.sleep(300)  # every 5 min

cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
        self.end_headers()
        self.wfile.write(body)

    def _send_stream_headers(self):
        """发送SSE流式响应头(chunked)"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_sse_chunk(self, data_bytes):
        """写一个chunked SSE数据块"""
        try:
            CRLF = chr(13) + chr(10)
            self.wfile.write(str(format(len(data_bytes), "X")).encode("ascii") + CRLF.encode("ascii"))
            self.wfile.write(data_bytes)
            self.wfile.write(CRLF.encode("ascii"))
            self.wfile.flush()
        except Exception:
            pass

    def _handle_stream(self, body_bytes):
        """流式: 能力调度选模型 → 流式调厂商 → 透传SSE。"""
        try:
            body = json.loads(body_bytes)
        except Exception:
            body = {}
        messages = body.get("messages", [])
        requested_model = body.get("model", "mesh-pro")
        user_msg = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")

        # 🔑 流式也加会话粘性(保前缀缓存, 和非流式 handle_capability 一致)
        # 同一会话复用首次选的模型, 避免切模型毁厂商前缀缓存(省59-85%输入成本)
        _sh = _session_hash(messages)
        _sticky_model = None
        if _sh:
            _cached = _session_model.get(_sh)
            if _cached and (_t.time() - _cached["ts"] < _SESSION_TTL):
                _sticky_model = _cached["model"]
                print(f"[STICKY-STREAM] session={_sh} reuse {_sticky_model} (保前缀缓存)")

        # 能力调度选模型(规则, 0延迟, 不实际调用LLM)
        model = None
        if _sticky_model:
            model = _sticky_model  # 粘性命中: 复用, 不重新选
        elif CAP_ROUTER_OK and requested_model.startswith("mesh-"):
            capability = classify_capability(user_msg)
            model = select_model(capability, requested_model, False)
            print("[MeshMoE-STREAM] " + requested_model + "/cap=" + capability + " -> " + str(model))
        elif requested_model.startswith("mesh-"):
            defaults = {"mesh-free": "glm-4-flash", "mesh-pro": "deepseek-v4-flash",
                        "mesh-max": "deepseek-v4-pro", "mesh-research": "deepseek-v4-pro"}
            model = defaults.get(requested_model, "deepseek-v4-flash")
        else:
            model = requested_model

        # 🔑 记录粘性(首次选模型后缓存, 后续同会话复用)
        if _sh and not _sticky_model and model:
            _session_model[_sh] = {"model": model, "tier": requested_model, "ts": _t.time()}

        # 压缩上下文
        if COMPRESS_OK and len(messages) > 2:
            try:
                messages, _ = compress_context(messages, "stacked")
            except Exception:
                pass

        # 查渠道
        upstream_url, upstream_headers, upstream_model = None, None, model
        if CAP_ROUTER_OK:
            ch = get_channel(model)
            if ch:
                upstream_url = ch["base_url"].rstrip("/") + "/chat/completions"
                upstream_headers = {"Authorization": "Bearer " + ch["api_key"],
                                    "Content-Type": "application/json",
                                    "Accept": "text/event-stream"}
                upstream_model = ch["model"]
        if not upstream_url:
            upstream_url = LITELLM_URL
            upstream_headers = {"Authorization": "Bearer " + LITELLM_KEY,
                                "Content-Type": "application/json",
                                "Accept": "text/event-stream"}

        req_body = json.dumps({"model": upstream_model, "messages": messages,
                               "stream": True, "stream_options": {"include_usage": True}},
                              ensure_ascii=False).encode("utf-8")

        self._send_stream_headers()
        NLNL = (chr(10) + chr(10)).encode("ascii")  # SSE行分隔
        DONE = b"data: [DONE]" + NLNL
        try:
            req = Request(upstream_url, data=req_body, method="POST")
            for k, v in upstream_headers.items():
                req.add_header(k, v)
            with urlopen(req, timeout=120) as resp:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self._write_sse_chunk(chunk)
            self._write_sse_chunk(DONE)
            self.wfile.write(b"0" + (chr(13)+chr(10)).encode("ascii") + (chr(13)+chr(10)).encode("ascii"))
        except Exception as e:
            err = json.dumps({"error": {"message": "stream failed: " + str(e)[:120],
                                        "type": "stream_error"}}, ensure_ascii=False)
            self._write_sse_chunk(b"data: " + err.encode("utf-8") + NLNL)
            self._write_sse_chunk(DONE)
            try:
                self.wfile.write(b"0" + (chr(13)+chr(10)).encode("ascii") + (chr(13)+chr(10)).encode("ascii"))
            except Exception:
                pass
            print("[MeshMoE-STREAM] failed: " + str(e))

    def do_OPTIONS(self):
        self._send_json({})


    def _node_token_ok(self, peer_id, token):
        """v3: 节点 token 校验 — 注册时带过 token 的节点, poll/result 必须凭证。
        未带 token 注册的节点(legacy)放行, 逐步收紧。"""
        info = scheduler.nodes.get(peer_id)
        if not info:
            return True  # unknown peer: let scheduler/stat path handle it
        expected = info.get("token_hash")
        if not expected:
            return True
        if not token:
            return False
        return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected)

    def _forward_joules(self, data):
        """v3: 把节点的 joules_est 最佳努力转发给 credits 服务入账(异步, 不阻塞)"""
        def _post():
            try:
                credits_url = os.getenv("MESHMOE_CREDITS_URL", "http://127.0.0.1:4005")
                req = Request(f"{credits_url.rstrip('/')}/report_joules",
                              data=json.dumps(data).encode(),
                              headers={"Content-Type": "application/json",
                                       "X-Internal-Key": os.getenv("MESHMOE_INTERNAL_KEY", "")},
                              method="POST")
                urlopen(req, timeout=5)
            except Exception as e:
                print(f"[Router] joules forward failed: {e}")
        threading.Thread(target=_post, daemon=True).start()

    def do_GET(self):
        if self.path == "/health":
            stats = scheduler.get_stats()
            online = sum(1 for s in stats.values() if time.time() - s.get('last_seen', 0) < 120)
            self._send_json({"status": "ok", "service": "MeshMoE Router v2", "online_nodes": online, "queued_tasks": len([t for t in task_queue.tasks.values() if t['status'] == 'pending'])})
        elif self.path == "/stats":
            self._send_json(scheduler.get_stats())
        elif self.path == "/nodes":
            # Node status — merge libp2p discovered + registered (scheduler is source of truth)
            stats = scheduler.get_stats()
            seen_pids = set()
            result = []

            # 1) Registered nodes from scheduler (primary source — these are real /register'd)
            _TIER_MAP = {0:'light', 1:'light', 2:'standard', 3:'heavy', 4:'cloud'}
            def _norm_tier(t):
                """Normalize tier to string ('light'/'standard'/'heavy'/'cloud')."""
                if isinstance(t, str) and t in _TIER_MAP.values():
                    return t
                if isinstance(t, (int, float)):
                    return _TIER_MAP.get(int(t), 'light')
                return 'light'
            for pid, s in stats.items():
                seen_pids.add(pid)
                reg_info = scheduler.nodes.get(pid, {})
                result.append({
                    "peer_id": pid,
                    "model": reg_info.get('model_name', 'unknown'),
                    "expert_type": reg_info.get('expert_type', 'general'),
                    "tier": _norm_tier(reg_info.get('tier', s.get('tier', 1))),
                    "reputation": s.get('reputation', 50),
                    "active_tasks": s.get('active_tasks', 0),
                    "total_tasks": s.get('total_tasks', 0),
                    "success_rate": round(s.get('success_tasks', 0) / max(1, s.get('total_tasks', 1)) * 100, 1),
                    "avg_latency_ms": round(s.get('avg_latency', 500)),
                    "last_seen_sec": round(time.time() - s.get('last_seen', 0)) if s.get('last_seen') else -1,
                    "online": (time.time() - s.get('last_seen', 0)) < 120 if s.get('last_seen') else False,
                    "source": "registered",
                })

            # 2) libp2p discovered nodes (supplementary — only add if not already registered)
            try:
                discovered = router.discover_nodes()
                for node in discovered:
                    pid = node.get('peer_id', '')
                    if pid and pid not in seen_pids:
                        seen_pids.add(pid)
                        s = stats.get(pid, {})
                        result.append({
                            "peer_id": pid,
                            "model": node.get('model_name', 'unknown'),
                            "expert_type": node.get('expert_type', 'general'),
                            "tier": _norm_tier(node.get('tier', s.get('tier', 1))),
                            "reputation": s.get('reputation', 50),
                            "active_tasks": s.get('active_tasks', 0),
                            "total_tasks": s.get('total_tasks', 0),
                            "success_rate": round(s.get('success_tasks', 0) / max(1, s.get('total_tasks', 1)) * 100, 1),
                            "avg_latency_ms": round(s.get('avg_latency', 500)),
                            "last_seen_sec": round(time.time() - s.get('last_seen', 0)) if s.get('last_seen') else -1,
                            "online": (time.time() - s.get('last_seen', 0)) < 120 if s.get('last_seen') else False,
                            "source": "libp2p",
                        })
            except Exception as e:
                print(f"[Router] /nodes discover supplementary failed: {e}")

            result.sort(key=lambda x: (x['online'], x['reputation']), reverse=True)
            self._send_json({"nodes": result, "total": len(result), "online": sum(1 for n in result if n['online'])})
        elif self.path.startswith("/task/poll/"):
            # Edge node polls for tasks: GET /task/poll/{peer_id}
            peer_id = self.path.split("/")[-1]
            # 🔑 v3: token 校验 (注册时带过 token 的节点必须凭证)
            if not self._node_token_ok(peer_id, self.headers.get("X-Node-Token", "")):
                self._send_json({"error": "invalid node token"}, 403)
                return
            # Update last_seen
            scheduler.update_node(peer_id)
            task = task_queue.poll(peer_id, timeout=25)
            if task:
                self._send_json(task)
            else:
                self._send_json({"status": "idle", "message": "no tasks"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions", "/register", "/task/result", "/task/queue"):
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 1048576:  # 1MB max request body
                self._send_json({"error": "request too large"}, 413)
                return
            body = self.rfile.read(length)
            if self.path == "/register":
                # Node registration endpoint
                data = json.loads(body)
                peer_id = data.get("peer_id", "unknown")
                expert = data.get("expert_type", "general")
                model = data.get("model_name", "unknown")
                port = data.get("port", 4002)
                tier = data.get("tier", "light")
                # 🔑 v3: token 归属认证 — credits 服务签发, 节点注册时带来
                node_token = data.get("node_token", "") or self.headers.get("X-Node-Token", "") or ""
                token_hash = hashlib.sha256(node_token.encode()).hexdigest() if node_token else None
                # 🔑 从注册请求IP推断region(地理就近调度用)
                client_ip = self.client_address[0]
                node_region = get_region_by_ip(client_ip)
                scheduler.nodes[peer_id] = {
                    "peer_id": peer_id,
                    "expert_type": expert,
                    "model_name": model,
                    "port": port,
                    "tier": tier,
                    "hardware": data.get("hardware", {}),
                    "region": node_region,
                    "protocol": data.get("protocol", 1),
                    "tools_supported": bool(data.get("tools_supported", False)),
                    "token_hash": token_hash,
                }
                scheduler.stats[peer_id]['region'] = node_region
                scheduler.update_node(peer_id,
                    reputation=scheduler.stats[peer_id].get('reputation', 50))
                print(f"[Router] Node registered: {peer_id} ({expert}/{model}/{tier}) :{port} proto={data.get('protocol', 1)} auth={'yes' if token_hash else 'no'}")
                self._send_json({"status": "ok", "peer_id": peer_id})
                return
            elif self.path == "/task/result":
                # Edge node submits inference result
                data = json.loads(body)
                task_id = data.get("task_id", "")
                peer_id = data.get("peer_id", "")
                result = data.get("result", {})
                success = data.get("success", True)
                latency = data.get("latency_ms", 0)
                # 🔑 v3: token 校验
                if not self._node_token_ok(peer_id, self.headers.get("X-Node-Token", "")):
                    self._send_json({"error": "invalid node token"}, 403)
                    return
                # Update scheduler
                scheduler.on_task_done(peer_id, latency, success=success)
                ok = task_queue.submit_result(task_id, result)
                # 🔑 v3: 焦耳转发 credits 入账 (最佳努力, 异步)
                joules_est = data.get("joules_est", 0)
                if joules_est:
                    hw = (scheduler.nodes.get(peer_id) or {}).get("hardware") or {}
                    self._forward_joules({
                        "node_id": peer_id,
                        "node_token": self.headers.get("X-Node-Token", ""),
                        "joules_est": joules_est,
                        "task_id": task_id,
                        "model": data.get("model"),
                        "gpu_name": hw.get("gpu_name"),
                        "duration_ms": latency,
                    })
                self._send_json({"status": "ok" if ok else "not_found"})
                return
            elif self.path == "/task/queue":
                # Internal: queue a task for edge processing
                data = json.loads(body)
                messages = data.get("messages", [])
                model = data.get("model", "meshmoe-edge")
                prefer_peer = data.get("prefer_peer")
                task_id = task_queue.enqueue(messages, model, prefer_peer)
                self._send_json({"task_id": task_id, "status": "queued"})
                return
            prefer_local = self.headers.get("X-Local-Mode", "").lower() == "true"
            # 🔑 流式分支(2026-07-24): gateway 流式转发到此后, 透传SSE给gateway
            try:
                is_stream = json.loads(body).get("stream", False)
            except Exception:
                is_stream = False
            if is_stream:
                self._handle_stream(body)
            else:
                result = router.handle(body, prefer_local=prefer_local)
                self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

if __name__ == "__main__":
    server = ThreadedHTTPServer(("0.0.0.0", 4001), Handler)
    server.allow_reuse_address = True
    import signal
    def shutdown(sig, frame):
        print(f"[MeshMoE Router v2] Received signal {sig}, shutting down...")
        server.shutdown()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print("[MeshMoE Router v2] Running on http://0.0.0.0:4001")
    print("[MeshMoE Router v2] Scheduler: reputation(40%)+load(30%)+latency(20%)+online(10%)")
    server.serve_forever()
