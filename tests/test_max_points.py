"""点数上限参数（0.2.0）：prompt 与 CLI 同源，缺省行为不变。"""

from __future__ import annotations

from kpe.cli import build_parser
from kpe.prompt import DEFAULT_MAX_POINTS, SYSTEM, build_system, clamp_max_points


def test_default_prompt_is_six():
    assert "1–6 个知识点" in SYSTEM
    assert build_system(DEFAULT_MAX_POINTS) == SYSTEM


def test_build_system_tunes_upper_bound():
    assert "1–3 个知识点" in build_system(3)
    assert "1–20 个知识点" in build_system(999)


def test_clamp_max_points():
    assert clamp_max_points(0) == 1
    assert clamp_max_points(-5) == 1
    assert clamp_max_points(3) == 3
    assert clamp_max_points(999) == 20
    assert clamp_max_points("x") == DEFAULT_MAX_POINTS
    assert clamp_max_points(None) == DEFAULT_MAX_POINTS


def test_cli_default_and_override():
    p = build_parser()
    ns = p.parse_args(["extract", "out", "--base-url", "u", "--model", "m"])
    assert ns.max_points == DEFAULT_MAX_POINTS
    ns2 = p.parse_args(["extract", "out", "--base-url", "u", "--model", "m",
                        "--max-points", "4"])
    assert ns2.max_points == 4