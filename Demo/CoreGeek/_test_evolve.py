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
    _answer_from_sandbox,
    _classify,
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
assert _classify("请阅读task_1_alpha.md，获取任务信息") == "engineering-fix"
assert _classify("请阅读task_1_beijing.md，获取任务信息") == "unknown-api"
assert task_family("请阅读task_1_beijing.md") == task_family("请阅读task_2_nanjing.md")

# 题干里直接给出的 token 仍可秒交；城市文物不再写死
skill = Skill(family="t")
token_ans = try_preset_answer("请提交 token=fc1e78eb2a5a", skill)
assert '"token"' in token_ans and "fc1e78eb2a5a" in token_ans
auth_ans = try_preset_answer("请提交认证码 0de1b57493cf", skill)
assert "0de1b57493cf" in auth_ans and '"token"' in auth_ans
bare_ans = try_preset_answer("提交以下字符串完成校验：c8be2288b213", skill)
assert "c8be2288b213" in bare_ans
assert try_preset_answer("请阅读task_1_beijing.md，获取任务信息", skill) == ""
assert try_preset_answer("查询北京世界遗产与文物统计", skill) == ""

# check TOKEN → 交卷
check_out = "[exitCode:0]\n[ OK ] 全部通过 (6/6) | TOKEN: fc1e78eb2a5a |"
got = _answer_from_sandbox(check_out, "请阅读task_1_alpha.md")
assert "fc1e78eb2a5a" in got

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

# 阅读 md：先 bootstrap，不写死交卷
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_1_beijing.md，获取任务信息"
payload["llmResp"] = ""
payload["lastCmdResult"] = ""
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("action") != "submitAnswer", out
assert "python3" in (out.get("executeCmd") or ""), out
assert "FWBUNDLE1" in out["executeCmd"]

# 沙盒给出 TOKEN 后应 submitAnswer
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_1_alpha.md，获取任务信息"
payload["lastCmdResult"] = (
    '[exitCode:0]\n[ OK ] 全部通过 (6/6) | TOKEN: 0de1b57493cf |'
)
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("action") == "submitAnswer", out
assert "0de1b57493cf" in cmd.get("taskAnswer", ""), cmd
assert out["executeCmd"] == ""

# HTTP 成功体应统计后交卷
reset_memory()
reset()
payload["phaseTask"] = "请阅读task_2_nanjing.md，获取任务信息"
payload["lastCmdResult"] = (
    '[exitCode:0] | FWHTTP1 {"ok": true, "status": 200, '
    '"body": "{\\"code\\": 200, \\"data\\": {\\"records\\": ['
    '{\\"id\\": \\"NJ001\\", \\"name\\": \\"明孝陵\\", \\"type\\": \\"陵墓\\", '
    '\\"era\\": \\"明\\", \\"protected_level\\": \\"世界遗产\\"}, '
    '{\\"id\\": \\"NJ002\\", \\"name\\": \\"鸡鸣寺\\", \\"type\\": \\"宗教建筑\\", '
    '\\"era\\": \\"南北朝\\", \\"protected_level\\": \\"全国重点\\"}]}}"} |'
)
out = decide(payload)
cmd = out["roleCommandMap"].get("10011") or {}
assert cmd.get("action") == "submitAnswer", out
answer = cmd.get("taskAnswer", "")
assert "南京" in answer or "total_count" in answer, cmd

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
assert (
    "10011" in out["roleCommandMap"]
    or 10011 in out["roleCommandMap"]
    or out["executeCmd"]
    or out["prompt"]
), out

# 开局工人应优先去建火箭炮（金币足够时）
reset_memory()
reset()
payload["phaseTask"] = ""
payload["roundNo"] = 1
payload["teamOur"]["goldNum"] = 80
out = decide(payload)
moves_or_builds = [
    c for c in out["roleCommandMap"].values()
    if c.get("action") in {"build", "move", "collect"}
]
assert moves_or_builds, out
worker_cmds = {
    k: v for k, v in out["roleCommandMap"].items() if k in {"10010", "10012"}
}
assert worker_cmds, out

print("evolve integration ok")
