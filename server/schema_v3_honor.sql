-- ============================================================
-- MeshMoe schema v3 "honor" migration
-- 目标: 打通 节点token认证 -> 焦耳记账 -> owner归属 三条链
-- 应用: psql -h 127.0.0.1 -U clawsq -d meshmoe -f schema_v3_honor.sql
-- 幂等: 可重复执行 (IF NOT EXISTS)
-- ============================================================

-- 1. nodes 表扩展: 归属 + 荣誉 + token + 专家维度
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS owner_user_id INT REFERENCES users(id);
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS joules BIGINT NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS node_token_hash TEXT;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS gpu_name TEXT;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS expert_type TEXT NOT NULL DEFAULT 'general';

-- 2. 焦耳账本 (append-only, 荣誉口径, 永不扣减)
CREATE TABLE IF NOT EXISTS joule_ledger (
    id            BIGSERIAL PRIMARY KEY,
    node_id       TEXT NOT NULL,
    owner_user_id INT REFERENCES users(id),
    joules        BIGINT NOT NULL,
    task_id       TEXT,
    model         TEXT,
    gpu_name      TEXT,
    duration_ms   INT,
    reason        TEXT NOT NULL DEFAULT 'mesh_task',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_joule_ledger_node ON joule_ledger (node_id, created_at);
CREATE INDEX IF NOT EXISTS idx_joule_ledger_owner ON joule_ledger (owner_user_id, created_at);

-- 3. 节点归属快速查询
CREATE INDEX IF NOT EXISTS idx_nodes_owner ON nodes (owner_user_id);
