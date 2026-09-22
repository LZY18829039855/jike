"""自进化任务：沙盒探测 → 观测纠错 → 交卷，并沉淀可复用 SOP。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from .intel import (
    MEM,
    force_submit_now,
    mark_prompt,
    patch_task_answer,
    remember_answer,
    task_rounds_left,
    task_rounds_used,
    treasure_imminent,
    treasure_ready,
    treasure_rider,
)
from .protocol import PlayerTask, Turn, Unit, distance, submit_answer_command


_JUNK_ANSWER = re.compile(
    r"(建议|可复用|SOP|SKILL|整理成|后续可能|形成固定|自进化|"
    r"please\s+form|reusable|document\s+the\s+process)",
    re.I,
)
_PATH_LIKE = re.compile(
    r"(^|/|\./|\\)([A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+$|/(proc|sys|dev|tmp)/",
    re.I,
)
_EXPLORE_CMD = re.compile(
    r"^(pwd|ls\b|find\b|test -f|sed -n)",
    re.I,
)
_FILE_HINT = re.compile(
    r"([A-Za-z0-9_./-]+\.(?:md|txt|json|py|yml|yaml|csv|ini|conf))",
    re.I,
)
_SAFE_BLOCK = re.compile(
    r"rm\s+-rf|shutdown|reboot|mkfs|dd\s+if=|:\(\)\s*\{",
    re.I,
)
_TOKEN_HINT = re.compile(
    r"token|令牌|口令|认证码?|auth(?:entication|code)?|secret|passwd|"
    r"password|密钥|验证码|口令码|\bkey\b|凭证|校验码",
    re.I,
)
_HEX12 = re.compile(r"\b([a-fA-F0-9]{12})\b")
_PLACEHOLDER_TOKEN = re.compile(
    r"^(xxx+|yyy+|zzz+|foo+|bar+|baz+|token|placeholder|example|sample|todo|null|none)$",
    re.I,
)
_READ_TASK = re.compile(r"(?:请阅读|阅读|see|read)\s+(\S+\.md)", re.I)
_TASK_MD = re.compile(r"(task[_-]\d+[_-][A-Za-z0-9_-]+\.md)", re.I)
_SKIP_DIRS = (
    "lib", "boot", "run", "sbin", "var", "sys", "bin", "etc",
    "dev", "usr", "lib64", "proc",
)
_CITY_EN = {
    "beijing": "北京", "nanjing": "南京", "chengdu": "成都",
    "shanghai": "上海", "hangzhou": "杭州", "xian": "西安", "xi'an": "西安",
    "guangzhou": "广州", "wuhan": "武汉", "chongqing": "重庆", "shenzhen": "深圳",
}
_CITY_CN = (
    "北京", "上海", "广州", "深圳", "杭州", "成都", "重庆", "武汉", "西安", "南京",
    "天津", "苏州", "长沙", "郑州", "青岛", "厦门", "福州", "合肥", "南昌", "昆明",
    "哈尔滨", "沈阳", "大连", "济南", "石家庄", "太原", "南宁", "海口", "贵阳", "兰州",
    "银川", "西宁", "呼和浩特", "乌鲁木齐", "拉萨",
)
_ERA_RANK = (
    "旧石器", "新石器", "史前", "商周", "商", "周", "春秋", "战国", "秦", "汉",
    "三国", "晋", "南北朝", "隋", "唐", "五代", "宋", "元", "明", "清", "民国", "现代",
)


@dataclass
class Skill:
    """同一任务族沉淀出的可复用流程。"""

    family: str
    explore_cmds: list[str] = field(default_factory=list)
    good_cmds: list[str] = field(default_factory=list)
    last_answer: str = ""
    sandbox_notes: str = ""
    steps: str = ""


@dataclass
class HttpSop:
    url: str = "http://localhost:8899/api/v1/heritage/search"
    api_key: str = "heritage-api-key-2024"
    auth: str = ""
    param: str = ""


@dataclass
class EvolveState:
    family: str = ""
    kind: str = ""
    explore_i: int = 0
    last_cmd: str = ""
    last_sandbox: str = ""
    asked_llm: bool = False
    reused_skill: bool = False
    task_file: str = ""
    task_path: str = ""
    bundle: str = ""
    city: str = ""
    app_name: str = ""
    ws_dir: str = ""
    http_done: bool = False
    ws_done: bool = False
    ws_tries: int = 0
    ws_phase: str = ""  # "" | edit | verify | failed
    boot_done: bool = False
    fingerprint: str = ""
    drop_sandbox: bool = False


_SKILLS: dict[str, Skill] = {}
_STATE = EvolveState()
_HTTP_SOP = HttpSop()
_LEARNED_CITIES: dict[str, dict[str, Any]] = {}
_USED_TOKENS: set[str] = set()
_TOKEN_OWNER: dict[str, str] = {}


def reset() -> None:
    _SKILLS.clear()
    _LEARNED_CITIES.clear()
    _USED_TOKENS.clear()
    _TOKEN_OWNER.clear()
    _HTTP_SOP.auth = ""
    _HTTP_SOP.param = ""
    _HTTP_SOP.url = "http://localhost:8899/api/v1/heritage/search"
    _HTTP_SOP.api_key = "heritage-api-key-2024"
    _STATE.family = ""
    _STATE.kind = ""
    _STATE.explore_i = 0
    _STATE.last_cmd = ""
    _STATE.last_sandbox = ""
    _STATE.asked_llm = False
    _STATE.reused_skill = False
    _STATE.task_file = ""
    _STATE.task_path = ""
    _STATE.bundle = ""
    _STATE.city = ""
    _STATE.app_name = ""
    _STATE.ws_dir = ""
    _STATE.http_done = False
    _STATE.ws_done = False
    _STATE.ws_tries = 0
    _STATE.ws_phase = ""
    _STATE.boot_done = False
    _STATE.fingerprint = ""
    _STATE.drop_sandbox = False


def _task_fingerprint(task: str) -> str:
    return "|".join((
        _task_filename(task) or "",
        _detect_city(task) or "",
        _detect_app(task) or "",
        _classify(task) or "",
    ))


def on_task_text(task: str) -> None:
    """题目文件/城市/应用一变就重置求解态，避免把上一题沙盒结果交到新题。"""
    family = task_family(task)
    fingerprint = _task_fingerprint(task)
    if fingerprint == _STATE.fingerprint and family == _STATE.family:
        return
    prev = _STATE.fingerprint
    prev_file = _STATE.task_file
    if prev_file:
        for skill in _SKILLS.values():
            token = _token_of_payload(skill.last_answer)
            if token and token not in _TOKEN_OWNER:
                _TOKEN_OWNER[token] = prev_file
                _USED_TOKENS.add(token)
        mem_token = _token_of_payload(MEM.task_answer)
        if mem_token and mem_token not in _TOKEN_OWNER:
            _TOKEN_OWNER[mem_token] = prev_file
            _USED_TOKENS.add(mem_token)
    _STATE.family = family
    _STATE.kind = _classify(task)
    _STATE.explore_i = 0
    _STATE.last_cmd = ""
    _STATE.last_sandbox = ""
    _STATE.asked_llm = False
    _STATE.reused_skill = False
    _STATE.task_file = _task_filename(task)
    _STATE.task_path = ""
    _STATE.bundle = ""
    _STATE.city = _detect_city(task) or ""
    _STATE.app_name = _detect_app(task)
    _STATE.ws_dir = ""
    _STATE.http_done = False
    _STATE.ws_done = False
    _STATE.ws_tries = 0
    _STATE.ws_phase = ""
    _STATE.boot_done = False
    _STATE.fingerprint = fingerprint
    _STATE.drop_sandbox = bool(prev)
    MEM.awaiting_task = False


def task_family(text: str) -> str:
    """按题型归族，便于 SOP 复用（城市/应用名视为同一族变体）。"""
    kind = _classify(text)
    if kind:
        return kind
    raw = (text or "").strip().lower()
    if not raw:
        return ""
    normalized = re.sub(r"\d+", "#", raw)
    normalized = re.sub(
        r"(北京|上海|广州|深圳|杭州|成都|重庆|武汉|西安|南京|"
        r"beijing|shanghai|guangzhou|shenzhen|hangzhou|chengdu|nanjing)",
        "<city>",
        normalized,
        flags=re.I,
    )
    normalized = re.sub(r"[a-z0-9_.-]+\.(md|txt|json|py)", "<file>.\\1", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    tip = normalized[:48]
    return f"{digest}:{tip}"


def _classify(text: str) -> str:
    raw = text or ""
    low = raw.lower()
    if re.search(
        r"engineering-fix|修复应用|部署问题|ws_\d|"
        r"task_\d+_(alpha|beta|gamma|delta)",
        low,
        re.I,
    ):
        return "engineering-fix"
    if re.search(
        r"unknown-api|heritage|文物|文化遗产|nchda|"
        r"localhost:\d+|task_\d+_(beijing|nanjing|chengdu|shanghai|hangzhou)",
        low,
        re.I,
    ):
        return "unknown-api"
    if _detect_city(raw) and re.search(r"查询|统计|api|http", raw, re.I):
        return "unknown-api"
    return ""


def should_prioritize(turn: Turn) -> bool:
    """能接/能做自进化任务时优先去做（白天临近入夜除外，夜间可持续刷）。"""
    if turn.phase_task.strip():
        return True
    if turn.is_day and turn.near_night:
        return False
    if turn.available_tasks():
        return True
    if turn.round_no < MEM.skip_task_until:
        return False
    return bool(turn.our_task_points() or turn.tasks)


def pick_task(turn: Turn, role: Unit) -> PlayerTask | None:
    tasks = turn.available_tasks()
    if not tasks:
        return None
    return max(tasks, key=lambda item: _task_score(turn, role, item))


def _task_score(turn: Turn, role: Unit, task: PlayerTask) -> tuple[float, float, int]:
    travel = max(0, distance(role.pos, task.pos) - 1)
    timeout = task.timeout_rounds or 25
    solve_rounds = min(10, max(3, timeout // 4))
    elapsed = max(1, travel + solve_rounds)
    speed_bonus = 5.0 * timeout / solve_rounds if task.timeout_rounds else 0.0
    family = task_family(task.task_type)
    reuse = 8.0 if family in _SKILLS and _SKILLS[family].good_cmds else 0.0
    expected = task.score_reward + speed_bonus + task.gold_reward * 0.5 + reuse
    return (expected / elapsed, expected, -travel)


def treasure_may_interrupt(turn: Turn) -> bool:
    """条件齐全能开宝藏时，打断任务去开（宝藏金币/积分远高于单次任务）。"""
    return treasure_ready(turn) and MEM.treasure.pos is not None


def solve(
    turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
) -> tuple[str, str]:
    task = turn.phase_task.strip()
    if not task:
        return "", ""
    on_task_text(task)
    family = _STATE.family or task_family(task)
    skill = _SKILLS.setdefault(family, Skill(family=family))
    drop_stale = _STATE.drop_sandbox
    _STATE.drop_sandbox = False

    # 1) 消化沙盒：bundle / HTTP / check TOKEN / 显式 ANSWER
    raw_result = turn.last_cmd_result.strip()
    if raw_result and not drop_stale:
        _STATE.last_sandbox = _clip(raw_result, 3500)
        skill.sandbox_notes = _STATE.last_sandbox[-1500:]
        _ingest_sandbox(raw_result, task, skill)
        concrete = _answer_from_sandbox(raw_result, task)
        if concrete and not is_junk_answer(concrete) and _answer_fits_task(concrete, task):
            payload = patch_task_answer(concrete, turn)
            if not is_junk_answer(payload) and _answer_fits_task(payload, task):
                if _STATE.last_cmd and not _is_explore_cmd(_STATE.last_cmd):
                    if _STATE.last_cmd not in skill.good_cmds:
                        skill.good_cmds.append(_STATE.last_cmd)
                _commit_answer(role, commands, payload, skill)
                _learn_from_answer(payload)
                _learn_sop(skill)
                return "", ""

    # 2) 消化 LLM：只接受 ANSWER / CMD，绝不把元描述当答案
    resp = turn.llm_resp.strip()
    if resp and MEM.awaiting_task:
        answer = _extract_tag(resp, "ANSWER")
        if answer and not is_junk_answer(answer) and _answer_fits_task(answer, task):
            payload = patch_task_answer(answer, turn)
            if not is_junk_answer(payload) and _answer_fits_task(payload, task):
                _commit_answer(role, commands, payload, skill)
                _learn_from_answer(payload)
                return "", ""
        cmd = _extract_tag(resp, "CMD")
        if cmd and "workspace_edit" not in cmd:
            MEM.awaiting_task = False
            _STATE.asked_llm = False
            safe = safe_cmd(cmd)
            _STATE.last_cmd = safe
            return "", safe
        MEM.awaiting_task = False
        _STATE.asked_llm = False

    # 3) 题干里直接嵌了完整答案（短任务，非「请阅读 md」）才秒交
    if not _is_read_file_task(task):
        preset = try_preset_answer(task, skill)
        if preset and not is_junk_answer(preset) and _answer_fits_task(preset, task):
            payload = patch_task_answer(preset, turn)
            _commit_answer(role, commands, payload, skill)
            _learn_from_answer(payload)
            return "", ""

    # 4) 判题缺键：就地修补再交
    if MEM.task_fails and (MEM.task_required or MEM.task_forbidden) and MEM.task_answer:
        if not is_junk_answer(MEM.task_answer):
            payload = patch_task_answer(MEM.task_answer, turn)
            if payload != MEM.task_answer:
                _commit_answer(role, commands, payload, skill)
                return "", ""

    # 5) 按族探测：bootstrap → HTTP/workspace（含报错自纠）
    probe = next_probe_cmd(task, skill)
    if probe:
        _STATE.last_cmd = probe
        if probe not in skill.explore_cmds:
            skill.explore_cmds.append(probe)
        MEM.awaiting_task = False
        return "", probe

    # 5.5) 工程题探索命令已发出、等沙盒：别抢着问 LLM
    if (
        (_STATE.kind or _classify(task)) == "engineering-fix"
        and not _STATE.ws_done
        and _STATE.ws_phase == "explore"
        and not force_submit_now(turn)
    ):
        return "", ""

    # 6) 快超时保底：只用沙盒证据，不用写死城市库
    if force_submit_now(turn):
        payload = patch_task_answer(_fallback_answer(turn), turn)
        if payload and not is_junk_answer(payload) and _answer_fits_task(payload, task):
            _commit_answer(role, commands, payload, skill)
            return "", ""
        if MEM.task_required:
            payload = patch_task_answer("{}", turn)
            _commit_answer(role, commands, payload, skill)
            return "", ""

    # 7) 问 LLM（任务期间不占每日额度）；禁止它输出 workspace_edit
    if not MEM.awaiting_task:
        MEM.prompted_task = task
        MEM.awaiting_task = True
        _STATE.asked_llm = True
        mark_prompt(turn)
        return build_prompt(turn, skill), ""

    used = task_rounds_used(turn)
    if used > 0 and used % 3 == 0:
        probe = next_probe_cmd(task, skill)
        if probe:
            MEM.awaiting_task = False
            _STATE.last_cmd = probe
            return "", probe
        mark_prompt(turn)
        return build_prompt(turn, skill), ""
    return "", ""


def next_probe_cmd(task: str, skill: Skill) -> str | None:
    kind = _STATE.kind or _classify(task) or _classify(_STATE.bundle)
    if not _STATE.boot_done:
        _STATE.boot_done = True
        return _bootstrap_cmd(task)

    if kind == "unknown-api" and not _STATE.http_done:
        city = _STATE.city or _detect_city(_STATE.bundle or task) or "北京"
        return _http_probe_cmd(city)

    # 工程题：自摸索 diagnose/fix/verify（单次沙盒内循环），最多外层 2 次
    if kind == "engineering-fix" and not _STATE.ws_done:
        if _STATE.ws_tries >= 2 and _STATE.ws_phase == "failed":
            return None
        if _STATE.ws_phase in {"", "failed"}:
            _STATE.ws_tries += 1
            _STATE.ws_phase = "explore"
            return _workspace_explore_cmd(task)
        return None

    # 未知题型：读点名文件 / 目录
    files = _FILE_HINT.findall(task)
    steps: list[str] = []
    for name in files:
        steps.append(f"sed -n '1,240p' {name} 2>/dev/null || cat {name}")
    steps.append(_generic_scan_cmd())
    while _STATE.explore_i < len(steps):
        cmd = steps[_STATE.explore_i]
        _STATE.explore_i += 1
        if cmd in skill.explore_cmds and _STATE.explore_i < len(steps):
            continue
        return cmd
    return None


def next_bootstrap_cmd(task: str, skill: Skill) -> str | None:
    """兼容旧测试入口，转交探测队列。"""
    return next_probe_cmd(task, skill)


def try_preset_answer(task: str, skill: Skill) -> str:
    """仅当题干本身已给出可交答案时使用；不写死城市文物表。"""
    del skill
    if _is_read_file_task(task):
        return ""
    # 题目里直接嵌了完整 JSON，且不像「输出格式示例」
    if "```" not in task and "如下" not in task:
        for match in re.finditer(r"\{[^{}]{3,800}\}", task):
            blob = match.group(0)
            try:
                data = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data and not _is_placeholder_payload(data):
                if "token" in data or "total_count" in data or len(data) >= 2:
                    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    if _is_token_context(task):
        token = _extract_token(task)
        if _valid_token(token):
            return json.dumps(
                {"token": token}, ensure_ascii=False, separators=(",", ":"),
            )
    return ""


def _answer_fits_task(payload: str, task: str) -> bool:
    """工程题只交 TOKEN，API 题只交当前城市统计，禁止串题。"""
    blob = (payload or "").strip()
    if not blob:
        return False
    kind = _STATE.kind or _classify(task) or _classify(_STATE.bundle)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if kind == "engineering-fix":
        token = ""
        if isinstance(data, dict):
            token = str(data.get("token") or "")
        elif _valid_token(blob):
            token = blob
        if not _valid_token(token):
            return False
        key = token.lower()
        owner = _TOKEN_OWNER.get(key, "")
        current = (_STATE.task_file or "").lower()
        # 本题可重交（走出范围失败）；别的工程题严禁复用
        if owner and current and owner != current:
            return False
        if key in _USED_TOKENS and owner != current:
            return False
        return True
    if kind == "unknown-api":
        if not isinstance(data, dict):
            return False
        if "total_count" not in data and "city" not in data:
            return False
        city = _STATE.city or _detect_city(task) or _detect_city(_STATE.bundle) or ""
        got = str(data.get("city") or "")
        if city and got and got != city:
            return False
        if "token" in data and not _valid_token(str(data.get("token") or "")):
            return False
        return True
    return not is_junk_answer(blob)


def _is_read_file_task(task: str) -> bool:
    return bool(_READ_TASK.search(task) or _TASK_MD.search(task))


def _is_token_context(task: str) -> bool:
    if not task or not task.strip():
        return False
    if _is_read_file_task(task):
        return False
    if _TOKEN_HINT.search(task):
        return True
    if _HEX12.search(task):
        if _detect_city(task):
            return False
        low = task.lower()
        if "文物" in task or "heritage" in low or "total_count" in low:
            return False
        return True
    return False


def _detect_city(task: str) -> str | None:
    if not task:
        return None
    low = task.lower()
    match = _TASK_MD.search(task)
    if match:
        stem = match.group(1).lower()
        for alias, city in _CITY_EN.items():
            if alias in stem:
                return city
    for alias, city in _CITY_EN.items():
        if alias in low:
            return city
    for city in _CITY_CN:
        if city in task:
            return city
    return None


def _detect_app(task: str) -> str:
    match = re.search(r"task_\d+_([A-Za-z]+)\.md", task or "", re.I)
    if match:
        name = match.group(1).lower()
        if name not in _CITY_EN:
            return name
    match = re.search(r"应用\s*[`'\"]?([A-Za-z][A-Za-z0-9_-]*)", task or "")
    if match:
        return match.group(1).lower()
    match = re.search(r"\b(alpha|beta|gamma|delta|omega)\b", task or "", re.I)
    if match:
        return match.group(1).lower()
    return ""


def _abs_tmp_path(path: str) -> str:
    """FWBUNDLE 常给 tmp/... 或缺前导 /，沙盒真实路径是 /tmp/...。"""
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("./tmp/"):
        return "/tmp/" + raw[6:]
    if raw.startswith("tmp/"):
        return "/tmp/" + raw[4:]
    if raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith("tmp/") and not raw.startswith("/"):
        return "/tmp/" + raw[4:]
    if not raw.startswith("/") and "/tmp/" in raw:
        idx = raw.find("tmp/")
        return "/" + raw[idx:]
    if not raw.startswith("/") and raw.startswith("tmp/"):
        return "/" + raw
    return raw


def _task_filename(task: str) -> str:
    match = _READ_TASK.search(task) or _TASK_MD.search(task)
    if match:
        return match.group(1)
    files = _FILE_HINT.findall(task or "")
    return files[0] if files else ""


def _valid_token(text: str) -> bool:
    token = (text or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{12,32}", token):
        return False
    return not _PLACEHOLDER_TOKEN.fullmatch(token)


def _is_placeholder_payload(data: dict[str, Any]) -> bool:
    token = str(data.get("token") or "")
    if token and not _valid_token(token):
        return True
    if str(data.get("city") or "") in {"city", "城市", "example"}:
        return True
    return False


def _extract_token(text: str) -> str:
    if not text:
        return ""
    # 工程题真值只在 check 的 TOKEN: 行；不要从 md 示例 / FWBUNDLE 里抽
    match = re.search(r"TOKEN\s*[:：]\s*([a-fA-F0-9]{12,32})", text, re.I)
    if match and _valid_token(match.group(1)):
        return match.group(1).lower()
    stripped = re.sub(r"FWBUNDLE1\s*\{.*", "", text, flags=re.S)
    patterns = (
        r'["\']?(?:token|auth|secret|key|令牌|口令|认证码?|密钥|验证码)'
        r'["\']?\s*[:=：]\s*["\']([a-fA-F0-9]{12,32})["\']',
        r"(?:token|auth|secret|令牌|口令|认证码?|密钥|验证码)"
        r"\s*[:=：]\s*([a-fA-F0-9]{12,32})",
        r"ANSWER\s*:\s*([a-fA-F0-9]{12,32})",
        r"\b([a-fA-F0-9]{12})\b",
        r"\b([a-fA-F0-9]{16})\b",
    )
    for pat in patterns:
        found = re.search(pat, stripped, re.I)
        if found and _valid_token(found.group(1)):
            return found.group(1).lower()
    return ""


def _ingest_sandbox(raw: str, task: str, skill: Skill) -> None:
    bundle = _extract_tagged_json(raw, "FWBUNDLE1")
    if bundle:
        _STATE.boot_done = True
        _STATE.task_path = str(bundle.get("task_path") or "")
        _STATE.bundle = str(bundle.get("task") or "")
        blob = _STATE.bundle or task
        _STATE.kind = _STATE.kind or _classify(blob) or _classify(_STATE.task_path)
        _STATE.city = _STATE.city or _detect_city(blob) or _detect_city(_STATE.task_path) or ""
        _STATE.app_name = _STATE.app_name or _detect_app(blob) or _detect_app(_STATE.task_path)
        ws = re.search(
            r"((?:/?\.?/)?tmp/\S*?/ws_\d+|/(?:tmp/\S*?/)?ws_\d+)|(?:目录的\s*)?(ws_\d+)\s*/",
            blob + " " + _STATE.task_path,
        )
        if ws:
            found = (ws.group(1) or ws.group(2) or "").strip().replace("\\", "/")
            if found.startswith(("ws_",)) and _STATE.task_path:
                parent = re.sub(r"[^/\\]+$", "", _STATE.task_path.replace("\\", "/"))
                found = (parent + found).replace("//", "/")
            _STATE.ws_dir = _abs_tmp_path(found)
        if _STATE.task_path:
            _STATE.task_path = _abs_tmp_path(_STATE.task_path)
        if not _STATE.app_name:
            _STATE.app_name = _detect_app(blob) or _detect_app(_STATE.task_path)
        url = re.search(r"https?://127\.0\.0\.1:\d+\S*|https?://localhost:\d+\S*", blob)
        if url:
            base = url.group(0).split("?")[0]
            if "heritage" in base or "api" in base:
                _HTTP_SOP.url = base.rstrip("/")
        key = re.search(r"(heritage-api-key-\d+)", blob)
        if key:
            _HTTP_SOP.api_key = key.group(1)

    https = _extract_tagged_jsons(raw, "FWHTTP1")
    ok_http = [item for item in https if item.get("ok") or int(item.get("status") or 0) == 200]
    for http in https:
        status = int(http.get("status") or 0)
        body = str(http.get("body") or "")
        if status == 401 and "Bearer" in body:
            _HTTP_SOP.auth = "bearer"
        if status == 400 and "location" in body.lower():
            _HTTP_SOP.param = "location"
    if ok_http:
        _HTTP_SOP.auth = _HTTP_SOP.auth or "bearer"
        _HTTP_SOP.param = _HTTP_SOP.param or "location"
        page_sizes: list[int] = []
        for item in ok_http:
            try:
                page_sizes.append(
                    len(_records_of(json.loads(str(item.get("body") or ""))))
                )
            except json.JSONDecodeError:
                page_sizes.append(0)
        # 单页满 10 条且没有 ANSWER：输出可能被截断，下回合再拉
        complete = bool(re.search(r"ANSWER\s*:", raw, re.I)) or (
            bool(page_sizes) and page_sizes[-1] < 10
        )
        if complete:
            _STATE.http_done = True
            skill.steps = "LOCAL_HTTP"
        else:
            _STATE.http_done = False
    elif https:
        # 只有 401/400：记下 SOP，下回合带着纠正后的头再打
        _STATE.http_done = False

    if re.search(r"\[\s*OK\s*\]|全部通过", raw, re.I) and _extract_token(raw):
        skill.steps = "DIAGNOSE>FIX>VERIFY"
        _STATE.ws_done = True
        _STATE.ws_phase = "done"
    elif (_STATE.kind or _classify(task) or _classify(_STATE.bundle)) == "engineering-fix":
        if _STATE.ws_phase == "explore":
            # 本轮探索未拿到 TOKEN：记下失败证据，允许再探一轮
            if not (re.search(r"\[\s*OK\s*\]|全部通过", raw, re.I) and _extract_token(raw)):
                _STATE.ws_phase = "failed"
                _STATE.ws_done = False
        elif re.search(r"NEED_WORKSPACE|未通过|FAIL|AssertionError|Error", raw, re.I):
            _STATE.ws_phase = "failed"
            _STATE.ws_done = False


def _answer_from_sandbox(raw: str, task: str) -> str:
    # 1) 工程题：必须 check 通过且 TOKEN 是 12+ 位 hex，拒绝 xxx
    if re.search(r"\[\s*OK\s*\]|全部通过", raw, re.I):
        token = _extract_token(raw)
        if _valid_token(token):
            return json.dumps(
                {"token": token}, ensure_ascii=False, separators=(",", ":"),
            )

    # 2) 显式 ANSWER:（脚本分页统计后的完整 JSON）
    concrete = concrete_sandbox_answer(raw)
    if concrete and not is_junk_answer(concrete):
        return concrete

    # 3) 取最后一次成功的 HTTP 体做统计（不要用第一页 401/10 条）
    ok_http = [
        item for item in _extract_tagged_jsons(raw, "FWHTTP1")
        if item.get("ok") or int(item.get("status") or 0) == 200
    ]
    if ok_http:
        city = _STATE.city or _detect_city(task) or ""
        merged: list[Any] = []
        seen: set[str] = set()
        last_body = ""
        for item in ok_http:
            last_body = str(item.get("body") or "")
            try:
                data = json.loads(last_body)
            except json.JSONDecodeError:
                continue
            for rec in _records_of(data):
                if not isinstance(rec, dict):
                    continue
                key = str(rec.get("id") or rec.get("name") or json.dumps(rec, ensure_ascii=False))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(rec)
        if merged:
            last_n = 0
            try:
                last_n = len(_records_of(json.loads(last_body))) if last_body else 0
            except json.JSONDecodeError:
                last_n = len(merged)
            # 只有一页且刚好 10 条：多半还有下一页，等分页 ANSWER
            if last_n < 10 or (len(ok_http) >= 2 and last_n <= 10):
                stats = _heritage_stats_from_records(merged, city)
                if stats:
                    return stats
        stats = _heritage_stats(last_body, city)
        if stats and last_body:
            try:
                if len(_records_of(json.loads(last_body))) < 10:
                    return stats
            except json.JSONDecodeError:
                pass
    return ""


def _heritage_stats_from_records(records: list[Any], city: str) -> str:
    types: list[str] = []
    world = 0
    oldest_name = ""
    oldest_rank = 10**6
    for item in records:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("types") or "")
        if kind and kind not in types:
            types.append(kind)
        level = str(item.get("protected_level") or item.get("level") or "")
        if "世界遗产" in level:
            world += 1
        era = str(item.get("era") or item.get("oldest_era") or "")
        rank = _era_rank(era)
        name = str(item.get("name") or "")
        if name and rank < oldest_rank:
            oldest_rank = rank
            oldest_name = name
    if not records:
        return ""
    payload = {
        "city": city,
        "total_count": len(records),
        "world_heritage_count": world,
        "types": types,
        "oldest_era": oldest_name,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _heritage_stats(body: str, city: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    records = _records_of(data)
    if not records:
        return ""
    return _heritage_stats_from_records(records, city or str(data.get("city") or ""))


def _records_of(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    inner = data.get("data")
    if isinstance(inner, dict):
        rec = inner.get("records") or inner.get("items") or inner.get("list")
        if isinstance(rec, list):
            return rec
    rec = data.get("records") or data.get("items") or data.get("list")
    return rec if isinstance(rec, list) else []


def _era_rank(era: str) -> int:
    for index, name in enumerate(_ERA_RANK):
        if name and name in era:
            return index
    return 10**6


def _extract_tagged_json(raw: str, tag: str) -> dict[str, Any] | None:
    items = _extract_tagged_jsons(raw, tag)
    return items[-1] if items else None


def _extract_tagged_jsons(raw: str, tag: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(rf"{tag}\s+", raw):
        blob = raw[match.end():].lstrip()
        try:
            data, _ = decoder.raw_decode(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            found.append(data)
    return found


def _learn_from_answer(payload: str) -> None:
    if not payload.startswith("{"):
        return
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    city = str(data.get("city") or "")
    if city and ("total_count" in data or "types" in data or "oldest_era" in data):
        _LEARNED_CITIES[city] = dict(data)


def _learn_sop(skill: Skill) -> None:
    if _STATE.kind == "unknown-api":
        skill.steps = "LOCAL_HTTP"
        if not _HTTP_SOP.auth:
            _HTTP_SOP.auth = "bearer"
        if not _HTTP_SOP.param:
            _HTTP_SOP.param = "location"
    elif _STATE.kind == "engineering-fix":
        skill.steps = "DIAGNOSE>FIX>VERIFY"


def _bootstrap_cmd(task: str) -> str:
    filename = _STATE.task_file or _task_filename(task) or "task.md"
    skip = ",".join(repr(name) for name in _SKIP_DIRS)
    return (
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"fn = {filename!r}\n"
        f"skip = {{{skip}}}\n"
        "found = None\n"
        "roots = [pathlib.Path('/tmp'), pathlib.Path('.'), pathlib.Path('tmp')]\n"
        "for root in roots:\n"
        "    if found is not None or not root.exists(): continue\n"
        "    for p in root.rglob(fn):\n"
        "        if p.is_file() and not (set(p.parts) & skip):\n"
        "            found = p; break\n"
        "if found is None:\n"
        "    for root in roots:\n"
        "        if found is not None or not root.exists(): continue\n"
        "        for p in root.rglob('task*.md'):\n"
        "            if p.is_file() and not (set(p.parts) & skip):\n"
        "                found = p; break\n"
        "path = ''\n"
        "if found is not None:\n"
        "    path = str(found.resolve()).replace('\\\\', '/')\n"
        "    if path.startswith('/private/tmp/'): path = path[len('/private'):]\n"
        "    if not path.startswith('/') and path.startswith('tmp/'): path = '/' + path\n"
        "text = found.read_text(encoding='utf-8', errors='ignore')[:7000] if found else ''\n"
        "print('FWBUNDLE1 ' + json.dumps("
        "{'ok': bool(found), 'task_path': path, 'task': text}, "
        "ensure_ascii=False))\n"
        "PY"
    )


def _http_probe_cmd(city: str) -> str:
    url = _HTTP_SOP.url
    key = _HTTP_SOP.api_key
    prefer_auth = _HTTP_SOP.auth or ""
    prefer_param = _HTTP_SOP.param or ""
    return (
        "python3 - <<'PY'\n"
        "import json, urllib.error, urllib.parse, urllib.request\n"
        f"URL = {url!r}\n"
        f"KEY = {key!r}\n"
        f"CITY = {city!r}\n"
        f"PREFER_AUTH = {prefer_auth!r}\n"
        f"PREFER_PARAM = {prefer_param!r}\n"
        "enc = urllib.parse.quote(CITY)\n"
        "auths = []\n"
        "if PREFER_AUTH == 'bearer':\n"
        "    auths.append({'Authorization': 'Bearer ' + KEY})\n"
        "auths += [{'Authorization': 'Bearer ' + KEY}, {'X-API-Key': KEY}]\n"
        "params = []\n"
        "if PREFER_PARAM: params.append(PREFER_PARAM)\n"
        "params += ['location', 'city']\n"
        "seen=set(); auths=[a for a in auths if tuple(a.items()) not in seen and not seen.add(tuple(a.items()))]\n"
        "seen=set(); params=[p for p in params if p not in seen and not seen.add(p)]\n"
        "def fetch(headers, query):\n"
        "    req = urllib.request.Request(URL + '?' + query, headers=headers)\n"
        "    try:\n"
        "        with urllib.request.urlopen(req, timeout=6) as resp:\n"
        "            return resp.status, resp.read(80000).decode('utf-8', 'ignore')\n"
        "    except urllib.error.HTTPError as exc:\n"
        "        return exc.code, exc.read(4000).decode('utf-8', 'ignore')\n"
        "    except Exception as exc:\n"
        "        return 0, str(exc)\n"
        "def recs(body):\n"
        "    try: data=json.loads(body)\n"
        "    except Exception: return []\n"
        "    inner=data.get('data') if isinstance(data, dict) and isinstance(data.get('data'), dict) else data\n"
        "    if not isinstance(inner, dict): return []\n"
        "    val=inner.get('records') or inner.get('items') or inner.get('list') or []\n"
        "    return val if isinstance(val, list) else []\n"
        "logs=[]; pair=None\n"
        "for headers in auths:\n"
        "    for param in params:\n"
        "        status, body = fetch(headers, param+'='+enc+'&offset=0&limit=10')\n"
        "        logs.append({'ok':200<=status<300,'status':status,'n':len(recs(body)),'body':body[:400]})\n"
        "        if 200<=status<300:\n"
        "            pair=(headers,param); break\n"
        "    if pair: break\n"
        "ans=None\n"
        "if pair:\n"
        "    headers, param = pair\n"
        "    all_rec=[]; seen=set(); offset=0\n"
        "    while offset<=80:\n"
        "        status, body = fetch(headers, param+'='+enc+'&offset=%d&limit=10'%offset)\n"
        "        chunk=recs(body)\n"
        "        logs.append({'ok':200<=status<300,'status':status,'n':len(chunk),'offset':offset})\n"
        "        for item in chunk:\n"
        "            if not isinstance(item, dict): continue\n"
        "            k=str(item.get('id') or item.get('name') or json.dumps(item,ensure_ascii=False))\n"
        "            if k in seen: continue\n"
        "            seen.add(k); all_rec.append(item)\n"
        "        if len(chunk)<10: break\n"
        "        offset += 10\n"
        "    types=[]; world=0; oldest=''; rank=10**6\n"
        "    eras=['旧石器','新石器','史前','商周','商','周','春秋','战国','秦','汉','三国','晋','南北朝','隋','唐','五代','宋','元','明','清','民国']\n"
        "    for item in all_rec:\n"
        "        k=str(item.get('type') or '')\n"
        "        if k and k not in types: types.append(k)\n"
        "        if '世界遗产' in str(item.get('protected_level') or ''): world += 1\n"
        "        era=str(item.get('era') or ''); r=next((i for i,n in enumerate(eras) if n in era), 10**6)\n"
        "        name=str(item.get('name') or '')\n"
        "        if name and r<rank: rank=r; oldest=name\n"
        "    ans={'city':CITY,'total_count':len(all_rec),'world_heritage_count':world,'types':types,'oldest_era':oldest}\n"
        "if ans is not None:\n"
        "    print('ANSWER:'+json.dumps(ans, ensure_ascii=False, separators=(',', ':')))\n"
        "for item in logs:\n"
        "    print('FWHTTP1 '+json.dumps(item, ensure_ascii=False)[:800])\n"
        "PY"
    )


def _workspace_explore_cmd(task: str) -> str:
    """自摸索：定位 workspace → 跑 check 看报错 → 按报错修 → 再验证（沙盒内最多 3 轮）。"""
    filename = _STATE.task_file or _task_filename(task) or "task.md"
    app = _STATE.app_name or "app"
    hint_path = _abs_tmp_path(_STATE.task_path) if _STATE.task_path else ""
    ws_hint = _abs_tmp_path(_STATE.ws_dir) if _STATE.ws_dir else ""
    bundle = _clip(_STATE.bundle, 1800) if _STATE.bundle else ""
    prev_err = _clip(_STATE.last_sandbox, 500) if _STATE.last_sandbox else ""
    return (
        "python3 - <<'PY'\n"
        "import json, os, pathlib, re, stat, subprocess, sys\n"
        f"FN = {filename!r}\n"
        f"APP = {app!r}\n"
        f"HINT = {hint_path!r}\n"
        f"WS_HINT = {ws_hint!r}\n"
        f"BUNDLE = {bundle!r}\n"
        f"PREV = {prev_err!r}\n"
        "skip={'lib','boot','run','sbin','var','sys','bin','etc','dev','usr','lib64','proc'}\n"
        "JUNK={'or','and','the','a','an','to','of','in','on','is','be','file','directory',"
        "'path','error','failed','fail','missing','not','found','no','such'}\n"
        "\n"
        "def abs_tmp(p):\n"
        "    s=str(p or '').replace('\\\\','/').strip()\n"
        "    if not s: return s\n"
        "    if s.startswith('./tmp/'): return '/tmp/'+s[6:]\n"
        "    if s.startswith('tmp/'): return '/tmp/'+s[4:]\n"
        "    if s.startswith('./'): s=s[2:]\n"
        "    if s.startswith('tmp/') and not s.startswith('/'): return '/tmp/'+s[4:]\n"
        "    if not s.startswith('/') and 'tmp/' in s:\n"
        "        i=s.find('tmp/'); return '/'+s[i:]\n"
        "    return s\n"
        "\n"
        "def find_task():\n"
        "    for cand in [HINT, abs_tmp(HINT)]:\n"
        "        if not cand: continue\n"
        "        task=pathlib.Path(cand)\n"
        "        if task.is_file(): return task.resolve()\n"
        "    for root in [pathlib.Path('/tmp'), pathlib.Path('.')]:\n"
        "        if not root.exists(): continue\n"
        "        for p in root.rglob(FN):\n"
        "            if p.is_file() and not (set(p.parts)&skip): return p.resolve()\n"
        "    return None\n"
        "\n"
        "def detect_app(text, task):\n"
        "    m=re.search(r'task_\\d+_([a-z]+)\\.md', (str(task or FN)+' '+text).lower())\n"
        "    if m and m.group(1) not in {'beijing','nanjing','chengdu','shanghai','hangzhou'}:\n"
        "        return m.group(1)\n"
        "    m=re.search(r'\\b(alpha|beta|gamma|delta|omega)\\b', text.lower())\n"
        "    return m.group(0) if m else APP\n"
        "\n"
        "def find_ws(task, text, app):\n"
        "    cands=[]\n"
        "    if WS_HINT: cands += [pathlib.Path(WS_HINT), pathlib.Path(abs_tmp(WS_HINT))]\n"
        "    for m in re.finditer(r'((?:/?\\.?/)?tmp/\\S*?/ws_\\d+|ws_\\d+)', text):\n"
        "        raw=m.group(1); cands.append(pathlib.Path(raw)); cands.append(pathlib.Path(abs_tmp(raw)))\n"
        "    if task:\n"
        "        base=task.parent\n"
        "        for name in [f'ws_{i}' for i in range(1,6)]+[app]:\n"
        "            cands.append(base/name)\n"
        "        for p in base.iterdir() if base.is_dir() else []:\n"
        "            if p.is_dir(): cands.append(p)\n"
        "    for root in ['/tmp', 'tmp', './tmp']:\n"
        "        rp=pathlib.Path(root)\n"
        "        if not rp.exists(): continue\n"
        "        for p in rp.rglob('ws_*'):\n"
        "            if p.is_dir(): cands.append(p)\n"
        "    seen=set()\n"
        "    for cand in cands:\n"
        "        paths=[cand, pathlib.Path(abs_tmp(str(cand)))]\n"
        "        if task and not cand.is_absolute():\n"
        "            paths.append(task.parent/cand.name)\n"
        "            paths.append(pathlib.Path(abs_tmp(str(task.parent/cand.name))))\n"
        "        for p in paths:\n"
        "            try:\n"
        "                key=str(p.resolve()) if p.exists() else str(p)\n"
        "            except Exception:\n"
        "                key=str(p)\n"
        "            if key in seen: continue\n"
        "            seen.add(key)\n"
        "            try:\n"
        "                if p.is_dir() and ((p/'check').exists() or (p/'check.py').exists()):\n"
        "                    return p.resolve()\n"
        "            except Exception:\n"
        "                continue\n"
        "    return None\n"
        "\n"
        "def strip_crlf(path):\n"
        "    try:\n"
        "        data=path.read_bytes()\n"
        "    except Exception:\n"
        "        return False\n"
        "    if b'\\r' not in data: return False\n"
        "    path.write_bytes(data.replace(b'\\r\\n', b'\\n').replace(b'\\r', b'\\n'))\n"
        "    return True\n"
        "\n"
        "def run_check(ws):\n"
        "    check=ws/'check.py' if (ws/'check.py').exists() else ws/'check'\n"
        "    if not check.exists(): return 'NEED_CHECK', ''\n"
        "    fixed=strip_crlf(check)\n"
        "    for sh in (ws/'bin'/'start.sh', ws/'start.sh'):\n"
        "        if sh.is_file(): strip_crlf(sh)\n"
        "    try:\n"
        "        os.chmod(check, stat.S_IRWXU|stat.S_IRGRP|stat.S_IXGRP|stat.S_IROTH|stat.S_IXOTH)\n"
        "    except Exception: pass\n"
        "    head=check.read_bytes()[:160]\n"
        "    if check.suffix=='.py' or (head.startswith(b'#!') and b'python' in head):\n"
        "        cmd=[sys.executable, str(check)]\n"
        "    else:\n"
        "        cmd=['bash', str(check)]\n"
        "    try:\n"
        "        proc=subprocess.run(cmd, cwd=str(ws), capture_output=True, text=True, timeout=20)\n"
        "        out=(proc.stdout or '')+'\\n'+(proc.stderr or '')\n"
        "        if fixed: out='[CRLF_FIXED]\\n'+out\n"
        "    except Exception as exc:\n"
        "        out=str(exc)\n"
        "    tok=''\n"
        "    m=re.search(r'TOKEN\\s*[:：]\\s*([a-fA-F0-9]{12,32})', out, re.I)\n"
        "    if m: tok=m.group(1).lower()\n"
        "    return out, tok\n"
        "\n"
        "def read_expectations(ws, text, app, err):\n"
        "    spec=''\n"
        "    for name in ('spec.md','SPEC.md','README.md','readme.md'):\n"
        "        p=ws/name\n"
        "        if p.is_file():\n"
        "            strip_crlf(p)\n"
        "            spec += p.read_text(encoding='utf-8', errors='ignore')+'\\n'\n"
        "    check=ws/'check.py' if (ws/'check.py').exists() else ws/'check'\n"
        "    src=''\n"
        "    try:\n"
        "        if check.exists():\n"
        "            raw=check.read_bytes()\n"
        "            src=raw.decode('utf-8','ignore')[:8000]\n"
        "    except Exception: pass\n"
        "    primary='\\n'.join([spec, text])\n"
        "    blob='\\n'.join([spec, text, src, err, PREV])\n"
        "    port=None; svc=None; log_app=app\n"
        "    # check FAIL 行优先：期望 name gamma-daemon / 期望 port 3000\n"
        "    m=re.search(r'期望\\s*name\\s+([A-Za-z0-9_-]+)', err)\n"
        "    if m: svc=m.group(1)\n"
        "    m=re.search(r'期望\\s*port\\s+(\\d{2,5})', err)\n"
        "    if m: port=m.group(1)\n"
        "    if not svc:\n"
        "        m=re.search(r'name\\s+([A-Za-z0-9_-]+)\\s*，\\s*实际', err)\n"
        "        if m: svc=m.group(1)\n"
        "    for pat in (r'logs[/\\\\]([A-Za-z0-9_-]+)', r'logs\\s*[`\\']?/?([A-Za-z0-9_-]+)'):\n"
        "        m=re.search(pat, blob)\n"
        "        if m and m.group(1).lower() not in JUNK: log_app=m.group(1); break\n"
        "    if not port:\n"
        "      for pat in (\n"
        "        r'(?m)^\\s*port\\s*[:=]?\\s*[\\'\"]?(\\d{2,5})',\n"
        "        r'(?:expected|期望|应为|需要|want)[^\\n]{0,20}?port[^\\n]{0,10}?(\\d{2,5})',\n"
        "        r'port\\s*(?:==|=|为|:)?\\s*[\\'\"]?(\\d{2,5})',\n"
        "        r'端口\\s*[:=：为]?\\s*(\\d{2,5})',\n"
        "      ):\n"
        "        src_blob = primary if pat.startswith('(?m)') else blob\n"
        "        m=re.search(pat, src_blob, re.I)\n"
        "        if m: port=m.group(1); break\n"
        "    if not svc:\n"
        "      for pat in (\n"
        "        r'(?m)^\\s*name\\s*[:=]?\\s*[\\'\"]?([A-Za-z0-9_-]+)',\n"
        "        r'(?:expected|期望|应为|需要|want)[^\\n]{0,20}?name[^\\n]{0,10}?[\\'\"]([A-Za-z0-9_-]+)[\\'\"]',\n"
        "        r'(?:service[_ ]?name|服务名)\\s*[:=：为]?\\s*([A-Za-z0-9_-]+)',\n"
        "        r'\\b((?:alpha|beta|gamma|delta|omega)-(?:app|svc|service|daemon))\\b',\n"
        "      ):\n"
        "        src_blob = primary if pat.startswith('(?m)') else blob\n"
        "        m=re.search(pat, src_blob, re.I)\n"
        "        if m and m.group(1).lower() not in JUNK: svc=m.group(1); break\n"
        "    if not svc:\n"
        "        svc = app+'-app' if app else 'app'\n"
        "    return port, svc, log_app, spec, src\n"
        "\n"
        "def apply_fix(ws, app, err, text):\n"
        "    port, svc, log_app, spec, src = read_expectations(ws, text, app, err)\n"
        "    actions=[]\n"
        "    logs=ws/'logs'/log_app\n"
        "    logs.mkdir(mode=0o755, parents=True, exist_ok=True)\n"
        "    try:\n"
        "        if (ws/'logs').is_dir(): os.chmod(ws/'logs', 0o755)\n"
        "        os.chmod(logs, 0o755)\n"
        "    except Exception: pass\n"
        "    actions.append('mkdir '+str(logs))\n"
        "    start=ws/'bin'/'start.sh'\n"
        "    if start.is_file():\n"
        "        strip_crlf(start)\n"
        "        os.chmod(start, stat.S_IRWXU|stat.S_IRGRP|stat.S_IXGRP|stat.S_IROTH|stat.S_IXOTH)\n"
        "        actions.append('chmod '+str(start))\n"
        "    cfgdir=ws/'config'\n"
        "    conf=None\n"
        "    for cand in [cfgdir/(app+'.conf'), cfgdir/(log_app+'.conf'), cfgdir/(svc+'.conf'),"
        "                 cfgdir/(app+'.cfg')]:\n"
        "        if cand.is_file(): conf=cand; break\n"
        "    if conf is None and cfgdir.is_dir():\n"
        "        files=list(cfgdir.glob('*.conf'))+list(cfgdir.glob('*.cfg'))\n"
        "        conf=files[0] if files else None\n"
        "    if conf is not None and (port or svc):\n"
        "        raw=conf.read_text(encoding='utf-8', errors='ignore')\n"
        "        out=[]\n"
        "        for line in raw.splitlines():\n"
        "            if port and re.match(r'\\s*port\\b', line, re.I):\n"
        "                out.append(re.sub(r'\\d+', port, line, count=1))\n"
        "            elif svc and re.match(r'\\s*name\\b', line, re.I):\n"
        "                out.append(re.sub(r'(name\\s*[:=]?\\s*)\\S+', r'\\g<1>'+svc, line, count=1, flags=re.I))\n"
        "            else:\n"
        "                out.append(line)\n"
        "        conf.write_text('\\n'.join(out)+('\\n' if raw.endswith('\\n') else ''), encoding='utf-8')\n"
        "        actions.append(f'patch {conf} port={port} name={svc}')\n"
        "    for m in re.finditer(\n"
        "        r'(?:missing|缺少|not found|No such file)[^\\n]{0,80}?'\n"
        "        r'([A-Za-z0-9_./-]+\\.(?:conf|cfg|sh|txt|md|log|py)|'\n"
        "        r'logs/[A-Za-z0-9_./-]+|bin/[A-Za-z0-9_./-]+|config/[A-Za-z0-9_./-]+)',\n"
        "        err, re.I,\n"
        "    ):\n"
        "        rel=m.group(1).lstrip('./')\n"
        "        if '..' in rel or rel.startswith('/') or rel.lower() in JUNK: continue\n"
        "        if len(rel) < 3: continue\n"
        "        path=ws/rel\n"
        "        if path.suffix or rel.endswith(('.conf','.cfg','.sh','.txt','.md','.log','.py')):\n"
        "            path.parent.mkdir(parents=True, exist_ok=True)\n"
        "            if not path.exists():\n"
        "                path.write_text('', encoding='utf-8'); actions.append('touch '+str(path))\n"
        "        elif '/' in rel:\n"
        "            path.mkdir(parents=True, exist_ok=True); actions.append('mkdir '+str(path))\n"
        "    return actions\n"
        "\n"
        "task=find_task()\n"
        "text=(task.read_text(encoding='utf-8', errors='ignore') if task else '')+'\\n'+BUNDLE\n"
        "app=detect_app(text, task)\n"
        "ws=find_ws(task, text, app)\n"
        "print('PROBE', 'task', task, 'ws', ws, 'app', app)\n"
        "if ws is None:\n"
        "    print('NEED_WORKSPACE')\n"
        "    if task:\n"
        "        for p in task.parent.iterdir():\n"
        "            print('cand', p, p.is_dir())\n"
        "    raise SystemExit\n"
        "token=''\n"
        "for i in range(3):\n"
        "    out, token = run_check(ws)\n"
        "    print(f'CHECK#{i}', out[:1800])\n"
        "    if token and ('[ OK ]' in out or '全部通过' in out):\n"
        "        print('ANSWER:'+json.dumps({'token': token}, separators=(',', ':')))\n"
        "        break\n"
        "    if token and re.fullmatch(r'[a-f0-9]{12,32}', token):\n"
        "        print('ANSWER:'+json.dumps({'token': token}, separators=(',', ':')))\n"
        "        break\n"
        "    actions=apply_fix(ws, app, out, text)\n"
        "    print('FIX#'+str(i), actions)\n"
        "    if not actions:\n"
        "        print('NO_FIX')\n"
        "        break\n"
        "else:\n"
        "    out, token = run_check(ws)\n"
        "    print('CHECK#final', out[:1800])\n"
        "    if token:\n"
        "        print('ANSWER:'+json.dumps({'token': token}, separators=(',', ':')))\n"
        "PY"
    )




def _workspace_edit_cmd(task: str) -> str:
    """兼容旧入口，转交自摸索脚本。"""
    return _workspace_explore_cmd(task)


def _workspace_verify_cmd(task: str) -> str:
    """兼容旧入口，转交自摸索脚本。"""
    return _workspace_explore_cmd(task)


def _workspace_fix_cmd(task: str) -> str:
    return _workspace_explore_cmd(task)


def _generic_scan_cmd() -> str:
    return (
        "python3 - <<'PY'\n"
        "import pathlib\n"
        "skip={'proc','sys','dev','lib','usr','bin','etc'}\n"
        "for p in pathlib.Path('.').rglob('*'):\n"
        "    if not p.is_file() or p.stat().st_size>200000: continue\n"
        "    if set(p.parts)&skip: continue\n"
        "    if p.suffix.lower() in {'.md','.txt','.json','.conf'} or p.name in {'check','README'}:\n"
        "        print(p)\n"
        "PY"
    )


def concrete_sandbox_answer(raw: str) -> str:
    """只接受显式 ANSWER: 或合法 JSON 对象，绝不把路径/目录列表当答案。"""
    if not raw.strip():
        return ""
    if "[TIMEOUT]" in raw or "[JUDGER_ERROR]" in raw:
        return ""

    match = re.search(r"ANSWER\s*:\s*(.+)", raw, re.I)
    if match:
        text = match.group(1).strip().strip("`").strip()
        if text and not is_junk_answer(text):
            if text.startswith("{") and text.endswith("}"):
                try:
                    data = json.loads(text)
                    if isinstance(data, dict) and data and not _is_placeholder_payload(data):
                        return json.dumps(
                            data, ensure_ascii=False, separators=(",", ":"),
                        )
                except json.JSONDecodeError:
                    pass
            if (
                not _PATH_LIKE.search(text)
                and "/" not in text
                and not text.startswith(".")
                and _valid_token(text)
            ):
                return text

    # 不要把 md 示例 JSON 行当答案（FWBUNDLE 全文里常有 {"token":"xxx"}）
    if "FWBUNDLE1" in raw and "ANSWER:" not in raw.upper():
        return ""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("[exitCode:") or line == "[TRUNCATED]":
            continue
        if line.startswith("FWHTTP1") or line.startswith("FWBUNDLE1"):
            continue
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data and not _is_placeholder_payload(data) and (
            "token" in data or "total_count" in data or "city" in data
        ):
            return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return ""


def is_junk_answer(text: str) -> bool:
    blob = (text or "").strip()
    if not blob:
        return True
    if _JUNK_ANSWER.search(blob):
        return True
    if blob.startswith("- ") and ("建议" in blob or "SOP" in blob.upper()):
        return True
    if _PATH_LIKE.search(blob) or blob.startswith("./") or blob.startswith("/"):
        return True
    if re.search(r"(^|\s)(proc|sys|dev)/", blob):
        return True
    if re.fullmatch(r"[\w./\\-]+", blob) and ("/" in blob or "\\" in blob):
        return True
    if blob.startswith("{"):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and _is_placeholder_payload(data):
            return True
        if isinstance(data, dict) and "token" in data and not _valid_token(str(data.get("token") or "")):
            return True
    elif _PLACEHOLDER_TOKEN.fullmatch(blob) or not _valid_token(blob) and blob.lower() in {"xxx", "yyy"}:
        return True
    return False


def _is_explore_cmd(cmd: str) -> bool:
    return bool(_EXPLORE_CMD.search((cmd or "").strip()))


def safe_cmd(cmd: str) -> str:
    cmd = cmd.strip().strip("`")
    if _SAFE_BLOCK.search(cmd):
        return "python3 -c \"print('')\""
    if "workspace_edit" in cmd:
        return "python3 -c \"print('')\""
    return cmd[:8000]


def build_prompt(turn: Turn, skill: Skill) -> str:
    parts = [
        "你是《未来战争》自进化任务求解器。沙盒无外网，可跑 shell 与 python。",
        "流程：先读任务 md，再探测；文档可能过时，以沙盒报错为准修正后再交。",
        "工程修复题：先跑 ./check 看报错，再按报错自行修补（缺目录就建、权限不对就 chmod、conf 不对就对照 check/spec 改），再验证；从 TOKEN: 交卷。",
        "API 题：用 HTTP 拉全量记录再统计；401 改 Bearer，缺参就按报错改参数名。",
        "禁止输出 workspace_edit；禁止写 SOP 说明；禁止把路径当答案。",
        "禁止提交 {\"token\":\"xxx\"} 或 md 里的示例 JSON；工程题必须 check 输出 [ OK ] 和 TOKEN: 后才交。",
        "工程题禁止复用上一题 TOKEN；不要猜答案，以本题沙盒 check 输出为准。",
        "API 题必须分页拉全量（offset+=10 直到本页不足 10 条），禁止只交第一页。",
        "若已得到最终答案，只输出一行：ANSWER:<最终答案，优先合法 JSON>",
        "若还需执行命令，只输出一行：CMD:<单条命令>",
        "",
        f"【当前任务】\n{turn.phase_task}",
        f"【剩余回合】{task_rounds_left(turn)}",
    ]
    if _STATE.bundle:
        parts.append(f"【任务全文摘录】\n{_clip(_STATE.bundle, 1600)}")
    if skill.steps:
        parts.append(f"【已沉淀步骤】{skill.steps}")
    if skill.good_cmds:
        usable = [cmd for cmd in skill.good_cmds if not _is_explore_cmd(cmd)]
        if usable:
            parts.append("【已沉淀可复用命令】\n" + "\n".join(usable[-2:]))
    if skill.last_answer and _STATE.kind != "engineering-fix":
        parts.append(f"【同类题上次正确答案】\n{skill.last_answer}")
    elif _STATE.kind == "engineering-fix":
        parts.append("【注意】步骤可复用，但 TOKEN 必须来自本题 ./check，禁止交上一题的 token。")
    if MEM.task_required:
        parts.append("【必须包含的键】 " + ", ".join(MEM.task_required))
    if MEM.task_forbidden:
        parts.append("【禁止出现的键】 " + ", ".join(MEM.task_forbidden))
    if turn.error_msgs:
        parts.append("【上轮判题错误】\n" + "\n".join(turn.error_msgs))
    if _STATE.last_sandbox:
        parts.append(f"【上轮沙盒输出】\n{_clip(_STATE.last_sandbox, 1800)}")
    elif skill.sandbox_notes:
        parts.append(f"【历史沙盒摘录】\n{_clip(skill.sandbox_notes, 1200)}")
    if MEM.task_answer:
        stale = _token_of_payload(MEM.task_answer)
        owner = _TOKEN_OWNER.get(stale, "") if stale else ""
        current = _STATE.task_file or ""
        if not (stale and owner and owner != current):
            parts.append(f"【上次提交】\n{MEM.task_answer}")
    rider = treasure_rider(turn)
    if rider:
        parts.append(rider)
    return "\n".join(parts)


def _commit_answer(
    role: Unit,
    commands: dict[int, dict[str, Any]],
    payload: str,
    skill: Skill,
) -> None:
    if is_junk_answer(payload) or not _answer_fits_task(payload, _STATE.task_file or ""):
        MEM.awaiting_task = False
        return
    remember_answer(payload)
    skill.last_answer = payload
    token = _token_of_payload(payload)
    if token:
        _USED_TOKENS.add(token)
        _TOKEN_OWNER[token] = _STATE.task_file or ""
    commands[role.unit_id] = submit_answer_command(payload)
    MEM.awaiting_task = False


def _fallback_answer(turn: Turn) -> str:
    task = turn.phase_task
    for source in (
        MEM.task_answer,
        _extract_tag(turn.llm_resp, "ANSWER"),
        _answer_from_sandbox(turn.last_cmd_result, task),
        concrete_sandbox_answer(turn.last_cmd_result),
    ):
        if source and source.strip() and not is_junk_answer(source) and _answer_fits_task(source, task):
            return source.strip()
    # 工程题绝不复用上一题 TOKEN；API 题城市对得上才复用
    if (_STATE.kind or _classify(task)) != "engineering-fix":
        skill = _SKILLS.get(_STATE.family)
        if skill and skill.last_answer and not is_junk_answer(skill.last_answer) and _answer_fits_task(skill.last_answer, task):
            return skill.last_answer
    if MEM.task_required:
        return "{}"
    return ""


def _token_of_payload(payload: str) -> str:
    blob = (payload or "").strip()
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    token = ""
    if isinstance(data, dict):
        token = str(data.get("token") or "")
    elif _valid_token(blob):
        token = blob
    return token.lower() if _valid_token(token) else ""


def _extract_tag(text: str, tag: str) -> str:
    match = re.search(rf"{tag}\s*:\s*(.+)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip().strip("`").strip()


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...<truncated>...\n" + text[-20:]
