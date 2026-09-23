from heapq import heappop, heappush
from itertools import count

from .protocol import Pos, Turn, Unit, distance

_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


def next_step(
    turn: Turn,
    moving: Unit,
    goal: Pos,
    extra_blocked: frozenset[Pos] | set[Pos] = frozenset(),
    *,
    allow_greedy: bool = True,
) -> Pos | None:
    blocked = set(turn.blocked(moving))
    blocked.update(extra_blocked)
    if moving.pos == goal:
        return None
    order = count()
    frontier: list[tuple[int, int, int, Pos]] = [
        (distance(moving.pos, goal), 0, next(order), moving.pos)
    ]
    came_from: dict[Pos, Pos] = {}
    best = {moving.pos: 0}
    seen: set[Pos] = set()

    while frontier:
        _, cost, _, current = heappop(frontier)
        if current in seen:
            continue
        if current == goal:
            return _first_step(came_from, moving.pos, goal)
        seen.add(current)
        for dx, dy in _STEPS:
            step = Pos(current.x + dx, current.y + dy)
            if step in blocked or not turn.land(step):
                continue
            new_cost = cost + 1
            if new_cost >= best.get(step, new_cost + 1):
                continue
            best[step] = new_cost
            came_from[step] = current
            heappush(
                frontier,
                (
                    new_cost + distance(step, goal),
                    new_cost,
                    next(order),
                    step,
                ),
            )
    if not allow_greedy:
        return None
    return _greedy_step(turn, moving, goal, blocked)


def _greedy_step(
    turn: Turn, moving: Unit, goal: Pos, blocked: set[Pos],
) -> Pos | None:
    now = distance(moving.pos, goal)
    options: list[tuple[int, int, int, Pos]] = []
    for dx, dy in _STEPS:
        step = Pos(moving.pos.x + dx, moving.pos.y + dy)
        if step in blocked or not turn.land(step):
            continue
        options.append((distance(step, goal), abs(dx) + abs(dy), step.x + step.y, step))
    if not options:
        return None
    options.sort(
        key=lambda option: (
            option[0],
            option[1],
            option[2],
            option[3].x,
            option[3].y,
        )
    )
    best = options[0][3]
    if distance(best, goal) >= now and now <= 1:
        return None
    return best


def _first_step(came_from: dict[Pos, Pos], start: Pos, goal: Pos) -> Pos:
    current = goal
    while came_from.get(current, start) != start:
        current = came_from[current]
    return current
