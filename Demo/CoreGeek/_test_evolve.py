import json
import sys

sys.path.insert(0, "src")

from agent.brain import decide
from agent.evolve import (
    is_junk_answer,
    task_family,
    concrete_sandbox_answer,
    try_preset_answer,
    reset,
    Skill,
)
from agent.intel import reset_memory

assert is_junk_answer("./proc/1/schedstat")
assert is_junk_answer("- 建议将修复过程整理成可复用的 SOP")
assert not is_junk_answer('{"temp":26}')
assert concrete_sandbox_answer("[exitCode:0]\n./proc/1/schedstat") == ""
assert concrete_sandbox_answer("[exitCode:0]\ntotal 12") == ""
got = concrete_sandbox_answer('[exitCode:0]\nANSWER:{"city":"北京","temp":26}')
assert "北京" in got and "26" in got
assert task_family("查询北京天气，见 API_DOCS.md") == task_family(
    "查询上海天气，见 API_DOCS.md"
)

# 预设 SOP：token / 城市文物
skill = Skill(family="t")
token_ans = try_preset_answer('请提交 token=fc1e78eb2a5a', skill)
assert '"token"' in token_ans and "fc1e78eb2a5a" in token_ans
city_ans = try_preset_answer("查询北京世界遗产与文物统计", skill)
assert "周口店遗址" in city_ans and "total_count" in city_ans

raw = open("../../docs/request.txt", encoding="utf-8").read()
payload = json.loads(raw[raw.index("{") :])

# 白天可接任务：开拓者应优先走向/领取任务
reset_memory()
payload["roundNo"] = 5
payload["phaseTask"] = ""
out = decide(payload)
cmds = out["roleCommandMap"]
pioneer = cmds.get("10011") or cmds.get(10011)
assert pioneer is not None, cmds
assert pioneer["action"] in {"move", "acceptTask"}, pioneer

# 城市文物题：接取后应直接 submitAnswer 预设 JSON
reset_memory()
reset()
payload["phaseTask"] = "请统计南京文物：输出 city/total_count/types/oldest_era 等字段"
payload["llmResp"] = ""
payload["lastCmdResult"] = ""
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("action") == "submitAnswer", out
assert "南京" in cmd.get("taskAnswer", ""), cmd
assert out["executeCmd"] == ""
assert out["prompt"] == ""

# 垃圾 LLM 答案不得直接提交
reset_memory()
reset()
payload["phaseTask"] = "查询未知星球天气"
payload["llmResp"] = "ANSWER: - 建议将修复过程整理成可复用的 SOP"
payload["lastCmdResult"] = ""
out = decide(payload)
action = (out["roleCommandMap"].get("10011") or {}).get("action")
answer = (out["roleCommandMap"].get("10011") or {}).get("taskAnswer", "")
assert action != "submitAnswer" or not is_junk_answer(answer), out

# 开局工人应优先去建火箭炮（金币足够时）
reset_memory()
reset()
payload["phaseTask"] = ""
payload["roundNo"] = 1
payload["teamOur"]["goldNum"] = 80
out = decide(payload)
builds = [
    c for c in out["roleCommandMap"].values()
    if c.get("action") == "build" and c.get("name") == "rocket"
]
moves_or_builds = [
    c for c in out["roleCommandMap"].values()
    if c.get("action") in {"build", "move", "collect"}
]
assert moves_or_builds, out
# 至少一个工人在朝塔位走或已在建火箭
worker_cmds = {
    k: v for k, v in out["roleCommandMap"].items() if k in {"10010", "10012"}
}
assert worker_cmds, out

print("evolve integration ok")
