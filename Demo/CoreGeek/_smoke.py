import json
import re
import sys

sys.path.insert(0, "src")

from agent.brain import decide
from agent.intel import reset_memory

raw = open("../../docs/request.txt", encoding="utf-8").read()
start = raw.index("{")
payload = json.loads(raw[start:])

reset_memory()
out = decide(payload)
print(json.dumps(out, ensure_ascii=False, indent=2)[:2000])

# 夜晚：roundNo 落在 71..130 区间
night = json.loads(json.dumps(payload))
night["roundNo"] = 80
reset_memory()
print(json.dumps(decide(night), ensure_ascii=False, indent=2)[:1500])

# 连续跑 1300 回合（10 天），确认不抛异常且响应结构完整
import time

reset_memory()
worst = 0.0
total = 0.0
for i in range(1, 1301):
    frame = json.loads(json.dumps(payload))
    frame["roundNo"] = i
    # 一半回合模拟上一回合全部失败，确保黑名单逻辑不会把自己锁死
    if i % 2 == 0:
        frame["lastRoundRoleActionResults"] = {
            str(r["id"]): False for r in frame["teamOur"]["roles"]
        }
    start = time.perf_counter()
    out = decide(frame)
    cost = time.perf_counter() - start
    worst = max(worst, cost)
    total += cost
    assert set(out) == {"roleCommandMap", "prompt", "executeCmd"}, out
    for key, cmd in out["roleCommandMap"].items():
        assert key.isdigit(), key
        assert "action" in cmd, cmd
        for pos in cmd.get("targetPos", ()):
            assert 0 <= pos["x"] < 41 and 0 <= pos["y"] < 32, cmd
print(f"1300 rounds ok  worst={worst*1000:.1f}ms  avg={total/1300*1000:.2f}ms")
