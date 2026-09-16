from collections import Counter
from dataclasses import dataclass
from typing import Any

DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
ROUNDS_PER_DAY = DAY_ROUNDS + NIGHT_ROUNDS

WEAPON_BUILD_COST = 25
WALL_MATERIAL = "stone"
IRON = "iron"
COPPER = "copper"
ORES = (WALL_MATERIAL, IRON, COPPER)

LAND = "land"
STATION = "station"
WALL = "wall"
WORKER = "worker"
PIONEER = "pioneer"
VENDOR = "vendor"
WEAPON_SHOP = "weaponShop"

TOWER_TYPES = ("gatling", "railgun", "rocket")
CONTROLLABLE_TYPES = (WORKER, PIONEER)
TOWER_RANGE_BY_LEVEL = {
    "gatling": (3, 5, 7),
    "railgun": (6, 8, 10),
    "rocket": (10, 15, 10**9),
}
TOWER_SHOTS_BY_LEVEL = {
    "gatling": (1, 2, 3),
    "railgun": (1, 1, 1),
    "rocket": (1, 2, 3),
}

ROBOT_KILL_SCORE = {
    "bossRobot": 10,
    "largeRobot": 4,
    "middleRobot": 2,
    "smallRobot": 1,
}
ROBOT_MAX_HP = {
    "bossRobot": 800,
    "largeRobot": 500,
    "middleRobot": 60,
    "smallRobot": 40,
}

WEAPON_UPGRADE_1 = "WeaponUpgradeVoucher1"
WEAPON_UPGRADE_2 = "WeaponUpgradeVoucher2"
STATION_UPGRADE_1 = "StationUpgradeVoucher1"
STATION_UPGRADE_2 = "StationUpgradeVoucher2"
WALL_UPGRADE_1 = "WallUpgradeVoucher1"
WALL_UPGRADE_2 = "WallUpgradeVoucher2"
WALL_FIXER = "WallFixer"
MEDICINE = "Medicine"
DIZZY = "DizzyWeapon"
BOMB = "Bomb"
SMALL_SUMMON = "SmallRobotSummonOrder"
MIDDLE_SUMMON = "MiddleRobotSummonOrder"
LARGE_SUMMON = "LargeRobotSummonOrder"
BOSS_SUMMON = "BossRobotSummonOrder"

CONSUMABLES = (
    WALL_FIXER, MEDICINE, DIZZY, BOMB,
    SMALL_SUMMON, MIDDLE_SUMMON, LARGE_SUMMON, BOSS_SUMMON,
)
VOUCHERS = (
    WEAPON_UPGRADE_1, WEAPON_UPGRADE_2,
    STATION_UPGRADE_1, STATION_UPGRADE_2,
    WALL_UPGRADE_1, WALL_UPGRADE_2,
)
KNOWN_GOODS = CONSUMABLES + VOUCHERS + ORES

ITEM_ALIASES = {
    "古符石板": "AcientTablet",
    "石板": "AcientTablet",
    "符石": "AcientTablet",
    "acienttablet": "AcientTablet",
    "ancienttablet": "AcientTablet",
    "星辰之沙": "StarSand",
    "星沙": "StarSand",
    "星辰": "StarSand",
    "starsand": "StarSand",
    "烈焰之息": "FlameBreath",
    "烈焰": "FlameBreath",
    "flamebreath": "FlameBreath",
    "寒霜药剂": "FrostPotion",
    "寒霜": "FrostPotion",
    "frostpotion": "FrostPotion",
    "荆棘护符": "ThornAmulet",
    "荆棘": "ThornAmulet",
    "thornamulet": "ThornAmulet",
    "回音铁哨": "IronWhistle",
    "铁哨": "IronWhistle",
    "ironwhistle": "IronWhistle",
}

DEFAULT_ORE_PRICE = {WALL_MATERIAL: 1, IRON: 3, COPPER: 5}
DEFAULT_SHOP_PRICE = {
    WEAPON_UPGRADE_1: 100,
    WEAPON_UPGRADE_2: 150,
    STATION_UPGRADE_1: 100,
    STATION_UPGRADE_2: 150,
    WALL_UPGRADE_1: 20,
    WALL_UPGRADE_2: 30,
    WALL_FIXER: 10,
    MEDICINE: 10,
    DIZZY: 100,
    BOMB: 100,
}


@dataclass(frozen=True, slots=True)
class Pos:
    x: int
    y: int

    @classmethod
    def load(cls, raw: Any) -> "Pos":
        return cls(int(raw["x"]), int(raw["y"]))

    def dump(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}


def distance(first: Pos, second: Pos) -> int:
    return max(abs(first.x - second.x), abs(first.y - second.y))


def station_footprint(pos: Pos) -> tuple[Pos, ...]:
    return (
        pos,
        Pos(pos.x + 1, pos.y),
        Pos(pos.x, pos.y - 1),
        Pos(pos.x + 1, pos.y - 1),
    )


def station_cells(pos: Pos) -> tuple[Pos, ...]:
    # 覆盖「左上角」与「左下角」两种 2x2 解读，避免走进基地占格
    return tuple({
        *station_footprint(pos),
        Pos(pos.x, pos.y + 1),
        Pos(pos.x + 1, pos.y + 1),
    })


def day_index(round_no: int) -> int:
    return (round_no - 1) // ROUNDS_PER_DAY + 1


def round_in_day(round_no: int) -> int:
    return (round_no - 1) % ROUNDS_PER_DAY


@dataclass(frozen=True, slots=True)
class Unit:
    unit_id: int
    pos: Pos
    kind: str
    health: int
    level: int
    cooldown: int
    attack_range: int
    capacity: int | None
    backpack: tuple[str, ...]

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Unit":
        raw_capacity = raw.get("backPackCapability")
        return cls(
            int(raw.get("id") or 0),
            Pos.load(raw["pos"]),
            str(raw["roleType"]),
            int(raw["health"]),
            int(raw.get("level") or 0),
            int(raw.get("cooldown") or 0),
            int(raw.get("attackRange") or 0),
            int(raw_capacity) if raw_capacity is not None else None,
            tuple(str(item) for item in raw.get("backpack") or ()),
        )

    @property
    def backpack_full(self) -> bool:
        if self.capacity is None:
            return False
        return len(self.backpack) >= self.capacity

    def item_count(self, name: str) -> int:
        target = name.casefold()
        return sum(1 for item in self.backpack if item.casefold() == target)

    def has_item(self, name: str) -> bool:
        return self.item_count(name) > 0

    def find_item(self, name: str) -> str | None:
        target = name.casefold()
        for item in self.backpack:
            if item.casefold() == target:
                return item
        return None

    def ore_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for item in self.backpack:
            key = item.casefold()
            if key in ORES:
                counts[key] += 1
        return counts

    def range_of_attack(self) -> int:
        if self.attack_range > 0:
            return self.attack_range
        table = TOWER_RANGE_BY_LEVEL.get(self.kind)
        if table is None:
            return 0
        level = min(max(self.level, 1), len(table))
        return table[level - 1]

    def shot_count(self) -> int:
        table = TOWER_SHOTS_BY_LEVEL.get(self.kind)
        if table is None:
            return 1
        level = min(max(self.level, 1), len(table))
        return table[level - 1]


@dataclass(frozen=True, slots=True)
class Robot:
    robot_id: int
    pos: Pos
    health: int
    kind: str
    abnormal: str
    target_team: str

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Robot":
        return cls(
            int(raw["id"]),
            Pos.load(raw["pos"]),
            int(raw["health"]),
            str(raw.get("roleType") or "smallRobot"),
            str(raw.get("abnormalState") or ""),
            str(raw.get("targetTeam") or ""),
        )

    @property
    def kill_score(self) -> int:
        return ROBOT_KILL_SCORE.get(self.kind, 1)

    @property
    def dizzy(self) -> bool:
        return self.abnormal.casefold() == "dizzy"


@dataclass(frozen=True, slots=True)
class PlayerTask:
    task_type: str
    pos: Pos
    cooldown: int
    score_reward: int
    gold_reward: int
    valid: bool
    timeout_rounds: int

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "PlayerTask":
        return cls(
            str(raw.get("taskType") or ""),
            Pos.load(raw["taskPosition"]),
            int(raw.get("coldDownRounds") or 0),
            int(raw.get("scoreReward") or 0),
            int(raw.get("goldReward") or 0),
            bool(raw.get("isValid")),
            int(raw.get("timeoutRounds") or 0),
        )


@dataclass(frozen=True, slots=True)
class Turn:
    round_no: int
    is_day: bool
    day_no: int
    round_of_day: int
    gold: int
    team_type: str
    width: int
    height: int
    zones: dict[Pos, str]
    ours: tuple[Unit, ...]
    enemies: tuple[Unit, ...]
    robots: tuple[Robot, ...]
    tasks: tuple[PlayerTask, ...]
    vendor_prices: dict[str, int]
    shop_prices: dict[str, int]
    phase_task: str
    llm_resp: str
    official_news: str
    folk_legends: str
    last_cmd_result: str
    last_summon_result: int
    errors: tuple[int, ...]
    error_msgs: tuple[str, ...]
    last_action_ok: dict[int, bool]

    @classmethod
    def load(cls, payload: dict[str, Any]) -> "Turn":
        round_no = int(payload["roundNo"])
        info = payload["mapInfo"]
        team = payload["teamOur"]
        news = payload.get("worldNews") or {}
        vendor_prices = dict(DEFAULT_ORE_PRICE)
        for entry in payload.get("vendorShopList") or ():
            vendor_prices[str(entry["name"]).casefold()] = int(entry["price"])
        shop_prices = dict(DEFAULT_SHOP_PRICE)
        for entry in payload.get("weaponShopList") or ():
            shop_prices[str(entry["name"])] = int(entry["price"])
        return cls(
            round_no,
            round_in_day(round_no) < DAY_ROUNDS,
            day_index(round_no),
            round_in_day(round_no),
            int(team.get("goldNum") or 0),
            str(team.get("type") or ""),
            int(info["width"]),
            int(info["height"]),
            {
                Pos.load(zone["pos"]): str(zone["neutralType"])
                for zone in info.get("zones") or ()
            },
            tuple(Unit.load(role) for role in team.get("roles") or ()),
            tuple(
                Unit.load(role)
                for role in (payload.get("teamEnemy") or {}).get("roles") or ()
            ),
            tuple(
                Robot.load(robot)
                for robot in (payload.get("robot") or {}).get("roles") or ()
            ),
            tuple(
                PlayerTask.load(task)
                for task in team.get("playerTasks") or ()
            ),
            vendor_prices,
            shop_prices,
            str(payload.get("phaseTask") or ""),
            str(payload.get("llmResp") or ""),
            str(news.get("officialNews") or ""),
            str(news.get("folkLegends") or ""),
            str(payload.get("lastCmdResult") or ""),
            int(payload.get("lastSummonTreasureResult") or 0),
            tuple(
                int(err.get("errorCode") or 0)
                for err in payload.get("errors") or ()
            ),
            tuple(
                str(err.get("description") or "")
                for err in payload.get("errors") or ()
            ),
            {
                int(key): bool(value)
                for key, value in (payload.get("lastRoundRoleActionResults") or {}).items()
            },
        )

    @property
    def near_night(self) -> bool:
        return self.is_day and self.round_of_day >= DAY_ROUNDS - 10

    def station(self) -> Unit | None:
        for unit in self.ours:
            if unit.kind == STATION:
                return unit
        return None

    def pioneer(self) -> Unit | None:
        roles = self.alive((PIONEER,))
        return roles[0] if roles else None

    def alive(self, kinds: tuple[str, ...]) -> tuple[Unit, ...]:
        return tuple(
            unit for unit in self.ours
            if unit.kind in kinds and unit.health > 0
        )

    def controllable(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive(CONTROLLABLE_TYPES), key=lambda unit: unit.unit_id,
        ))

    def workers(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive((WORKER,)), key=lambda unit: unit.unit_id,
        ))

    def weapons(self) -> tuple[Unit, ...]:
        return tuple(sorted(
            self.alive(TOWER_TYPES),
            key=lambda unit: (unit.pos.x, unit.pos.y),
        ))

    def walls(self) -> tuple[Unit, ...]:
        return self.alive((WALL,))

    def mines(self, kind: str) -> tuple[Pos, ...]:
        key = kind.casefold()
        return tuple(pos for pos, name in self.zones.items() if name == key)

    def stone_mines(self) -> tuple[Pos, ...]:
        return self.mines(WALL_MATERIAL)

    def vendor_pos(self) -> Pos | None:
        for pos, kind in self.zones.items():
            if kind == VENDOR:
                return pos
        return None

    def shop_pos(self) -> Pos | None:
        for pos, kind in self.zones.items():
            if kind == WEAPON_SHOP:
                return pos
        return None

    def our_task_points(self) -> tuple[Pos, ...]:
        prefix = (
            "challengerTaskPoint"
            if self.team_type == "challenger"
            else "defenderTaskPoint"
        )
        return tuple(
            pos for pos, kind in self.zones.items() if kind.startswith(prefix)
        )

    def available_tasks(self) -> tuple[PlayerTask, ...]:
        return tuple(
            task for task in self.tasks
            if task.valid and task.cooldown <= 0
        )

    def hostile_robots(self) -> tuple[Robot, ...]:
        return tuple(
            robot for robot in self.robots
            if robot.health > 0
            and (not robot.target_team or robot.target_team == self.team_type)
        )

    def footprint(self, unit: Unit) -> tuple[Pos, ...]:
        if unit.kind == STATION:
            return station_cells(unit.pos)
        return (unit.pos,)

    def land(self, pos: Pos) -> bool:
        if not 0 <= pos.x < self.width or not 0 <= pos.y < self.height:
            return False
        return self.zones.get(pos, LAND) == LAND

    def occupied_cells(self) -> frozenset[Pos]:
        cells: set[Pos] = set()
        for unit in self.ours:
            cells.update(self.footprint(unit))
        return frozenset(cells)

    def blocked(self, moving: Unit) -> frozenset[Pos]:
        cells = {pos for pos, kind in self.zones.items() if kind != LAND}
        cells.update(self.occupied_cells())
        cells.discard(moving.pos)
        for robot in self.robots:
            cells.add(robot.pos)
        for enemy in self.enemies:
            if enemy.kind in CONTROLLABLE_TYPES and enemy.health > 0:
                cells.add(enemy.pos)
            elif enemy.kind == STATION:
                cells.update(station_cells(enemy.pos))
            elif enemy.health > 0:
                cells.add(enemy.pos)
        return frozenset(cells)

    def shop_price(self, name: str) -> int:
        if name in self.shop_prices:
            return self.shop_prices[name]
        for key, price in self.shop_prices.items():
            if key.casefold() == name.casefold():
                return price
        return DEFAULT_SHOP_PRICE.get(name, 10**9)

    def ore_price(self, name: str) -> int:
        return self.vendor_prices.get(name.casefold(), 0)

    def ritual_catalog(self) -> tuple[str, ...]:
        known = {name.casefold() for name in KNOWN_GOODS}
        names: list[str] = []
        for name in self.shop_prices:
            if name.casefold() in known:
                continue
            names.append(name)
        if not names:
            names = [
                "AcientTablet", "StarSand", "FlameBreath",
                "FrostPotion", "ThornAmulet", "IronWhistle",
            ]
        return tuple(names)

    def resolve_item(self, raw: str) -> str | None:
        text = raw.strip()
        if not text:
            return None
        catalog = {name.casefold(): name for name in self.shop_prices}
        for name in self.ritual_catalog():
            catalog[name.casefold()] = name
        key = text.casefold()
        if key in catalog:
            return catalog[key]
        alias = ITEM_ALIASES.get(text) or ITEM_ALIASES.get(key)
        if alias:
            return catalog.get(alias.casefold(), alias)
        for name in catalog.values():
            if text in name or name in text:
                return name
        return None


def move_command(pos: Pos) -> dict[str, Any]:
    return {"action": "move", "targetPos": [pos.dump()]}


def collect_command(pos: Pos) -> dict[str, Any]:
    return {"action": "collect", "targetPos": [pos.dump()]}


def build_command(pos: Pos, name: str) -> dict[str, Any]:
    return {"action": "build", "targetPos": [pos.dump()], "name": name}


def attack_command(controller_id: int, *positions: Pos) -> dict[str, Any]:
    return {
        "action": "attack",
        "targetPos": [pos.dump() for pos in positions],
        "controllerId": str(controller_id),
    }


def sell_command(name: str, num: int) -> dict[str, Any]:
    return {"action": "sell", "name": name, "num": num}


def buy_command(name: str, num: int = 1) -> dict[str, Any]:
    return {"action": "buy", "name": name, "num": num}


def use_command(name: str, target: Pos | None = None) -> dict[str, Any]:
    command: dict[str, Any] = {"action": "use", "name": name}
    if target is not None:
        command["targetPos"] = [target.dump()]
    return command


def accept_task_command() -> dict[str, Any]:
    return {"action": "acceptTask"}


def submit_answer_command(answer: str) -> dict[str, Any]:
    return {"action": "submitAnswer", "taskAnswer": answer}


def summon_treasure_command(pos: Pos, items: list[str]) -> dict[str, Any]:
    return {
        "action": "summonTreasure",
        "targetPos": [pos.dump()],
        "item": items,
    }
