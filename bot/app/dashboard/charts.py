"""服务端生成的 SVG 静态图：不开脚本时的后备，也是 app.js 交互图的初始画面（D-080）。

每张图只有一个纵轴：单位净值与回撤分成两张图。颜色用 CSS 类（见 static/app.css），随浅色 / 深色模式变化；
图形按容器宽度拉伸，坐标文字放在图下方的 HTML 里（拉伸 SVG 会让其中的文字变形）。
返回值都是 markupsafe.Markup；所有数值都由本模块格式化，不含用户输入。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from markupsafe import Markup

from bot.app.dashboard.data import NavPoint, RegimePoint

W, H = 640, 180
PAD = 6


def _scale(lo: float, hi: float, top: float, bottom: float) -> tuple[float, float]:
    """返回 (k, b)，y = b - v * k 把 [lo, hi] 映射到 [bottom, top]。"""
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


def _xs(n: int) -> list[float]:
    span = W - 2 * PAD
    return [PAD + (span * i / (n - 1) if n > 1 else span / 2) for i in range(n)]


def _frame(body: str, label: str, h: int, first: date, last: date, note: str) -> Markup:
    return Markup(
        f'<svg class="static-chart" viewBox="0 0 {W} {h}" role="img" aria-label="{label}" '
        f'preserveAspectRatio="none" style="height:{h}px">{body}</svg>'
        f'<div class="axis-row"><span>{first.isoformat()}</span><span>{note}</span>'
        f"<span>{last.isoformat()}</span></div>"
    )


def _empty(text: str) -> Markup:
    return Markup(f'<p class="empty">{text}</p>')


def nav_chart(points: Sequence[NavPoint]) -> Markup:
    if not points:
        return _empty("还没有净值快照。")
    xs = _xs(len(points))
    vals = [p.navu for p in points]
    lo, hi = min(vals), max(vals)
    k, b = _scale(lo, hi, PAD, H - PAD)
    if len(points) == 1:  # 只有一个点：画一条水平线
        y = b - vals[0] * k
        line = f"M{PAD},{y:.1f} L{W - PAD},{y:.1f}"
    else:
        line = _path(xs, [b - v * k for v in vals])
    body = f'<path class="s1" d="{line}"/>'
    return _frame(body, "单位净值", H, points[0].day, points[-1].day, f"{lo:.4f} ～ {hi:.4f}")


def drawdown_chart(points: Sequence[NavPoint]) -> Markup:
    if not points:
        return Markup("")
    h = 72
    xs = _xs(len(points))
    lo = min(min(p.drawdown for p in points), -1e-9)
    k, b = _scale(lo, 0.0, PAD, h - PAD)
    line = _path(xs, [b - p.drawdown * k for p in points])
    area = f"{line} L{xs[-1]:.1f},{b:.1f} L{xs[0]:.1f},{b:.1f} Z" if len(points) > 1 else ""
    body = (f'<path class="dd-area" d="{area}"/>' if area else "") + f'<path class="dd" d="{line}"/>'
    note = f"最大回撤 {lo:.1%}" if lo < -1e-6 else "无回撤"
    return _frame(body, "回撤", h, points[0].day, points[-1].day, note)


def regime_chart(points: Sequence[RegimePoint]) -> Markup:
    """BTC 收盘价（墨色）与均线（系列 2）；Risk-On 的日子用浅色背景标出。"""
    if not points:
        return _empty("还没有大盘数据（第一次运行或同步行情后出现）。")
    xs = _xs(len(points))
    vals = [p.close for p in points] + [p.sma for p in points if p.sma is not None]
    lo, hi = min(vals), max(vals)
    k, b = _scale(lo, hi, PAD, H - PAD)
    step = (W - 2 * PAD) / max(len(points) - 1, 1)
    bands = "".join(
        f'<rect class="band-on" x="{x - step / 2:.1f}" y="0" width="{step + 0.5:.2f}" height="{H}"/>'
        for x, p in zip(xs, points, strict=True)
        if p.risk_on
    )
    close = _path(xs, [b - p.close * k for p in points])
    sma = _path(xs, [b - p.sma * k if p.sma is not None else None for p in points])
    body = f'{bands}<path class="s2" d="{sma}"/><path class="price" d="{close}"/>'
    return _frame(body, "BTC 收盘价与均线", H, points[0].day, points[-1].day, f"{lo:,.0f} ～ {hi:,.0f}")
