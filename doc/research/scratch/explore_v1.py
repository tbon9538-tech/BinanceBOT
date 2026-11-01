"""一次性脚本：推荐第一版（核心 BTC 趋势 + 卫星山寨动量）的探索性估算。
注意：配置是在看过 explore_backtest 结果之后确定的，样本内数字偏乐观，只用于判断方向。
用法（项目根目录）：
    py -3.13 -I doc/research/scratch/explore_v1.py doc/research/scratch/data doc/research/scratch/out
"""
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import explore_backtest as eb  # noqa: E402
import explore_extra as ex  # noqa: E402


def main():
    data_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    pre = eb.Pre(eb.load_panel(data_dir))
    sat = replace(eb.P(), name="rec_v1_satellite", universe_mode="rank", universe_size=30, rank_floor=5e6,
                  skip=0, target_vol=0.30, stop=False, ramp_on=True, exclude=("BTCUSDT",))
    lines = []
    for v in [sat, replace(sat, name="rec_v1_sat_with_stop", stop=True),
              replace(sat, name="rec_v1_sat_tv0.25", target_vol=0.25)]:
        eq, g, to = eb.run(pre, v)
        s = eb.stats(eq, g, to)
        lines.append(v.name + ": " + " ".join(f"{k}={x:.3f}" for k, x in s.items()))
        if v.name == "rec_v1_satellite":
            sat_r = eq.pct_change().dropna()
    core = ex.btc_trend_vt(data_dir, sat_r.index[0], sat_r.index[-1], 0.30)
    core.index = pd.to_datetime(core.index)
    for w in (1.0, 0.7, 0.5, 0.3, 0.0):
        r = (w * core.reindex(sat_r.index).fillna(0) + (1 - w) * sat_r).dropna()
        s = ex.stats(r)
        yearly = (1 + r).groupby(r.index.year).prod() - 1
        h1, h2 = ex.stats(r.loc[:"2022-09-30"]), ex.stats(r.loc["2022-10-01":])
        lines.append(f"core_w={w:.1f}: " + " ".join(f"{k}={v:.3f}" for k, v in s.items())
                     + " | " + " ".join(f"{y}:{v:.1%}" for y, v in yearly.items())
                     + f" | H1 SR={h1['Sharpe']:.2f} MDD={h1['MDD']:.1%} H2 SR={h2['Sharpe']:.2f} MDD={h2['MDD']:.1%}")
    text = "\n".join(lines)
    print(text)
    (out_dir / "explore_v1.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
