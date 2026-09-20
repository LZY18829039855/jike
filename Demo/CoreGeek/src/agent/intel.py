from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    COPPER,
    IRON,
    ITEM_ALIASES,
    PlayerTask,
    Pos,
    Turn,
    WALL_MATERIAL,
    distance,
)

LLM_DAILY_LIMIT = 3
DEFAULT_ORE_PRICE = {WALL_MATERIAL: 1, IRON: 3, COPPER: 5}

_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

_ORE_WORDS = {
    IRON: ("铁矿", "铁资源", "生铁", "铁矿区", "铁"),
    COPPER: ("铜矿", "铜资源", "黄铜", "铜矿区", "铜"),
    WALL_MATERIAL: ("石矿", "石头", "石料", "矿石", "石"),
}

_STOP_WORDS = (
    "停工", "停产", "塌方", "短缺", "禁采", "无法采集", "中断开采",
    "关闭", "封矿", "供不应求", "紧缺", "减产", "事故",
)
_SURPLUS_WORDS = ("丰收", "增产", "过剩", "供过于求", "大量开采", "库存积压")
_TODAY_WORDS = ("今天", "今日", "当天")
_TOMORROW_WORDS = ("明天", "明日", "次日")


@dataclass
class MarketEvent:
    ore: str
    kind: str
    start_day: int
    end_day: int
    source: str = ""


@dataclass
class TreasureGuess:
    pos: Pos | None = None
    items: list[str] = field(default_factory=list)
    day: int | None = None
    phase: str = "day"
    item_count: int | None = None
    region: str = ""
    weak: bool = False
    done: bool = False
    last_result: int = 0
    failed: set[tuple[Any, ...]] = field(default_factory=set)


@dataclass
class Memory:
    folk: list[str] = field(default_factory=list)
    official: list[tuple[int, str]] = field(default_factory=list)
    events: list[MarketEvent] = field(default_factory=list)
    treasure: TreasureGuess = field(default_factory=TreasureGuess)
    llm_day: int = 0
    llm_used: int = 0
    awaiting_task: bool = False
    awaiting_treasure: bool = False
    prompted_task: str = ""
    last_folk: str = ""
    last_official: str = ""
    task_fails: int = 0
    task_answer: str = ""
    task_required: list[str] = field(default_factory=list)
    task_forbidden: list[str] = field(default_factory=list)
    task_started_round: int = 0
    task_timeout: int = 0
    task_pos: Pos | None = None
    task_submits: int = 0
    pending_task_timeout: int = 0
    pending_task_pos: Pos | None = None
    pending_task_round: int = 0
    abandon_task: bool = False
    skip_task_until: int = 0
    last_move: dict[int, Pos] = field(default_factory=dict)
    last_build: dict[int, Pos] = field(default_factory=dict)
    failed_steps: dict[int, set[Pos]] = field(default_factory=dict)
    bad_build: set[Pos] = field(default_factory=set)
    build_failures: dict[Pos, int] = field(default_factory=dict)
    good_wall: set[Pos] = field(default_factory=set)
    good_weapon: set[Pos] = field(default_factory=set)
    threat_x: int = 0
    threat_y: int = 0
    threat_n: int = 0
    summon_day: int = 0
    summon_used: int = 0
    last_round: int = 0
    match_signature: tuple[Any, ...] = ()


MEM = Memory()


def reset_memory() -> None:
    MEM.folk.clear()
    MEM.official.clear()
    MEM.events.clear()
    MEM.treasure = TreasureGuess()
    MEM.llm_day = 0
    MEM.llm_used = 0
    MEM.awaiting_task = False
    MEM.awaiting_treasure = False
    MEM.prompted_task = ""
    MEM.last_folk = ""
    MEM.last_official = ""
    MEM.skip_task_until = 0
    MEM.last_move.clear()
    MEM.last_build.clear()
    MEM.failed_steps.clear()
    MEM.bad_build.clear()
    MEM.build_failures.clear()
    MEM.good_wall.clear()
    MEM.good_weapon.clear()
    MEM.threat_x = 0
    MEM.threat_y = 0
    MEM.threat_n = 0
    MEM.summon_day = 0
    MEM.summon_used = 0
    MEM.pending_task_timeout = 0
    MEM.pending_task_pos = None
    MEM.pending_task_round = 0
    MEM.last_round = 0
    MEM.match_signature = ()
    _clear_task()
    try:
        from .evolve import reset as reset_evolve

        reset_evolve()
    except Exception:
        pass


def observe(turn: Turn) -> None:
    _ensure_match(turn)

    if MEM.llm_day != turn.day_no:
        MEM.llm_day = turn.day_no
        MEM.llm_used = 0
        MEM.awaiting_treasure = False

    if MEM.summon_day != turn.day_no:
        MEM.summon_day = turn.day_no
        MEM.summon_used = 0

    _observe_threat(turn)

    folk = turn.folk_legends.strip()
    if folk and folk != MEM.last_folk:
        MEM.folk.append(folk)
        MEM.last_folk = folk
        if len(MEM.folk) > 14:
            del MEM.folk[:-14]
        _merge_treasure(turn, parse_folk(turn, folk))

    news = turn.official_news.strip()
    if news and news != MEM.last_official:
        MEM.official.append((turn.day_no, news))
        MEM.last_official = news
        event = parse_official(turn, news)
        if event is not None:
            MEM.events = [item for item in MEM.events if item.ore != event.ore]
            MEM.events.append(event)

    _observe_task(turn)
    _observe_moves(turn)
    _observe_zones(turn)

    if 5 in turn.errors and MEM.llm_used < LLM_DAILY_LIMIT:
        MEM.llm_used = LLM_DAILY_LIMIT

    if turn.last_summon_result:
        MEM.treasure.last_result = turn.last_summon_result
        if turn.last_summon_result in {1, 4}:
            MEM.treasure.done = True
        elif turn.last_summon_result in {2, 3}:
            if MEM.treasure.pos and MEM.treasure.items:
                MEM.treasure.failed.add(
                    (_pos_key(MEM.treasure.pos), tuple(sorted(MEM.treasure.items)), MEM.treasure.day),
                )
            if turn.last_summon_result == 3:
                MEM.treasure.items = []
            # 2 同时表示地点错误或时间未到，不能武断地把日期加一。
            # 3 表示祭品错误；两种情况都要求重新推理后才能再次消耗祭品。
            MEM.treasure.weak = True
            MEM.awaiting_treasure = False

    if turn.llm_resp.strip():
        _absorb_llm(turn)

    MEM.last_round = turn.round_no


def _match_signature(turn: Turn) -> tuple[Any, ...]:
    return (
        turn.team_id,
        turn.team_type,
        turn.width,
        turn.height,
    )


def _ensure_match(turn: Turn) -> None:
    signature = _match_signature(turn)
    new_match = bool(
        MEM.match_signature
        and (
            signature != MEM.match_signature
            or turn.round_no < MEM.last_round
            or (turn.round_no == 1 and MEM.last_round > 1)
        )
    )
    if new_match:
        reset_memory()
    MEM.match_signature = signature


def can_prompt(turn: Turn) -> bool:
    if turn.phase_task.strip():
        return True
    return MEM.llm_used < LLM_DAILY_LIMIT


def mark_prompt(turn: Turn) -> None:
    if not turn.phase_task.strip():
        MEM.llm_used += 1


def _observe_moves(turn: Turn) -> None:
    for unit_id, ok in turn.last_action_ok.items():
        dest = MEM.last_move.get(unit_id)
        if ok:
            MEM.failed_steps.pop(unit_id, None)
        elif dest is not None:
            bucket = MEM.failed_steps.setdefault(unit_id, set())
            bucket.add(dest)
            if len(bucket) > 16:
                victim = next((pos for pos in bucket if pos != dest), None)
                if victim is not None:
                    bucket.discard(victim)

        site = MEM.last_build.get(unit_id)
        if site is None:
            continue
        if ok:
            MEM.bad_build.discard(site)
            MEM.build_failures.pop(site, None)
        else:
            failures = MEM.build_failures.get(site, 0) + 1
            MEM.build_failures[site] = failures
            # 一次失败可能只是临时占格；重复失败才认定为非法建造区。
            if failures >= 2:
                MEM.bad_build.add(site)


def remember_commands(commands: dict[int, dict[str, Any]]) -> None:
    MEM.last_move.clear()
    MEM.last_build.clear()
    for unit_id, command in commands.items():
        action = command.get("action")
        if action not in {"move", "build"}:
            continue
        targets = command.get("targetPos") or ()
        if not targets:
            continue
        raw = targets[0]
        pos = Pos(int(raw["x"]), int(raw["y"]))
        if action == "move":
            MEM.last_move[unit_id] = pos
        else:
            MEM.last_build[unit_id] = pos


def failed_cells(unit_id: int) -> frozenset[Pos]:
    return frozenset(MEM.failed_steps.get(unit_id) or ())


def bad_build_cells() -> frozenset[Pos]:
    return frozenset(MEM.bad_build)


def _observe_zones(turn: Turn) -> None:
    """接口不给可建造区域，只能拿站得住的建筑当合法样本反推。"""
    for wall in turn.walls():
        MEM.good_wall.add(wall.pos)
        MEM.bad_build.discard(wall.pos)
    for weapon in turn.weapons():
        MEM.good_weapon.add(weapon.pos)
        MEM.bad_build.discard(weapon.pos)


def wall_zone_seeds() -> frozenset[Pos]:
    return frozenset(MEM.good_wall)


def weapon_zone_seeds() -> frozenset[Pos]:
    return frozenset(MEM.good_weapon)


def _observe_threat(turn: Turn) -> None:
    """累计敌方机器人出现位置，用来决定塔该朝哪边摆。"""
    for robot in turn.hostile_robots():
        MEM.threat_x += robot.pos.x
        MEM.threat_y += robot.pos.y
        MEM.threat_n += 1
    if MEM.threat_n > 4000:
        MEM.threat_x //= 2
        MEM.threat_y //= 2
        MEM.threat_n //= 2


def threat_anchor(turn: Turn) -> Pos:
    if MEM.threat_n >= 8:
        return Pos(MEM.threat_x // MEM.threat_n, MEM.threat_y // MEM.threat_n)
    return Pos(turn.width // 2, turn.height // 2)


def weapon_ready(turn: Turn, weapon) -> bool:
    # 接口明确下发 cooldown，以服务端状态为唯一真值。
    return weapon.cooldown <= 0


def summon_budget_left() -> int:
    return max(0, 10 - MEM.summon_used)


def note_summon_used() -> None:
    MEM.summon_used += 1


def _clear_task() -> None:
    MEM.task_fails = 0
    MEM.task_answer = ""
    MEM.task_required.clear()
    MEM.task_forbidden.clear()
    MEM.task_started_round = 0
    MEM.task_timeout = 0
    MEM.task_pos = None
    MEM.task_submits = 0
    MEM.abandon_task = False
    MEM.awaiting_task = False
    MEM.prompted_task = ""


def _observe_task(turn: Turn) -> None:
    task = turn.phase_task.strip()
    if not task:
        _clear_task()
        if (
            MEM.pending_task_round
            and turn.round_no - MEM.pending_task_round > 2
        ):
            MEM.pending_task_timeout = 0
            MEM.pending_task_pos = None
            MEM.pending_task_round = 0
        return
    # 同一接取内题目文本可能演进（自进化子题）；只重置求解态，保留超时起点。
    if MEM.prompted_task and MEM.prompted_task != task:
        MEM.task_answer = ""
        MEM.awaiting_task = False
        MEM.prompted_task = task
        try:
            from .evolve import on_task_text

            on_task_text(task)
        except Exception:
            pass
    if MEM.task_started_round == 0:
        MEM.prompted_task = task
        if (
            MEM.pending_task_round
            and turn.round_no - MEM.pending_task_round <= 2
        ):
            MEM.task_started_round = MEM.pending_task_round
            MEM.task_timeout = MEM.pending_task_timeout
            MEM.task_pos = MEM.pending_task_pos
        else:
            MEM.task_started_round = turn.round_no
            pioneer = turn.pioneer()
            if pioneer is not None and turn.tasks:
                nearest = min(
                    turn.tasks,
                    key=lambda item: distance(pioneer.pos, item.pos),
                )
                MEM.task_timeout = nearest.timeout_rounds
                MEM.task_pos = nearest.pos
        MEM.pending_task_timeout = 0
        MEM.pending_task_pos = None
        MEM.pending_task_round = 0
    if MEM.task_timeout == 0:
        # 无法识别任务点时宁可使用最短超时，确保保底答案不会交晚。
        positive = [item.timeout_rounds for item in turn.tasks if item.timeout_rounds > 0]
        MEM.task_timeout = min(positive, default=0)
    for code, msg in zip(turn.errors, turn.error_msgs):
        _ingest_schema_error(msg)
        if code in {1, 2}:
            MEM.task_fails += 1
        # 答案错误后需要重新问一次 LLM，别停在等待态；清掉错误答案防反复提交
        if code == 2:
            MEM.awaiting_task = False
            if MEM.task_answer:
                try:
                    from .evolve import is_junk_answer

                    if is_junk_answer(MEM.task_answer):
                        MEM.task_answer = ""
                except Exception:
                    MEM.task_answer = ""
    # 任务超时即已结束，此后留在任务点没有意义
    if 1 in turn.errors or task_rounds_left(turn) <= 0:
        MEM.abandon_task = True
        if MEM.skip_task_until < turn.round_no:
            MEM.skip_task_until = turn.round_no + 30


def remember_task_accept(task: PlayerTask, round_no: int) -> None:
    MEM.pending_task_timeout = task.timeout_rounds
    MEM.pending_task_pos = task.pos
    MEM.pending_task_round = round_no


def task_rounds_left(turn: Turn) -> int:
    """timeoutRounds 未下发时返回极大值，宁可多等也不要提前放弃。"""
    if not MEM.task_timeout or not MEM.task_started_round:
        return 10**6
    return MEM.task_timeout - (turn.round_no - MEM.task_started_round)


def task_rounds_used(turn: Turn) -> int:
    if not MEM.task_started_round:
        return 0
    return turn.round_no - MEM.task_started_round


def should_abandon_task(turn: Turn) -> bool:
    """只在任务已经结束时离开：答错不算结束，最高通过率会被计分。"""
    return MEM.abandon_task


def force_submit_now(turn: Turn) -> bool:
    """还没交过答案就先交一份保底：部分通过率也计分，交了不亏。"""
    if MEM.task_submits:
        return False
    if task_rounds_left(turn) <= 3:
        return True
    # timeoutRounds 缺失时的兜底，别在一道题上空耗整个白天
    return task_rounds_used(turn) >= 25


def remember_answer(answer: str) -> None:
    MEM.task_answer = answer
    MEM.task_submits += 1
    MEM.awaiting_task = False


def patch_task_answer(raw: str, turn: Turn) -> str:
    blob = (raw or MEM.task_answer or "").strip()
    obj, is_obj = _as_object(blob)
    for key in list(MEM.task_forbidden):
        obj.pop(key, None)
    for key in MEM.task_required:
        if key not in obj:
            obj[key] = _default_field(key, blob, turn)
    if not is_obj and not MEM.task_required:
        return blob
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _ingest_schema_error(msg: str) -> None:
    match = re.search(r"\$/([A-Za-z0-9_]+)\s*[:：]\s*(缺少键|多余键|值不符)", msg)
    if not match:
        return
    key, kind = match.group(1), match.group(2)
    if kind == "缺少键":
        if key not in MEM.task_required:
            MEM.task_required.append(key)
        if key in MEM.task_forbidden:
            MEM.task_forbidden.remove(key)
    elif kind == "多余键":
        if key not in MEM.task_forbidden:
            MEM.task_forbidden.append(key)
        if key in MEM.task_required:
            MEM.task_required.remove(key)


def _as_object(blob: str) -> tuple[dict[str, Any], bool]:
    if blob.startswith("{") and blob.endswith("}"):
        try:
            data = json.loads(blob)
            if isinstance(data, dict):
                return dict(data), True
        except json.JSONDecodeError:
            pass
    return {}, False


def _default_field(key: str, blob: str, turn: Turn) -> Any:
    if key.endswith("_count") or key.endswith("Count"):
        nums = re.findall(r"\d+", turn.last_cmd_result or blob)
        return int(nums[-1]) if nums else 0
    if key.lower() == "token":
        if blob and not blob.startswith("{"):
            return blob
        return _extract_tag(turn.llm_resp, "ANSWER") or blob[:80]
    return ""


def mine_blocked(turn: Turn, ore: str) -> bool:
    for event in MEM.events:
        if event.ore == ore and event.kind == "shortage":
            if event.start_day <= turn.day_no <= event.end_day:
                return True
    return False


def hold_ore(turn: Turn, ore: str) -> bool:
    for event in MEM.events:
        if event.ore == ore and event.kind == "shortage":
            if turn.day_no < event.start_day:
                return True
    return False


def dump_ore(turn: Turn, ore: str) -> bool:
    if hold_ore(turn, ore):
        return False
    for event in MEM.events:
        if event.ore != ore:
            continue
        if (
            event.kind == "shortage"
            and event.start_day <= turn.day_no <= event.end_day
        ):
            return True
        if event.kind == "surplus" and turn.day_no < event.start_day:
            return True
    price = turn.ore_price(ore)
    return price > DEFAULT_ORE_PRICE.get(ore, price)


def upcoming_hot_ore(turn: Turn) -> str | None:
    soon: list[MarketEvent] = []
    for event in MEM.events:
        if event.kind == "shortage" and turn.day_no < event.start_day:
            soon.append(event)
    if not soon:
        return None
    soon.sort(key=lambda event: event.start_day)
    return soon[0].ore


def mine_rank(turn: Turn, keep_stone: bool) -> list[str]:
    """工人采金顺序：涨价矿 > 铜 > 铁；石头只为砌墙，不进金币主线。"""
    hot = upcoming_hot_ore(turn)
    scored: list[tuple[int, int, str]] = []
    for ore in (COPPER, IRON, WALL_MATERIAL):
        if mine_blocked(turn, ore):
            continue
        # 基础档：铜 > 铁 > 石
        tier = 3 if ore == COPPER else 2 if ore == IRON else 0
        score = turn.ore_price(ore) * 10
        if hot == ore:
            score += 100
            tier = 4
        if dump_ore(turn, ore) and not hold_ore(turn, ore):
            score += 40
            tier = max(tier, 4)
        if any(
            event.ore == ore
            and event.kind == "surplus"
            and event.start_day <= turn.day_no <= event.end_day
            for event in MEM.events
        ):
            score -= 100
            tier = min(tier, 1)
        if ore == WALL_MATERIAL:
            # 石头不参与卖金主线；仅在仍需砌墙时作为保底去采
            if keep_stone:
                score = 1
                tier = 0
            else:
                continue
        scored.append((tier, score, ore))
    scored.sort(reverse=True)
    return [ore for _, _, ore in scored]


def treasure_ready(turn: Turn) -> bool:
    guess = MEM.treasure
    if guess.done or guess.pos is None or not guess.items or guess.weak:
        return False
    if guess.day is not None and turn.day_no < guess.day:
        return False
    if guess.phase == "night" and turn.is_day:
        return False
    if guess.phase == "day" and not turn.is_day:
        return False
    key = (_pos_key(guess.pos), tuple(sorted(guess.items)), guess.day)
    return key not in guess.failed


def treasure_imminent(turn: Turn) -> bool:
    guess = MEM.treasure
    if guess.done or guess.pos is None:
        return False
    if guess.day is None:
        return len(MEM.folk) >= 3
    return guess.day <= turn.day_no + 1


def missing_ritual(turn: Turn, role) -> list[str]:
    items = list(MEM.treasure.items)
    if not items:
        return []
    need: list[str] = []
    bag = [item.casefold() for item in role.backpack]
    used: list[str] = list(bag)
    for name in items:
        key = name.casefold()
        if key in used:
            used.remove(key)
            continue
        need.append(name)
    return need


def parse_official(turn: Turn, text: str) -> MarketEvent | None:
    ore = _detect_ore(text)
    if ore is None:
        return None
    kind = "shortage" if any(word in text for word in _STOP_WORDS) else ""
    if any(word in text for word in _SURPLUS_WORDS):
        kind = "surplus"
    if not kind:
        return None
    # 短缺新闻通常描述“今天出事、明天停采”；仅明确写了即日生效才封当天。
    start = turn.day_no if kind == "surplus" else turn.day_no + 1
    today_effective = (
        "即日起", "即日停", "今日起", "今天起", "当天停工", "立即停工",
    )
    if any(word in text for word in _TOMORROW_WORDS):
        start = turn.day_no + 1
    elif any(word in text for word in today_effective):
        start = turn.day_no
    duration = _first_int(text, default=2)
    duration = max(1, min(duration, 5))
    return MarketEvent(ore, kind, start, start + duration - 1, text[:80])


def parse_folk(turn: Turn, text: str) -> TreasureGuess:
    guess = TreasureGuess()
    guess.pos = _extract_pos(turn, text)
    guess.items = _extract_items(turn, text)
    guess.item_count = _extract_item_count(text)
    guess.day = _extract_day(text)
    guess.phase = _extract_phase(text)
    guess.region = _extract_region(text)
    if guess.pos is None and guess.region:
        guess.pos = _region_anchor(turn, guess.region)
        guess.weak = True
    if guess.items and guess.item_count:
        guess.items = guess.items[: guess.item_count]
    return guess


def treasure_prompt(turn: Turn) -> str:
    catalog = "、".join(turn.ritual_catalog())
    landmarks = []
    if turn.vendor_pos():
        landmarks.append(f"小贩{turn.vendor_pos().dump()}")
    if turn.shop_pos():
        landmarks.append(f"商店{turn.shop_pos().dump()}")
    station = turn.station()
    if station:
        landmarks.append(f"我方基地{station.pos.dump()}")
    folk = "\n".join(f"第{idx}条：{text}" for idx, text in enumerate(MEM.folk, 1)) or turn.folk_legends
    return "\n".join((
        "你在解析《未来战争》民间传闻以开启唯一宝藏。",
        f"地图大小 {turn.width}x{turn.height}，原点左下，切比雪夫距离。西=x小，东=x大，南=y小，北=y大。",
        f"地标：{'；'.join(landmarks) or '无'}",
        f"任务用品英文名只能从商店目录中选：{catalog}",
        "只输出一行 JSON，不要解释：",
        'TREASURE:{"x":整数或null,"y":整数或null,"items":["英文名",...],"day":整数或null,"phase":"day或night"}',
        "items 不能多不能少；day 是第几天开启；phase 是白天或夜晚。",
        f"上回合召唤结果码 lastSummonTreasureResult={turn.last_summon_result}（0未用 1成功 2地点/时间不对 3物品不对 4已空）",
        f"当前第{turn.day_no}天。",
        "【民间传闻】",
        folk,
    ))


def treasure_unsolved() -> bool:
    guess = MEM.treasure
    if guess.done:
        return False
    return guess.pos is None or guess.weak or not guess.items or guess.day is None


def treasure_rider(turn: Turn) -> str:
    """任务执行期间 LLM 不限次，顺带把宝藏问题挂在同一次提问里。"""
    if not treasure_unsolved() or not MEM.folk:
        return ""
    catalog = "、".join(turn.ritual_catalog())
    folk = "\n".join(MEM.folk)
    return "\n".join((
        "",
        "【附加题：宝藏推理】除上面那一行外，再额外输出一行：",
        'TREASURE:{"x":整数或null,"y":整数或null,"items":["英文名",...],"day":整数或null,"phase":"day或night"}',
        f"地图 {turn.width}x{turn.height}，原点左下，西=x小 东=x大 南=y小 北=y大。",
        f"物品英文名只能取自：{catalog}",
        f"当前第{turn.day_no}天，上回合召唤结果码={turn.last_summon_result}。",
        "【民间传闻】",
        folk,
    ))


def parse_sandbox_answer(raw: str) -> str:
    if not raw.strip():
        return ""
    if "[TIMEOUT]" in raw or "[JUDGER_ERROR]" in raw:
        return ""
    text = raw
    match = re.search(r"\[exitCode:(-?\d+)\]\s*\n?(.*)", raw, re.S)
    code = 0
    if match:
        code = int(match.group(1))
        text = match.group(2)
    text = re.sub(r"\[TRUNCATED\]\s*$", "", text).strip()
    if code != 0:
        return ""
    if re.search(r"traceback|syntaxerror|command not found", text, re.I):
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    answer = lines[-1]
    if answer.startswith("ANSWER:"):
        answer = answer.split(":", 1)[1].strip()
    if len(answer) > 400:
        return ""
    return answer


def _absorb_llm(turn: Turn) -> None:
    text = turn.llm_resp
    if MEM.awaiting_treasure or "TREASURE:" in text:
        parsed = _parse_treasure_json(turn, text)
        if parsed is not None:
            _merge_treasure(turn, parsed)
            MEM.awaiting_treasure = False
    answer = _extract_tag(text, "ANSWER")
    if answer:
        MEM.awaiting_task = False


def _parse_treasure_json(turn: Turn, text: str) -> TreasureGuess | None:
    match = re.search(r"TREASURE\s*:\s*(\{.*\})", text, re.S | re.I)
    if not match:
        return None
    blob = match.group(1)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        blob = blob.split("}")[0] + "}"
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return None
    guess = TreasureGuess()
    x, y = data.get("x"), data.get("y")
    if isinstance(x, int) and isinstance(y, int):
        if 0 <= x < turn.width and 0 <= y < turn.height:
            guess.pos = Pos(x, y)
    items: list[str] = []
    for raw in data.get("items") or ():
        resolved = turn.resolve_item(str(raw))
        if resolved and resolved not in items:
            items.append(resolved)
    guess.items = items
    day = data.get("day")
    if isinstance(day, int) and 1 <= day <= 10:
        guess.day = day
    phase = str(data.get("phase") or "day").lower()
    guess.phase = "night" if "night" in phase or "晚" in phase else "day"
    return guess


def _merge_treasure(turn: Turn, incoming: TreasureGuess) -> None:
    cur = MEM.treasure
    if cur.done:
        return
    if incoming.pos is not None:
        cur.pos = incoming.pos
        cur.weak = incoming.weak
    if incoming.items:
        cur.items = incoming.items
    if incoming.day is not None:
        cur.day = incoming.day
    if incoming.phase:
        cur.phase = incoming.phase
    if incoming.item_count:
        cur.item_count = incoming.item_count
        if cur.items:
            cur.items = cur.items[: incoming.item_count]
    if incoming.region:
        cur.region = incoming.region
    if cur.pos is None and cur.region:
        cur.pos = _region_anchor(turn, cur.region)
        cur.weak = True


def _detect_ore(text: str) -> str | None:
    for ore, words in _ORE_WORDS.items():
        for word in words:
            if word in text:
                return ore
    return None


def _extract_pos(turn: Turn, text: str) -> Pos | None:
    patterns = (
        r"坐标\s*[（(]?\s*(\d{1,2})\s*[,，]\s*(\d{1,2})\s*[)）]?",
        r"位置\s*[：:]\s*(\d{1,2})\s*[,，]\s*(\d{1,2})",
        r"\(\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)",
        r"x\s*[=＝:：]\s*(\d{1,2})\D{0,8}y\s*[=＝:：]\s*(\d{1,2})",
        r"(?:第)?(\d{1,2})\s*列\D{0,4}(?:第)?(\d{1,2})\s*行",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        pos = Pos(int(match.group(1)), int(match.group(2)))
        if 0 <= pos.x < turn.width and 0 <= pos.y < turn.height:
            return pos
    return None


def _extract_items(turn: Turn, text: str) -> list[str]:
    found: list[str] = []
    catalog = list(turn.ritual_catalog())
    for alias, name in ITEM_ALIASES.items():
        if alias in text or alias.casefold() in text.casefold():
            resolved = turn.resolve_item(name) or name
            if resolved not in found:
                found.append(resolved)
    for name in catalog:
        if name in text or name.casefold() in text.casefold():
            if name not in found:
                found.append(name)
    return found


def _extract_item_count(text: str) -> int | None:
    match = re.search(r"([一二两三四五六七八九十\d]+)\s*(?:钥|件|种|份|枚|样)", text)
    if not match:
        return None
    return _to_int(match.group(1))


def _extract_day(text: str) -> int | None:
    match = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*天", text)
    if match:
        value = _to_int(match.group(1))
        if 1 <= value <= 10:
            return value
    return None


def _extract_phase(text: str) -> str:
    if any(word in text for word in ("夜晚", "夜里", "晚上", "午夜", "子时", "黄昏后")):
        return "night"
    return "day"


def _extract_region(text: str) -> str:
    found = []
    for word in ("东南", "东北", "西南", "西北", "东", "西", "南", "北"):
        if word in text and word not in found:
            found.append(word)
            break
    return found[0] if found else ""


def _region_anchor(turn: Turn, region: str) -> Pos:
    w, h = turn.width, turn.height
    x0, x1 = 0, w - 1
    y0, y1 = 0, h - 1
    if "东" in region:
        x0 = (w * 2) // 3
    if "西" in region:
        x1 = w // 3
    if "北" in region:
        y0 = (h * 2) // 3
    if "南" in region:
        y1 = h // 3
    vendor = turn.vendor_pos()
    if vendor and x0 <= vendor.x <= x1 and y0 <= vendor.y <= y1:
        return vendor
    return Pos((x0 + x1) // 2, (y0 + y1) // 2)


def _first_int(text: str, default: int) -> int:
    match = re.search(r"([一二两三四五六七八九十]+|\d+)\s*天", text)
    if match:
        return _to_int(match.group(1))
    return default


def _to_int(raw: str) -> int:
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    if raw in _CN_NUM:
        return _CN_NUM[raw]
    if raw.startswith("十"):
        rest = raw[1:]
        return 10 + (_CN_NUM.get(rest, 0) if rest else 0)
    if "十" in raw:
        left, right = raw.split("十", 1)
        return _CN_NUM.get(left, 1) * 10 + _CN_NUM.get(right, 0)
    return 0


def _extract_tag(text: str, tag: str) -> str:
    match = re.search(rf"{tag}\s*:\s*(.+)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip().strip("`").strip()


def _pos_key(pos: Pos) -> tuple[int, int]:
    return pos.x, pos.y
