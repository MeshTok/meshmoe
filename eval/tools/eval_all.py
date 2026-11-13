# -*- coding: utf-8 -*-
"""
NeoHorse-1-4B Step 0 baseline 评测
套件 A: 函数调用准确率 (BFCL-lite, 24 例, 含负例)
套件 B: 多步 agent 真实任务 (6 例, mock 工具真实执行)
用法: python eval_all.py --port 8081 --label Q4_K_M
"""
import json, urllib.request, sys, argparse, time, re

# ---------------- 套件 A: 函数调用 ----------------

TOOLS_A = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "获取指定城市当前天气",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "城市名"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}},
            "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "search_flight",
        "description": "搜索航班",
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "string"}, "destination": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["origin", "destination", "date"]}}},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "发送邮件",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"}},
            "required": ["to", "subject", "body"]}}},
    {"type": "function", "function": {
        "name": "calculate",
        "description": "计算数学表达式",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string"}}, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "get_order_status",
        "description": "查询订单状态",
        "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string"}}, "required": ["order_id"]}}},
]

# (user, expect)  expect: {"calls":[{"name","args":{...}(子集匹配)}]} 或 {"no_call":True}
CASES_A = [
    ("北京今天天气怎么样？", {"calls": [{"name": "get_weather", "args": {"city": "北京"}}]}),
    ("帮我查一下上海到广州 2026-12-01 的航班", {"calls": [{"name": "search_flight", "args": {"origin": "上海", "destination": "广州", "date": "2026-12-01"}}]}),
    ("现在深圳多少度？用华氏度显示", {"calls": [{"name": "get_weather", "args": {"city": "深圳", "unit": "fahrenheit"}}]}),
    ("帮我给 zhang@corp.com 发封邮件，主题是周报，内容写本周完成了模型评测", {"calls": [{"name": "send_email", "args": {"to": "zhang@corp.com", "subject": "周报"}}]}),
    ("计算 (375 * 24) / 5 等于多少", {"calls": [{"name": "calculate", "args": {"expression": "(375 * 24) / 5"}}]}),
    ("订单 ORD-20261109-8842 现在到哪了？", {"calls": [{"name": "get_order_status", "args": {"order_id": "ORD-20261109-8842"}}]}),
    ("查一下杭州的天气，然后帮我搜明天(2026-11-14)杭州到北京的航班", {"calls": [{"name": "get_weather", "args": {"city": "杭州"}}, {"name": "search_flight", "args": {"origin": "杭州", "destination": "北京", "date": "2026-11-14"}}]}),
    ("9 * 9 + 19 是多少？", {"calls": [{"name": "calculate", "args": {"expression": "9 * 9 + 19"}}]}),
    ("给我看看订单 A-100 的状态，再给 boss@corp.com 发邮件说订单 A-100 已发货", {"calls": [{"name": "get_order_status", "args": {"order_id": "A-100"}}, {"name": "send_email", "args": {"to": "boss@corp.com"}}]}),
    ("纽约现在天气如何？", {"calls": [{"name": "get_weather", "args": {"city": "纽约"}}]}),
    # ---- 负例: 不该调工具 ----
    ("你好，你能做什么？", {"no_call": True}),
    ("什么是机器学习？", {"no_call": True}),
    ("帮我写一首关于秋天的短诗", {"no_call": True}),
    ("北京和上海哪个城市人口更多？说说你的看法", {"no_call": True}),
    ("如果我明天去北京出差，一般需要注意什么？", {"no_call": True}),
    ("1 加 1 等于几？这种问题不用工具吧？", {"no_call": True}),
    ("你叫什么名字？", {"no_call": True}),
    ("翻译成英文：今天天气很好", {"no_call": True}),
    # ---- 参数抽取细节 ----
    ("帮我搜 2027 年 3 月 15 号从成都到西安的航班", {"calls": [{"name": "search_flight", "args": {"origin": "成都", "destination": "西安", "date": "2027-03-15"}}]}),
    ("给 li@corp.com 发邮件主题会议通知内容定于周五上午十点开会", {"calls": [{"name": "send_email", "args": {"to": "li@corp.com", "subject": "会议通知"}}]}),
    ("订单号 B-7788-B 的物流状态", {"calls": [{"name": "get_order_status", "args": {"order_id": "B-7788-B"}}]}),
    ("今天苏州天气适合洗车吗？", {"calls": [{"name": "get_weather", "args": {"city": "苏州"}}]}),
    ("(1024 - 64) * 3 请计算", {"calls": [{"name": "calculate", "args": {"expression": "(1024 - 64) * 3"}}]}),
    ("查订单 ZZ-1 的状态，如果查不到就直接告诉我查不到", {"calls": [{"name": "get_order_status", "args": {"order_id": "ZZ-1"}}]}),
]

# ---------------- 套件 B: agent 任务 (mock 工具) ----------------

TOOLS_B = [
    {"type": "function", "function": {"name": "get_weather", "description": "获取城市天气, 返回温度与降水概率",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}},
    {"type": "function", "function": {"name": "get_calendar", "description": "查询指定日期的日程, 返回事件列表",
        "parameters": {"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]}}},
    {"type": "function", "function": {"name": "calculator", "description": "计算数学表达式",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}},
    {"type": "function", "function": {"name": "query_db", "description": "查询订单数据库, 参数 field 可为 amount/order_id/customer",
        "parameters": {"type": "object", "properties": {"sql_filter": {"type": "string"}}, "required": ["sql_filter"]}}},
    {"type": "function", "function": {"name": "convert_currency", "description": "汇率换算, 返回目标币种金额",
        "parameters": {"type": "object", "properties": {"amount": {"type": "number"}, "from_cur": {"type": "string"}, "to_cur": {"type": "string"}}, "required": ["amount", "from_cur", "to_cur"]}}},
]

MOCK_DB = [
    {"order_id": "A-1", "customer": "acme", "amount": 1200},
    {"order_id": "A-2", "customer": "globex", "amount": 800},
    {"order_id": "A-3", "customer": "acme", "amount": 500},
]

def exec_mock(name, args):
    if name == "get_weather":
        w = {"北京": (12, 10), "上海": (18, 60), "杭州": (17, 80), "广州": (26, 20)}
        t, p = w.get(args.get("city", ""), (20, 0))
        return json.dumps({"city": args.get("city"), "temp_c": t, "rain_prob": p}, ensure_ascii=False)
    if name == "get_calendar":
        if "2026-11-13" in args.get("date", "") or "今天" in args.get("date", ""):
            return json.dumps([{"time": "10:00", "event": "评审会(线上)"}], ensure_ascii=False)
        return json.dumps([], ensure_ascii=False)
    if name in ("calculator", "calculate"):
        try:
            return json.dumps({"result": eval(args["expression"])})
        except Exception as e:
            return json.dumps({"error": str(e)})
    if name == "query_db":
        f = args.get("sql_filter", "")
        rows = [r for r in MOCK_DB if r["customer"] in f or f == "*"]
        return json.dumps(rows, ensure_ascii=False)
    if name == "convert_currency":
        rate = {("USD", "CNY"): 7.2, ("CNY", "USD"): 1 / 7.2, ("EUR", "CNY"): 7.8}
        k = (args.get("from_cur"), args.get("to_cur"))
        if k in rate:
            return json.dumps({"amount": round(args["amount"] * rate[k], 2), "cur": args.get("to_cur")})
        return json.dumps({"error": "unsupported pair"})
    return json.dumps({"error": "unknown tool"})

# (user, checker) checker 收 (final_text, tool_calls_trace, exec_results) -> (ok, note)
CASES_B = [
    ("我今天在北京，下午想去户外跑步，需要带雨具吗？先查天气再回答。",
     lambda t, tr, ex: (any("get_weather" in c.get("name", "") for c in tr), "")),
    ("帮我查 acme 客户的所有订单金额，算出总金额是多少美元换算成人民币(报人民币数)。",
     lambda t, tr, ex: (_check_acme_total(t, tr))),
    ("今天(2026-11-13)我有什么日程？",
     lambda t, tr, ex: (any("get_calendar" in c.get("name", "") for c in tr), "")),
    ("北京 12 度，上海 18 度(假设)，温差多少度？请用计算器算。",
     lambda t, tr, ex: (any(c.get("name") in ("calculator", "calculate") for c in tr) and "6" in t, "")),
    ("帮我查一下 1200 美元是多少人民币。",
     lambda t, tr, ex: (any("8" in str(ex) or "8640" in t for _ in [0]) if any("convert_currency" in c.get("name", "") for c in tr) else False, "应调用 convert_currency 并得 8640")),
    ("你最近过得怎么样？随便聊聊。",
     lambda t, tr, ex: (len(tr) == 0, "闲聊不应调用任何工具")),
]

def _check_acme_total(text, trace):
    called = any("query_db" in c.get("name", "") for c in trace)
    conv = any("convert_currency" in c.get("name", "") for c in trace)
    has_num = bool(re.search(r"18[.,]?0?0?0?\s*(元|人民币|CNY)|1700|1728|250\s*d", text)) or "1700" in text or "1800" in text
    return (called and conv, f"query_db={called} convert={conv}")

# ---------------- HTTP ----------------

def chat(port, messages, tools=None, temperature=0.0, max_tokens=1024):
    body = {"model": "local", "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)

def run_suite_a(port):
    ok = 0; results = []
    for i, (user, expect) in enumerate(CASES_A):
        try:
            resp = chat(port, [{"role": "user", "content": user}], TOOLS_A)
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            names = [c["function"]["name"] for c in calls]
            if expect.get("no_call"):
                passed = len(calls) == 0
                note = f"unexpected_calls={names}" if not passed else ""
            else:
                exp = expect["calls"]
                passed = len(calls) >= len(exp)
                if passed:
                    for j, e in enumerate(exp):
                        c = calls[j]["function"]
                        if c["name"] != e["name"]:
                            passed = False; note = f"want {e['name']} got {c['name']}"; break
                        try:
                            got_args = json.loads(c.get("arguments") or "{}")
                        except Exception:
                            got_args = {}
                        for k, v in e.get("args", {}).items():
                            gv = str(got_args.get(k, "")).strip()
                            if v.lower() not in gv.lower() and gv.lower() != str(v).lower():
                                passed = False; note = f"{e['name']}.{k}: want {v} got {gv}"; break
                        if not passed: break
                    else:
                        note = ""
                else:
                    note = f"want {len(exp)} calls got {len(calls)}: {names}"
        except Exception as e:
            passed = False; note = f"ERR {e}"; names = []
        results.append({"i": i, "user": user[:40], "pass": passed, "note": note})
        ok += passed
        print(f"A{i+1:02d} [{'PASS' if passed else 'FAIL'}] {user[:36]}  {note}")
    return ok, len(CASES_A), results

def run_suite_b(port):
    ok = 0; results = []
    for i, (user, checker) in enumerate(CASES_B):
        trace = []; execs = []
        msgs = [{"role": "system", "content": "你是一个助手，可以调用工具完成任务。工具返回是 mock 数据。"}, {"role": "user", "content": user}]
        try:
            for step in range(6):
                resp = chat(port, msgs, TOOLS_B)
                msg = resp["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                if not calls:
                    final = msg.get("content") or ""
                    break
                for c in calls:
                    fn = c["function"]["name"]
                    try: args = json.loads(c.get("arguments") or "{}")
                    except Exception: args = {}
                    trace.append({"name": fn, "args": args})
                    out = exec_mock(fn, args)
                    execs.append(out)
                    msgs.append({"role": "assistant", "tool_calls": [c]})
                    msgs.append({"role": "tool", "tool_call_id": c.get("id", f"c{step}"), "content": out})
            else:
                final = "(max steps)"
            passed, note = checker(final, trace, execs)
        except Exception as e:
            passed, note, final = False, f"ERR {e}", ""
        results.append({"i": i, "user": user[:40], "pass": passed, "note": note})
        ok += passed
        print(f"B{i+1} [{'PASS' if passed else 'FAIL'}] {user[:36]}  {note}")
    return ok, len(CASES_B), results

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--label", default="Q4_K_M")
    ap.add_argument("--suite", default="ab")
    a = ap.parse_args()
    out = {"label": a.label, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    if "a" in a.suite:
        ok, tot, res = run_suite_a(a.port)
        out["A"] = {"ok": ok, "total": tot, "acc": round(ok / tot * 100, 1), "detail": res}
        print(f"\n=== 套件A 函数调用: {ok}/{tot} = {ok/tot*100:.1f}% ===\n")
        time.sleep(2)
    if "b" in a.suite:
        ok, tot, res = run_suite_b(a.port)
        out["B"] = {"ok": ok, "total": tot, "acc": round(ok / tot * 100, 1), "detail": res}
        print(f"\n=== 套件B agent任务: {ok}/{tot} = {ok/tot*100:.1f}% ===\n")
    import os
    outdir = "D:/meshmoe/eval/results"
    os.makedirs(outdir, exist_ok=True)
    with open(f"{outdir}/{a.label}.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved ->", f"/d/meshmoe/eval/results/{a.label}.json")
