"""
MeshMoE Credit Settlement API
积分结算服务 — 余额/上报/扣减/充值
端口: 4005
"""
import json, os, time, signal, hashlib, hmac
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import psycopg2
import psycopg2.extras
import psycopg2.pool

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

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "127.0.0.1"),
    "port": os.getenv("PG_PORT", "5432"),
    "database": "meshmoe",
    "user": os.getenv("PG_USER", "clawsq"),
    "password": os.getenv("PG_PASSWORD", ""),
}

PORT = int(os.getenv("CREDITS_PORT", "4005"))
EARN_RATE = 100  # credits per 1000 tokens (base rate for tier 1)

# Internal API key — shared between Router/Gateway/Edge services
INTERNAL_API_KEY = os.getenv("MESHMOE_INTERNAL_KEY", "meshmoe-internal-2026")

# Tier multipliers (5档异构节点, 见 MESHMOE-ARCHITECTURE-v2.md 第四节)
# Tier0=手机(0.5-2B) / Tier1=轻量电脑(8B) / Tier2=标准(14B) / Tier3=重量(32B) / Tier4=算力中心(旗舰)
# 倍数 = 贡献算力价值权重, earn/spend 对称
TIER_MULTIPLIER = {0: 0.3, 1: 1, 2: 3, 3: 10, 4: 30}

# Edge nodes may send tier as a word ("light"/"standard"/"heavy"/"ultra") or a number;
# the DB column is smallint, so normalize before any SQL.
TIER_WORDS = {"light": 1, "standard": 2, "medium": 2, "heavy": 3, "ultra": 4}
def tier_int(value, default=1):
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if 0 <= value <= 4 else default
    if isinstance(value, float):
        return int(value) if 0 <= value <= 4 else default
    if isinstance(value, str):
        s = value.strip().lower()
        if s.isdigit():
            n = int(s)
            return n if 0 <= n <= 4 else default
        return TIER_WORDS.get(s, default)
    return default


# ========== Joule honor system (v3) ==========
# GPU power table (watts) — mirrors node/edge_node.py GPU_POWER_TABLE
JOULE_POWER_WATTS = {
    "rtx 3050": 80, "rtx 3060": 115, "rtx 3070": 125, "rtx 3080": 320,
    "rtx 3090": 350, "rtx 4050": 65, "rtx 4060": 115, "rtx 4070": 200,
    "rtx 4080": 320, "rtx 4090": 450, "gtx 1650": 75, "gtx 1060": 120,
    "apple m1": 20, "apple m2": 22, "apple m3": 22, "apple m4": 25,
}
CPU_DEFAULT_WATTS = 65          # CPU-only fallback
JOULE_SANITY_TOLERANCE = 1.5    # allow +50% vs theoretical max (boost clocks etc.)
JOULE_MIN_FLOOR = 50            # minimum plausible per-report floor (J)

NODE_TOKEN_VERSION = "node-token-v1"

def _node_token_secret():
    return (INTERNAL_API_KEY + NODE_TOKEN_VERSION).encode()

def issue_node_token(node_id):
    """Deterministic per-node token: HMAC(node_id, internal secret)."""
    return hmac.new(_node_token_secret(), node_id.encode(), hashlib.sha256).hexdigest()[:40]

def hash_node_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

def verify_node_token(node_id, token, stored_hash):
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(hash_node_token(token), stored_hash)

def node_power_watts(gpu_name):
    g = (gpu_name or "").lower()
    for k, w in JOULE_POWER_WATTS.items():
        if k in g:
            return w
    return CPU_DEFAULT_WATTS

def record_joules(conn, node_id, joules, task_id=None, model=None,
                  gpu_name=None, duration_ms=None, reason="mesh_task"):
    """Validate + append joule entry. Returns accepted joules (clamped)."""
    joules = max(0, int(joules or 0))
    if joules > 0:
        max_watts = node_power_watts(gpu_name)
        if duration_ms and duration_ms > 0:
            theoretical = max_watts * duration_ms / 1000.0
            cap = int(max(JOULE_MIN_FLOOR, theoretical * JOULE_SANITY_TOLERANCE))
            joules = min(joules, cap)
    if joules <= 0:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT owner_user_id FROM nodes WHERE peer_id = %s", (node_id,))
        row = cur.fetchone()
        owner = row[0] if row else None
        cur.execute(
            """INSERT INTO joule_ledger (node_id, owner_user_id, joules, task_id, model, gpu_name, duration_ms, reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (node_id, owner, joules, task_id, model, gpu_name, duration_ms, reason)
        )
        cur.execute("UPDATE nodes SET joules = joules + %s, last_seen = NOW() WHERE peer_id = %s",
                    (joules, node_id))
    return joules

def get_node_honor(conn, node_id):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT peer_id, joules, total_tasks, total_tokens, owner_user_id, expert_type, gpu_name
               FROM nodes WHERE peer_id = %s""", (node_id,))
        row = cur.fetchone()
        if not row:
            return None
        owner_joules = 0
        if row[4]:
            cur.execute("SELECT COALESCE(SUM(joules),0) FROM joule_ledger WHERE owner_user_id = %s", (row[4],))
            owner_joules = cur.fetchone()[0]
    return {"node_id": row[0], "joules": row[1], "tasks": row[2], "tokens": row[3],
            "owned": bool(row[4]), "expert_type": row[5], "gpu_name": row[6],
            "owner_joules": owner_joules}

# ========== Connection Pool ==========
_pool = None

def _get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.SimpleConnectionPool(1, 10, **DB_CONFIG)
    return _pool

def _get_conn():
    return _get_pool().getconn()

def _put_conn(conn):
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass

def get_user(conn, username):
    """获取或创建用户"""
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, credits, sharing_enabled, total_earned, total_spent FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        if row:
            return {"id": row[0], "username": row[1], "credits": row[2], "sharing_enabled": row[3], "total_earned": row[4] or 0, "total_spent": row[5] or 0}
        cur.execute(
            "INSERT INTO users (username, credits, sharing_enabled) VALUES (%s, 500, true) RETURNING id, credits, sharing_enabled, total_earned, total_spent",
            (username,)
        )
        row = cur.fetchone()
        return {"id": row[0], "username": username, "credits": row[1], "sharing_enabled": row[2], "total_earned": 0, "total_spent": 0}

def earn_credits(conn, user_id, username, tokens, node_id, model, task_type, tier=1):
    """推理赚积分 — 按节点等级x1/x3/x10"""
    multiplier = TIER_MULTIPLIER.get(tier, 1)
    credits = max(1, int(tokens * EARN_RATE * multiplier / 1000))
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET credits = credits + %s, total_earned = total_earned + %s, updated_at = NOW() WHERE id = %s RETURNING credits",
            (credits, credits, user_id)
        )
        new_balance = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO credit_transactions (user_id, type, amount, balance_after, description) VALUES (%s, 'earn', %s, %s, %s)",
            (user_id, credits, new_balance, f"Inference: {tokens} tokens, model={model}")
        )
        cur.execute(
            "INSERT INTO inference_records (user_id, node_id, model_name, task_type, tokens_in, tokens_out) VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, node_id, model, task_type, tokens, tokens)
        )
        cur.execute(
            """INSERT INTO nodes (peer_id, model_name, total_tasks, total_tokens, last_seen)
               VALUES (%s, %s, 1, %s, NOW())
               ON CONFLICT (peer_id) DO UPDATE SET total_tasks = nodes.total_tasks + 1,
               total_tokens = nodes.total_tokens + %s, last_seen = NOW()""",
            (node_id, model, tokens, tokens)
        )
    return credits, new_balance

def spend_credits(conn, user_id, username, tokens, tier=1):
    """消费积分 — 按tier扣费"""
    multiplier = TIER_MULTIPLIER.get(tier, 1)
    credits = max(1, int(tokens * EARN_RATE * multiplier / 1000))
    warning = None

    user = get_user_from_id(conn, user_id)
    total_earned = user.get('total_earned', 0)
    total_spent = user.get('total_spent', 0)

    if total_earned > 0 and (total_spent + credits) > total_earned * 1.5:
        warning = "Your usage exceeds 1.5x your earnings. Enable compute sharing for free usage."

    with conn.cursor() as cur:
        cur.execute("SELECT credits FROM users WHERE id = %s FOR UPDATE", (user_id,))
        balance = cur.fetchone()[0]
        if balance < credits:
            return 0, balance, "insufficient_credits", warning
        cur.execute(
            "UPDATE users SET credits = credits - %s, total_spent = total_spent + %s, updated_at = NOW() WHERE id = %s RETURNING credits",
            (credits, credits, user_id)
        )
        new_balance = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO credit_transactions (user_id, type, amount, balance_after, description) VALUES (%s, 'spend', %s, %s, %s)",
            (user_id, credits, new_balance, f"AI usage: {tokens} tokens (tier {tier})")
        )
    return credits, new_balance, "ok", warning

def get_user_from_id(conn, user_id):
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, credits, sharing_enabled, total_earned, total_spent FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            return {"id": row[0], "username": row[1], "credits": row[2], "sharing_enabled": row[3], "total_earned": row[4] or 0, "total_spent": row[5] or 0}
    return {"total_earned": 0, "total_spent": 0}

class CreditHandler(BaseHTTPRequestHandler):
    def _send(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Internal-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _check_internal_auth(self):
        """Verify internal API key for write operations.
        Accepts: X-Internal-Key header or Authorization: Bearer <key>"""
        key = self.headers.get("X-Internal-Key", "")
        if not key:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                key = auth[7:]
        if key == INTERNAL_API_KEY:
            return True
        # Also allow if request comes from localhost (internal services)
        client_ip = self.client_address[0]
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            return True
        return False

    def do_OPTIONS(self):
        """CORS preflight"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Internal-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send({"status": "ok", "service": "MeshMoE Credits"})
        elif self.path.startswith("/balance/"):
            username = self.path.split("/")[-1]
            conn = _get_conn()
            conn.autocommit = True
            try:
                user = get_user(conn, username)
                # Joule honor: sum of ledger entries owned by this user
                joules_total = 0
                with conn.cursor() as cur:
                    cur.execute("SELECT COALESCE(SUM(joules),0) FROM joule_ledger WHERE owner_user_id = %s", (user["id"],))
                    joules_total = cur.fetchone()[0]
                self._send({
                    "username": user["username"],
                    "credits": user["credits"],
                    "sharing_enabled": user["sharing_enabled"],
                    "total_earned": user["total_earned"],
                    "total_spent": user["total_spent"],
                    "joules": joules_total,
                    "joules_unit": "J"
                })
            finally:
                _put_conn(conn)
        elif self.path.startswith("/transactions/"):
            username = self.path.split("/")[-1]
            conn = _get_conn()
            conn.autocommit = True
            try:
                user = get_user(conn, username)
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT type, amount, balance_after, description, created_at FROM credit_transactions WHERE user_id = %s ORDER BY created_at DESC LIMIT 50",
                        (user["id"],)
                    )
                    rows = cur.fetchall()
                    records = []
                    for r in rows:
                        records.append({
                            "type": r["type"],
                            "amount": r["amount"],
                            "balance": r["balance_after"],
                            "description": r["description"],
                            "time": r["created_at"].isoformat() if r["created_at"] else ""
                        })
                self._send({"username": username, "records": records})
            finally:
                _put_conn(conn)
        elif self.path.startswith("/node/"):
            # Node honor card: joules, tasks, owner binding status
            node_id = self.path.split("/")[-1]
            conn = _get_conn()
            conn.autocommit = True
            try:
                honor = get_node_honor(conn, node_id)
                if honor:
                    self._send(honor)
                else:
                    self._send({"error": "unknown node"}, 404)
            finally:
                _put_conn(conn)
        elif self.path == "/stats":
            conn = _get_conn()
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM users")
                    total_users = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM inference_records")
                    total_inferences = cur.fetchone()[0]
                    cur.execute("SELECT COALESCE(SUM(total_tokens),0) FROM nodes")
                    total_tokens = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM nodes WHERE last_seen > NOW() - INTERVAL '2 minutes'")
                    online_nodes = cur.fetchone()[0]
                self._send({"users": total_users, "inferences": total_inferences, "tokens": total_tokens, "online_nodes": online_nodes})
            finally:
                _put_conn(conn)
        elif self.path.startswith("/preferences/"):
            # GET /preferences/{username} — read user compute preferences
            username = self.path.split("/")[-1]
            conn = _get_conn()
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT preferences FROM users WHERE username = %s", (username,))
                    row = cur.fetchone()
                prefs = row[0] if row and row[0] else {}
                # Merge with defaults
                defaults = {"cpu_limit": "50%", "gpu_limit": "70%", "charge_only": True, "time_range": "All day", "custom_start": "22:00", "custom_end": "06:00"}
                defaults.update(prefs)
                self._send(defaults)
            finally:
                _put_conn(conn)
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 65536:  # 64KB max request body
                self._send({"error": "request too large"}, 413)
                return
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except:
            self._send({"error": "invalid JSON"}, 400)
            return

        # POST endpoints require internal auth, EXCEPT the three node-facing
        # endpoints: they authenticate via node_token (issued at /register_node)
        # — user PCs never hold MESHMOE_INTERNAL_KEY.
        NODE_FACING = ("/register_node", "/claim_node", "/report_joules")
        if self.path not in NODE_FACING and not self._check_internal_auth():
            self._send({"error": "unauthorized — provide X-Internal-Key header"}, 401)
            return

        conn = _get_conn()
        try:
            if self.path == "/report":
                # Edge node reports inference — earns credits
                username = body.get("username", "anonymous")
                tokens = body.get("tokens", 0)
                node_id = body.get("node_id", username)
                model = body.get("model", "unknown")
                task_type = body.get("task_type", "general")
                tier = body.get("tier", 1)

                # Anti-abuse: cap single report to 50K tokens
                tokens = min(tokens, 50000)

                user = get_user(conn, username)
                credits, balance = earn_credits(conn, user["id"], username, tokens, node_id, model, task_type, tier)
                conn.commit()
                self._send({"status": "ok", "username": username, "earned": credits, "balance": balance, "tier": tier, "multiplier": TIER_MULTIPLIER.get(tier, 1)})

            elif self.path == "/spend":
                # Deduct credits — called by Gateway/Router
                username = body.get("username", "anonymous")
                tokens = body.get("tokens", 0)
                tier = body.get("tier", 1)

                user = get_user(conn, username)
                credits_needed = max(1, int(tokens * EARN_RATE * TIER_MULTIPLIER.get(tier, 1) / 1000))
                _, balance, status, warning = spend_credits(conn, user["id"], username, tokens, tier)
                conn.commit()
                resp = {"status": status, "username": username, "balance": balance}
                if status == "insufficient_credits":
                    resp["need"] = credits_needed
                    if warning: resp["warning"] = warning
                    self._send(resp, 402)
                else:
                    resp["spent"] = credits_needed
                    if warning: resp["warning"] = warning
                    self._send(resp)

            elif self.path == "/topup":
                # Top-up — INTERNAL ONLY (called by payment callback, not users)
                # Additional safety: require explicit confirmation field
                username = body.get("username", "anonymous")
                amount = body.get("amount", 0)
                confirmation = body.get("confirmation", "")

                if amount <= 0 or amount > 100000:
                    self._send({"error": "invalid amount"}, 400)
                    return
                # Require payment confirmation code
                if not confirmation:
                    self._send({"error": "missing payment confirmation"}, 400)
                    return

                user = get_user(conn, username)
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET credits = credits + %s, updated_at = NOW() WHERE id = %s RETURNING credits", (amount, user["id"]))
                    balance = cur.fetchone()[0]
                    cur.execute("INSERT INTO credit_transactions (user_id, type, amount, balance_after, description) VALUES (%s, 'topup', %s, %s, %s)", (user["id"], amount, balance, f"Top-up: +{amount} credits"))
                conn.commit()
                self._send({"status": "ok", "username": username, "topup": amount, "balance": balance})

            elif self.path == "/toggle_sharing":
                username = body.get("username", "anonymous")
                enabled = body.get("enabled", True)
                user = get_user(conn, username)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET sharing_enabled = %s, updated_at = NOW() WHERE id = %s RETURNING sharing_enabled",
                        (enabled, user["id"])
                    )
                    new_val = cur.fetchone()[0]
                conn.commit()
                self._send({"status": "ok", "username": username, "sharing_enabled": new_val})

            elif self.path == "/register_node":
                # v3: node self-registration. username optional (anonymous ok).
                # Issues deterministic node_token; stores only its sha256 hash.
                username = body.get("username", "anonymous")
                node_id = body.get("node_id", username)
                gpu_name = body.get("gpu_name", "none")
                expert_type = body.get("expert_type", "general")
                tier = tier_int(body.get("tier", 1))
                provided_token = body.get("node_token", "") or \
                    (self.headers.get("X-Node-Token", "") or "")

                with conn.cursor() as cur:
                    cur.execute("SELECT node_token_hash, owner_user_id FROM nodes WHERE peer_id = %s", (node_id,))
                    row = cur.fetchone()
                    if row and row[0]:
                        # already claimed: token must match
                        if provided_token and verify_node_token(node_id, provided_token, row[0]):
                            token = provided_token
                        else:
                            conn.rollback()
                            self._send({"error": "node already claimed — token required"}, 403)
                            return
                        cur.execute(
                            """UPDATE nodes SET last_seen = NOW(), gpu_name = %s,
                               expert_type = %s, tier = COALESCE(%s, tier) WHERE peer_id = %s""",
                            (gpu_name, expert_type, tier, node_id)
                        )
                    else:
                        user = get_user(conn, username) if username != "anonymous" else None
                        token = issue_node_token(node_id)
                        cur.execute(
                            """INSERT INTO nodes (peer_id, model_name, total_tasks, total_tokens, last_seen,
                               owner_user_id, node_token_hash, gpu_name, expert_type, tier)
                               VALUES (%s, %s, 0, 0, NOW(), %s, %s, %s, %s, %s)
                               ON CONFLICT (peer_id) DO UPDATE SET last_seen = NOW(),
                               gpu_name = EXCLUDED.gpu_name, expert_type = EXCLUDED.expert_type,
                               node_token_hash = EXCLUDED.node_token_hash,
                               owner_user_id = COALESCE(EXCLUDED.owner_user_id, nodes.owner_user_id)""",
                            (node_id, gpu_name, user["id"] if user else None,
                             hash_node_token(token), gpu_name, expert_type, tier)
                        )
                conn.commit()
                self._send({"status": "ok", "node_id": node_id, "tier": tier,
                            "node_token": token,
                            "owned": username != "anonymous"})

            elif self.path == "/claim_node":
                # Bind an anonymous node to a user account; honor follows the owner.
                node_id = body.get("node_id", "")
                token = body.get("node_token", "") or self.headers.get("X-Node-Token", "")
                username = body.get("username", "")
                if not node_id or not username:
                    self._send({"error": "node_id and username required"}, 400)
                    return
                # 注：账号必须在 node/token 校验通过之后才创建。否则任何人用任意邮箱
                # POST 一次不存在的 node_id，就能凭空建出用户并拿到 500 初始 credits。
                with conn.cursor() as cur:
                    cur.execute("SELECT node_token_hash, owner_user_id FROM nodes WHERE peer_id = %s FOR UPDATE", (node_id,))
                    row = cur.fetchone()
                    if not row:
                        conn.rollback()
                        self._send({"error": "unknown node_id"}, 404)
                        return
                    if row[1]:
                        conn.rollback()
                        self._send({"error": "node already owned"}, 409)
                        return
                    if not verify_node_token(node_id, token, row[0]):
                        conn.rollback()
                        self._send({"error": "invalid node token"}, 403)
                        return
                    # 校验全部通过，这才建号（幂等：已存在则复用）
                    user = get_user(conn, username)
                    cur.execute("UPDATE nodes SET owner_user_id = %s WHERE peer_id = %s", (user["id"], node_id))
                    cur.execute(
                        "UPDATE joule_ledger SET owner_user_id = %s WHERE node_id = %s AND owner_user_id IS NULL",
                        (user["id"], node_id)
                    )
                conn.commit()
                self._send({"status": "ok", "node_id": node_id, "owner": username})

            elif self.path == "/report_joules":
                # Edge node reports energy contribution. X-Node-Token required.
                node_id = body.get("node_id", "")
                token = body.get("node_token", "") or self.headers.get("X-Node-Token", "")
                joules_est = body.get("joules_est", 0)
                task_id = body.get("task_id")
                model = body.get("model")
                gpu_name = body.get("gpu_name")
                duration_ms = body.get("duration_ms")
                if not node_id:
                    self._send({"error": "node_id required"}, 400)
                    return
                with conn.cursor() as cur:
                    cur.execute("SELECT node_token_hash FROM nodes WHERE peer_id = %s", (node_id,))
                    row = cur.fetchone()
                if not row:
                    conn.rollback()
                    self._send({"error": "unknown node_id"}, 404)
                    return
                if row[0] and not verify_node_token(node_id, token, row[0]):
                    # Trusted internal caller (router forwarding a verified node result,
                    # or localhost service) may record on the node's behalf.
                    if not self._check_internal_auth():
                        conn.rollback()
                        self._send({"error": "invalid node token"}, 403)
                        return
                accepted = record_joules(conn, node_id, joules_est, task_id=task_id,
                                         model=model, gpu_name=gpu_name,
                                         duration_ms=duration_ms)
                conn.commit()
                self._send({"status": "ok", "node_id": node_id,
                            "joules_reported": joules_est, "joules_accepted": accepted})

            elif self.path == "/preferences":
                # POST /preferences — save user compute preferences
                username = body.get("username", "anonymous")
                prefs = body.get("preferences", {})
                # Validate prefs keys
                allowed = {"cpu_limit", "gpu_limit", "charge_only", "time_range", "custom_start", "custom_end"}
                prefs = {k: v for k, v in prefs.items() if k in allowed}
                user = get_user(conn, username)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET preferences = %s, updated_at = NOW() WHERE id = %s",
                        (json.dumps(prefs), user["id"])
                    )
                conn.commit()
                self._send({"status": "ok", "username": username, "preferences": prefs})

            else:
                self._send({"error": "not found"}, 404)
        except Exception as e:
            conn.rollback()
            self._send({"error": str(e)}, 500)
        finally:
            _put_conn(conn)

    def log_message(self, format, *args):
        pass  # Suppress per-request logging

def main():
    print(f"[Credits API] Starting on port {PORT}")
    print(f"[Credits API] Database: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    server = ThreadedHTTPServer(("0.0.0.0", PORT), CreditHandler)
    server.allow_reuse_address = True
    def shutdown(sig, frame):
        print(f"[Credits API] Received signal {sig}, shutting down...")
        server.shutdown()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()

if __name__ == "__main__":
    main()
