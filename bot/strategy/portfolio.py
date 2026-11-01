"""权重封顶与组合合成（spec §4.3、§4.5）。"""

from __future__ import annotations

from collections.abc import Mapping


def cap_weights(a: Mapping[str, float], cap: float, max_iter: int = 20) -> dict[str, float]:
    """迭代封顶（water-filling）：超出上限的部分按比例分给未封顶的币；全部封顶后剩余部分留作 USDT。"""
    w = dict(a)
    for _ in range(max_iter):
        over = {k for k, v in w.items() if v > cap + 1e-12}
        if not over:
            break
        excess = sum(w[k] - cap for k in over)
        for k in over:
            w[k] = cap
        free = {k for k, v in w.items() if v < cap - 1e-12}
        if not free:
            break
        tot = sum(w[k] for k in free)
        if tot <= 0:
            break
        for k in free:
            w[k] += excess * w[k] / tot
    return w


def combine_sleeves(
    sleeve_targets: Mapping[str, Mapping[str, float]], capital_weights: Mapping[str, float]
) -> dict[str, float]:
    """W_t(i) = Σ capital_weight × S_t^sleeve(i)；去掉权重为 0 的项。"""
    out: dict[str, float] = {}
    for name, targets in sleeve_targets.items():
        cw = capital_weights[name]
        for sym, w in targets.items():
            out[sym] = out.get(sym, 0.0) + cw * w
    return {k: v for k, v in out.items() if v > 0}
