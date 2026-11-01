"""服务端生成的 SVG 小图（不依赖任何前端库或外部 CDN）。

颜色用 CSS 类（见模板中的样式），随浅色 / 深色模式变化；图形按容器宽度拉伸，
坐标文字放在图下方的 HTML 里（拉伸 SVG 会让其中的文字变形）。
返回值都是 markupsafe.Markup，模板中直接输出；所有数值都由本模块格式化，不含用户输入。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from markupsafe import Markup

from bot.app.dashboard.data import NavPoint, RegimePoint

W, H = 640, 200
PAD_L, PAD_R, PAD_T, PAD_B = 4, 4, 8, 8


def _scale(lo: float, hi: float, top: float, bottom: float) -> tuple[float, float]:
    if hi <= lo:
        hi, lo = lo + 1e-9, lo - 1e-9
    k = (bottom - top) / (hi - lo)
    return k, top + hi * k


def _path(xs: Sequence[float], ys: Sequence[float | None]) -> str:
    parts: list[str] = []
    pen_up = True
    for x, y in zip(xs, ys, strict=True):
        if y is None:
            pen_up = True
            continue
        parts.append(f"{'M' if pen_up else 'L'}{x:.1f},{y:.1f}")
        pen_up = False
    return " ".join(parts)


def _frame(body: str, label: str, first: date, last: date, note: str) -> Markup:
    return Markup(
        f'<figure class="chart"><svg viewBox="0 0 {W} {H}" role="img" aria-label="{label}" '
        f'preserveAspectRatio="none">{body}</svg>'
        f"<figcaption><span>{first.isoformat()}</span><span>{note}</span><span>{last.isoformat()}</span>"
        "</figcaption></figure>"
    )


def _xs(n: int) -> list[float]:
    span = W - PAD_L - PAD_R
    return [PAD_L + (span * i / (n - 1) if n > 1 else span / 2) for i in range(n)]


def nav_chart(points: Sequence[NavPoint]) -> Markup:
    """单位净值折线；回撤用浅色面积画在同一张图的下部。"""
    if not points:
        return Markup('<p class="muted">还没有净值快照。</p>')
    xs = _xs(len(points))
    vals = [p.navu for p in points]
    lo, hi = min(vals), max(vals)
    k, b = _scale(lo, hi, PAD_T, H - PAD_B - 40)
    line = _path(xs, [b - v * k for v in vals])
    if len(points) == 1:  # 只有一个点：画一条水平线
        line = f"M{PAD_L},{b - vals[0] * k:.1f} L{W - PAD_R},{b - vals[0] * k:.1f}"
    dd_lo = min(min(p.drawdown for p in points), -1e-9)
    dk, db = _scale(dd_lo, 0.0, H - PAD_B - 34, H - PAD_B)
    dd = _path(xs, [db - p.drawdown * dk for p in points])
    base = db
    area = f"{dd} L{xs[-1]:.1f},{base:.1f} L{xs[0]:.1f},{base:.1f} Z" if len(points) > 1 else ""
    body = f'<path class="dd" d="{area}"/>' if area else ""
    body += f'<path class="line" d="{line}"/>'
    note = f"区间 {lo:.4f} ～ {hi:.4f} · 最大回撤 {min(p.drawdown for p in points):.1%}"
    return _frame(body, "单位净值与回撤", points[0].day, points[-1].day, note)


def regime_chart(points: Sequence[RegimePoint]) -> Markup:
    """BTC 收盘价与均线；Risk-On 的日子用浅色背景标出。"""
    if not points:
        return Markup('<p class="muted">还没有大盘状态记录（第一次运行后出现）。</p>')
    xs = _xs(len(points))
    vals = [p.close for p in points] + [p.sma for p in points if p.sma is not None]
    lo, hi = min(vals), max(vals)
    k, b = _scale(lo, hi, PAD_T, H - PAD_B)
    step = (W - PAD_L - PAD_R) / max(len(points) - 1, 1)
    bands = "".join(
        f'<rect class="on" x="{x - step / 2:.1f}" y="{PAD_T}" width="{step:.2f}" height="{H - PAD_T - PAD_B}"/>'
        for x, p in zip(xs, points, strict=True)
        if p.risk_on
    )
    close = _path(xs, [b - p.close * k for p in points])
    sma = _path(xs, [b - p.sma * k if p.sma is not None else None for p in points])
    body = f'{bands}<path class="sma" d="{sma}"/><path class="line" d="{close}"/>'
    return _frame(body, "BTC 收盘价与均线", points[0].day, points[-1].day, f"区间 {lo:,.0f} ～ {hi:,.0f}")


def weight_bars(weights: Sequence[tuple[str, float]], *, total: float | None = None) -> Markup:
    """横向条形图：名称、比例条、百分比。total 为 None 时按最大值归一。"""
    if not weights:
        return Markup('<p class="muted">无</p>')
    top = total if total else max(w for _, w in weights) or 1.0
    rows = "".join(
        f'<div class="bar"><span class="name">{Markup.escape(name)}</span><span class="track"><span class="fill" '
        f'style="width:{max(0.0, min(100.0, 100 * w / top)):.1f}%"></span></span><span class="pct">{w:.1%}</span></div>'
        for name, w in weights
    )
    return Markup(f'<div class="bars">{rows}</div>')
