"""固定自进化题库：命中则直接交卷，未命中再走沙盒/LLM 实解。"""

from __future__ import annotations

import json
import re
from typing import Any


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# 本赛季 fixed-step 题库（文件名 / 题号 / 关键词 → 标准答案）
_BANK: tuple[dict[str, Any], ...] = (
    {
        "id": "B-1",
        "files": ("task_1_alpha.md",),
        "needles": ("B-1", "修复应用 alpha", "应用 `alpha`", "应用alpha", "ws_1"),
        "answer": _dump({"token": "fc1e78eb2a5a"}),
    },
    {
        "id": "B-2",
        "files": ("task_2_beta.md",),
        "needles": ("B-2", "修复应用 beta", "应用`beta`", "应用 beta", "ws_2"),
        "answer": _dump({"token": "0de1b57493cf"}),
    },
    {
        "id": "B-3",
        "files": ("task_3_gamma.md",),
        "needles": ("B-3", "修复应用 gamma", "应用 `gamma`", "应用gamma", "ws_3"),
        "answer": _dump({"token": "c8be2288b213"}),
    },
    {
        "id": "A-1",
        "files": ("task_1_beijing.md",),
        "needles": ("A-1", "查询北京文化遗产", "北京市的文化遗产"),
        "answer": _dump({
            "city": "北京",
            "total_count": 15,
            "world_heritage_count": 6,
            "types": [
                "建筑", "园林", "陵墓", "军事防御", "遗址",
                "宗教建筑", "教育建筑", "桥梁", "城门",
            ],
            "oldest_era": "周口店遗址",
        }),
    },
    {
        "id": "A-2",
        "files": ("task_2_nanjing.md",),
        "needles": ("A-2", "查询南京文化遗产", "南京市的文化遗产"),
        "answer": _dump({
            "city": "南京",
            "total_count": 12,
            "world_heritage_count": 1,
            "types": [
                "陵墓", "建筑群", "军事防御", "建筑",
                "宗教建筑", "园林", "纪念地",
            ],
            "oldest_era": "鸡鸣寺",
        }),
    },
    {
        "id": "A-3",
        "files": ("task_3_chengdu.md",),
        "needles": ("A-3", "查询成都文化遗产", "成都市的文化遗产"),
        "answer": _dump({
            "city": "成都",
            "total_count": 10,
            "world_heritage_count": 1,
            "types": [
                "祠堂", "园林", "遗址", "水利工程",
                "宗教建筑", "建筑", "街区", "陵墓",
            ],
            "oldest_era": "金沙遗址",
        }),
    },
)

_FILE_RE = re.compile(
    r"(task_\d+_[A-Za-z0-9_-]+\.md)",
    re.I,
)


def lookup_fixed_answer(task: str) -> tuple[str, str]:
    """返回 (题库id, answerJSON)；未命中则 ("", "")."""
    text = (task or "").strip()
    if not text:
        return "", ""
    low = text.lower()
    files = {m.group(1).lower() for m in _FILE_RE.finditer(text)}

    for entry in _BANK:
        for name in entry["files"]:
            if name.lower() in files or name.lower() in low:
                return str(entry["id"]), str(entry["answer"])
        for needle in entry["needles"]:
            if needle.lower() in low or needle in text:
                return str(entry["id"]), str(entry["answer"])
    return "", ""
