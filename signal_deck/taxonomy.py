from __future__ import annotations

import re


CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("robotics", ("robot", "actuator", "pneumatic", "servo", "gripper", "leg", "arm", "walking")),
    ("mechanisms", ("gear", "cam", "linkage", "joint", "bearing", "spring", "flexure", "clutch", "ratchet")),
    ("vehicles", ("vehicle", "wheel", "track", "suspension", "drone", "lift", "downforce", "tether")),
    ("fabrication", ("3d print", "printing", "printer", "extruder", "resin", "filament", "cnc", "cast")),
    ("materials", ("material", "silica", "aluminum", "magnet", "neodymium", "ceramic", "polymer", "composite")),
    ("optics", ("lens", "light", "mirror", "oled", "transparent", "camera", "vision")),
    ("controls", ("control", "closed loop", "pid", "sensor", "signal", "feedback", "logic")),
    ("math", ("quaternion", "theorem", "fractal", "geodesic", "infinite", "exponential", "graph")),
    ("tools", ("desk", "cutlery", "clip", "box", "lock", "typewriter", "pillow", "umbrella")),
    ("philosophy", ("meaning", "purpose", "happy", "think", "problem", "solution", "win")),
)


def categorize_text(text: str, max_categories: int = 4) -> list[str]:
    haystack = text.lower()
    scored: list[tuple[int, str]] = []
    for category, terms in CATEGORY_RULES:
        score = 0
        for term in terms:
            if " " in term:
                score += haystack.count(term) * 2
            else:
                score += len(re.findall(rf"\b{re.escape(term)}s?\b", haystack))
        if score:
            scored.append((score, category))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [category for _score, category in scored[:max_categories]] or ["uncategorized"]
