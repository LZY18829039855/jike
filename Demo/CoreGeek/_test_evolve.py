import json
import sys

sys.path.insert(0, "src")

from agent.brain import decide
from agent.evolve import is_junk_answer, task_family, concrete_sandbox_answer, reset
from agent.intel import reset_memory

assert is_junk_answer("- 建议将修复过程整理成可复用的 SOP")
assert not is_junk_answer('{"temp":26}')
assert concrete_sandbox_answer("[exitCode:0]\ntotal 12") == ""
got = concrete_sandbox_answer('[exitCode:0]\nANSWER:{"city":"北京","temp":26}')
assert "北京" in got and "26" in got
assert task_family("查询北京天气，见 API_DOCS.md") == task_family(
    "查询上海天气，见 API_DOCS.md"
)

raw = open("../../docs/request.txt", encoding="utf-8").read()
payload = json.loads(raw[raw.index("{") :])

# 白天可接任务：开拓者应优先走向/领取任务，而不是先去宝藏推理
reset_memory()
payload["roundNo"] = 5
payload["phaseTask"] = ""
out = decide(payload)
cmds = out["roleCommandMap"]
pioneer = cmds.get("10011") or cmds.get(10011)
assert pioneer is not None, cmds
assert pioneer["action"] in {"move", "acceptTask"}, pioneer

# 已接任务：应发出探索 executeCmd 或 prompt，而不是交垃圾 SOP
reset_memory()
reset()
payload["phaseTask"] = (
    "给你 API_DOCS.md，请查询北京天气。先探索沙盒再提交 JSON 答案。"
)
payload["llmResp"] = ""
payload["lastCmdResult"] = ""
out = decide(payload)
assert out["executeCmd"] or out["prompt"] or (
    (out["roleCommandMap"].get("10011") or {}).get("action") == "submitAnswer"
), out

# 垃圾 LLM 答案不得直接提交
reset_memory()
reset()
payload["phaseTask"] = "查询北京天气"
payload["llmResp"] = "ANSWER: - 建议将修复过程整理成可复用的 SOP"
payload["lastCmdResult"] = ""
out = decide(payload)
action = (out["roleCommandMap"].get("10011") or {}).get("action")
answer = (out["roleCommandMap"].get("10011") or {}).get("taskAnswer", "")
assert action != "submitAnswer" or not is_junk_answer(answer), out

print("evolve integration ok")
