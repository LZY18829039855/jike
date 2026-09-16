from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .protocol import COPPER, IRON, ITEM_ALIASES, Pos, Turn, WALL_MATERIAL

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


def observe(turn: Turn) -> None:
    if MEM.llm_day != turn.day_no:
        MEM.llm_day = turn.day_no
        MEM.llm_used = 0
        MEM.awaiting_treasure = False

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
            if turn.last_summon_result == 2:
                nxt = (MEM.treasure.day or turn.day_no) + 1
                MEM.treasure.day = nxt if nxt <= 10 else MEM.treasure.day
            MEM.awaiting_treasure = False

    if turn.llm_resp.strip():
        _absorb_llm(turn)


def can_prompt(turn: Turn) -> bool:
    if turn.phase_task.strip():
        return True
    return MEM.llm_used < LLM_DAILY_LIMIT


def mark_prompt(turn: Turn) -> None:
    if not turn.phase_task.strip():
        MEM.llm_used += 1


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
        if event.start_day <= turn.day_no <= event.end_day:
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
    hot = upcoming_hot_ore(turn)
    scored: list[tuple[int, str]] = []
    for ore in (COPPER, IRON, WALL_MATERIAL):
        if mine_blocked(turn, ore):
            continue
        score = turn.ore_price(ore) * 10
        if hot == ore:
            score += 80
        if dump_ore(turn, ore) and not hold_ore(turn, ore):
            score += 15
        if keep_stone and ore == WALL_MATERIAL:
            score += 40
        scored.append((score, ore))
    scored.sort(reverse=True)
    return [ore for _, ore in scored]


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
    start = turn.day_no + 1
    if any(word in text for word in _TOMORROW_WORDS):
        start = turn.day_no + 1
    elif any(word in text for word in _TODAY_WORDS) and "明天" not in text and "明日" not in text:
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
