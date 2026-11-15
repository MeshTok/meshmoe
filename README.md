# MeshMoe

**Turn your idle compute into community memory.**

MeshMoe is a honor-driven distributed AI compute network. Every machine that
runs MeshMoe gets free, private, local AI (MeshMoe-4B, Apache-2.0). When the
machine is idle, it joins the mesh as a specialist, runs tasks for other
community members, and burns **Joules** — a permanent, public, append-only
record of the energy you contributed. Nothing is ever subtracted. Joules are
honor, not currency: non-consumable, non-transferable, non-exchangeable.

> Mesh + MoE (Mixture of Experts). In a model-level MoE, a router sends each
> token to the best expert sub-network. MeshMoe scales that to the network
> level: the router sends each task to the best machine.

## Why

BOINC lit up 3 million machines with pure honor. We inherited that playbook —
but for the agent era. One shared base model for the whole network, real
task trajectories flowing back to train the next generation, and a million
computers acting as one self-improving Mixture-of-Experts brain.

## Benchmarks (measured, not lab numbers)

| Metric | MeshMoe-4B (Q4_K_M) |
|---|---|
| Function-call accuracy (24 cases, 8 adversarial) | **95.8%** |
| Generation speed (RTX 3050 laptop, 8 GB VRAM) | **45 t/s** |
| Install size | **2.7 GB** |
| Context window | **32K** (verified on real agent runs) |
| CPU-only | works, 4 GB RAM |

Runs as a `llama-server` sidecar with native tool calling (`--jinja`) and
zero reasoning budget for snappy agent loops.

## Components

```
meshmoe/
├── node/edge_node.py        # Edge node v3: local inference + mesh worker
│                            #   - tool-calling passthrough (OpenAI-compatible)
│                            #   - node_token auth (X-Node-Token)
│                            #   - Joule metering: J = GPU watts x latency
│                            #   - register / poll / submit against the Router
├── dsh-meshmoe/             # DSH plugin: one-click install for DeepSeek Harness
│                            #   - merges user patch, preserves their providers
│                            #   - auto-starts llama-server (:8081)
│                            #   - honor poller (announces burned Joules)
├── site-v2/                 # Website (EN at /, zh at /zh/)
├── eval/                    # Regression harness: 24 FC + 6 agent cases
└── dsh-integration/         # Desktop packaging & launcher
```

## Quick start (dev)

```bash
# 1. local model (already installed by MeshMoe Desktop / DSH plugin)
llama-server -m MeshMoe-4B-Q4_K_M.gguf --port 8081 -c 32768 --jinja --reasoning-budget 0

# 2. edge node (joins the mesh, burns Joules)
set MESHMOE_NODE_TOKEN=<token-from-credits-server>
python node/edge_node.py

# 3. DSH plugin: dsh plugin install dsh-plugin-meshmoe (separate repo: MeshTok/dsh-plugin-meshmoe).
```

## The honor ladder

Contributor → Maker → Maintainer → Core → Legend (100 seats, ever).

Genesis Nodes: the first 1,000 node numbers are permanent and never re-issued.

## Status

Road to a million experts: `0 → 10K` geek community (now) → `10K → 300K`
teams + expert routing + trajectory distillation → `300K → 1M+` RL phase
change.

## License

Apache-2.0. MeshMoe-4B is fine-tuned from open Qwen3.5 / NeoHorse-1 lineage.
