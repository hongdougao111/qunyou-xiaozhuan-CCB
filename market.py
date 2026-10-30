"""红豆股市 · 市场引擎。

纯逻辑、纯同步、不碰 AstrBot，方便单测。插件层负责加锁和落盘。

核心概念：
  tick      一次行情刷新。默认 5 分钟一次，一天 288 次。
  价格模型  几何随机游走 + 每日漂移 + 软边界反弹。
            每 tick 波动 = 日波动率 / sqrt(一天多少 tick)，
            所以「日波动率」这个参数是直觉可读的。
  软边界    价格跌破 base*0.2 时漂移转正、涨过 base*5 时漂移转负，
            既不会跌成 0 卡死，也不会无限膨胀。

所有金额单位都是「红豆币」，内部用 float，展示时取整。
"""
from __future__ import annotations

import math
import random
import time
from typing import Any

from .stocks import STOCKS, Stock, resolve

# ---- 交易规则常量（改这里，不是配置项，避免玩家乱调）----
FEE_RATE = 0.0015          # 手续费 0.15%
FEE_MIN = 1.0              # 最低手续费 1 红豆币
PRICE_FLOOR = 0.5          # 绝对价格下限
SHARES_MIN = 1             # 最少买 1 股
SHARES_MAX_ORDER = 1_000_000

# ---- 软边界 ----
LOW_BAND = 0.20
HIGH_BAND = 5.00
BAND_PUSH = 0.006          # 触边后每 tick 额外施加的漂移

# ---- 历史 ----
HISTORY_KEEP = 288         # 每只股票保留最近 288 个 tick（约一天）


def new_state(now: float | None = None) -> dict[str, Any]:
    """全新市场状态。"""
    t = now if now is not None else time.time()
    stocks: dict[str, dict[str, Any]] = {}
    for s in STOCKS:
        stocks[s.code] = {
            "price": s.base_price,
            "open": s.base_price,
            "prev_close": s.base_price,
            "history": [s.base_price],
            "volume": 0,
        }
    return {
        "version": 1,
        "created_at": t,
        "last_tick": t,
        "tick_count": 0,
        "day": _day_key(t),
        "stocks": stocks,
        "users": {},
        "news": [],
    }


def _day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# ---------------------------------------------------------------------------
# 行情推进
# ---------------------------------------------------------------------------

def ticks_per_day(tick_seconds: int) -> int:
    return max(1, int(round(86400 / max(60, tick_seconds))))


def tick_state(
    state: dict[str, Any],
    tick_seconds: int,
    rng: random.Random | None = None,
    now: float | None = None,
) -> list[str]:
    """推进一个 tick，返回值得公告的新闻（涨跌幅超阈值的）。

    不负责补算漏掉的 tick —— 那是 catch_up 的事。
    """
    r = rng or random.Random()
    t = now if now is not None else time.time()
    n = ticks_per_day(tick_seconds)
    sq = math.sqrt(n)

    news: list[str] = []
    today = _day_key(t)

    for s in STOCKS:
        st = state["stocks"].setdefault(
            s.code,
            {"price": s.base_price, "open": s.base_price,
             "prev_close": s.base_price, "history": [], "volume": 0},
        )
        price = float(st.get("price", s.base_price))
        base = s.base_price

        # 软边界：把价格往区间内推
        band_drift = 0.0
        if price < base * LOW_BAND:
            band_drift = BAND_PUSH
        elif price > base * HIGH_BAND:
            band_drift = -BAND_PUSH

        mu = s.daily_drift / n + band_drift
        sigma = s.daily_vol / sq
        shock = r.gauss(0.0, 1.0)

        new_price = price * math.exp(mu + sigma * shock)
        if new_price < PRICE_FLOOR:
            new_price = PRICE_FLOOR
        # 单 tick 最大涨跌 12%，防止极端随机数把价格撕碎
        new_price = max(price * 0.88, min(price * 1.12, new_price))

        st["price"] = round(new_price, 2)
        hist = st.setdefault("history", [])
        hist.append(st["price"])
        if len(hist) > HISTORY_KEEP:
            del hist[: len(hist) - HISTORY_KEEP]

        chg = (st["price"] - price) / price if price > 0 else 0.0
        if abs(chg) >= 0.08:
            arrow = "📈" if chg > 0 else "📉"
            news.append(
                f"{arrow} {s.name}({s.code}) 单次{'暴涨' if chg > 0 else '暴跌'} "
                f"{abs(chg) * 100:.1f}%，现价 {st['price']:.2f}"
            )

    # 跨日：结算开盘价与昨收
    if today != state.get("day"):
        for code, st in state["stocks"].items():
            st["prev_close"] = st.get("price", 0.0)
            st["open"] = st.get("price", 0.0)
        state["day"] = today

    state["last_tick"] = t
    state["tick_count"] = int(state.get("tick_count", 0)) + 1

    if news:
        log = state.setdefault("news", [])
        log.extend(news)
        del log[:-40]
    return news


def catch_up(
    state: dict[str, Any],
    tick_seconds: int,
    max_ticks: int = 240,
    now: float | None = None,
) -> int:
    """停机期间漏掉的 tick 补算，最多补 max_ticks 个。

    为什么要设上限：服务器关了一周，补 2000 个 tick 会卡住启动，
    而且价格早就随机漂到没意义的地方了。补一小段意思一下即可。
    """
    t = now if now is not None else time.time()
    last = float(state.get("last_tick", t))
    step = max(60, tick_seconds)
    missed = int((t - last) // step)
    if missed <= 0:
        return 0
    n = min(missed, max_ticks)
    for _ in range(n):
        tick_state(state, tick_seconds, now=last + step)
        last += step
    state["last_tick"] = t
    return n


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def price_of(state: dict[str, Any], code: str) -> float:
    st = state.get("stocks", {}).get(code)
    if not st:
        return 0.0
    return float(st.get("price", 0.0))


def change_pct(state: dict[str, Any], code: str) -> float:
    """当日涨跌幅（相对昨收）。"""
    st = state.get("stocks", {}).get(code)
    if not st:
        return 0.0
    prev = float(st.get("prev_close") or st.get("price") or 0.0)
    cur = float(st.get("price") or 0.0)
    if prev <= 0:
        return 0.0
    return (cur - prev) / prev


def sparkline(state: dict[str, Any], code: str, width: int = 12) -> str:
    """把最近的价格画成一条迷你走势线。"""
    st = state.get("stocks", {}).get(code)
    if not st:
        return "▁" * width
    hist = list(st.get("history") or [])
    if len(hist) < 2:
        return "▁" * width
    hist = hist[-width:]
    lo, hi = min(hist), max(hist)
    blocks = "▁▂▃▄▅▆▇█"
    if hi - lo < 1e-9:
        return blocks[3] * len(hist)
    out = []
    for p in hist:
        idx = int((p - lo) / (hi - lo) * (len(blocks) - 1))
        out.append(blocks[max(0, min(len(blocks) - 1, idx))])
    return "".join(out)


# ---------------------------------------------------------------------------
# 用户与交易
# ---------------------------------------------------------------------------

def user_of(
    state: dict[str, Any],
    uid: str,
    name: str = "",
    initial_cash: float = 0.0,
    now: float | None = None,
) -> dict[str, Any]:
    """取用户，没有就开户（送 initial_cash）。"""
    users = state.setdefault("users", {})
    u = users.get(uid)
    if u is None:
        t = now if now is not None else time.time()
        u = {
            "name": name or uid,
            "cash": float(initial_cash),
            "holdings": {},
            "cost": {},
            "created_at": t,
            "last_low_income": None,
            "last_dividend": t,
            "trades": 0,
            "realized": 0.0,
        }
        users[uid] = u
    elif name:
        u["name"] = name
    return u


def holding_value(state: dict[str, Any], u: dict[str, Any]) -> float:
    total = 0.0
    for code, sh in (u.get("holdings") or {}).items():
        if sh > 0:
            total += price_of(state, code) * sh
    return total


def total_assets(state: dict[str, Any], u: dict[str, Any]) -> float:
    return float(u.get("cash", 0.0)) + holding_value(state, u)


def _fee(amount: float) -> float:
    return max(FEE_MIN, amount * FEE_RATE)


def buy(
    state: dict[str, Any],
    u: dict[str, Any],
    code: str,
    shares: int,
) -> tuple[bool, str]:
    """买入。返回 (成功?, 说明文字)。"""
    stock = resolve(code)
    if stock is None:
        return False, "没有这只股票。"
    if shares < SHARES_MIN:
        return False, f"最少买 {SHARES_MIN} 股。"
    if shares > SHARES_MAX_ORDER:
        return False, "单笔太大了，分批买吧。"

    px = price_of(state, stock.code)
    if px <= 0:
        return False, "这只票现在没报价。"
    amount = px * shares
    fee = _fee(amount)
    need = amount + fee
    cash = float(u.get("cash", 0.0))
    if cash < need:
        return False, (
            f"红豆币不够。需要 {need:.0f}（含手续费 {fee:.0f}），"
            f"你只有 {cash:.0f}。"
        )

    u["cash"] = round(cash - need, 2)
    hold = u.setdefault("holdings", {})
    hold[stock.code] = int(hold.get(stock.code, 0)) + shares
    cost = u.setdefault("cost", {})
    cost[stock.code] = round(float(cost.get(stock.code, 0.0)) + need, 2)
    u["trades"] = int(u.get("trades", 0)) + 1
    return True, (
        f"买入 {stock.name} {shares} 股 @ {px:.2f}，"
        f"花费 {amount:.0f} + 手续费 {fee:.0f} = {need:.0f} 红豆币。"
    )


def sell(
    state: dict[str, Any],
    u: dict[str, Any],
    code: str,
    shares: int,
) -> tuple[bool, str]:
    """卖出。返回 (成功?, 说明文字)。"""
    stock = resolve(code)
    if stock is None:
        return False, "没有这只股票。"
    hold = u.setdefault("holdings", {})
    owned = int(hold.get(stock.code, 0))
    if owned <= 0:
        return False, f"你没有 {stock.name} 的持仓。"
    if shares < SHARES_MIN:
        return False, f"最少卖 {SHARES_MIN} 股。"
    if shares > owned:
        return False, f"你只有 {owned} 股，卖不了 {shares} 股。"

    px = price_of(state, stock.code)
    gross = px * shares
    fee = _fee(gross)
    net = gross - fee

    # 成本按股数比例结转，用来算已实现盈亏
    cost = u.setdefault("cost", {})
    total_cost = float(cost.get(stock.code, 0.0))
    part_cost = total_cost * (shares / owned) if owned else 0.0
    u["realized"] = round(
        float(u.get("realized", 0.0)) + (net - part_cost), 2
    )
    cost[stock.code] = round(max(0.0, total_cost - part_cost), 2)

    hold[stock.code] = owned - shares
    if hold[stock.code] <= 0:
        hold.pop(stock.code, None)
        cost.pop(stock.code, None)

    u["cash"] = round(float(u.get("cash", 0.0)) + net, 2)
    u["trades"] = int(u.get("trades", 0)) + 1

    pnl = net - part_cost
    sign = "赚" if pnl >= 0 else "亏"
    return True, (
        f"卖出 {stock.name} {shares} 股 @ {px:.2f}，"
        f"到手 {net:.0f}（手续费 {fee:.0f}），{sign}了 {abs(pnl):.0f} 红豆币。"
    )


def can_low_income(
    state: dict[str, Any],
    u: dict[str, Any],
    threshold: float,
    now: float | None = None,
) -> tuple[bool, str]:
    """能不能领低保。"""
    t = now if now is not None else time.time()
    today = _day_key(t)
    if u.get("last_low_income") == today:
        return False, "今天已经领过了，明天再来。"
    assets = total_assets(state, u)
    if assets > threshold:
        return False, (
            f"低保是给穷人的。你现在身家 {assets:.0f}，"
            f"超过 {threshold:.0f} 就领不了了。"
        )
    return True, ""


def grant_low_income(
    state: dict[str, Any],
    u: dict[str, Any],
    amount: float,
    now: float | None = None,
) -> str:
    t = now if now is not None else time.time()
    u["cash"] = round(float(u.get("cash", 0.0)) + amount, 2)
    u["last_low_income"] = _day_key(t)
    return f"低保到账 {amount:.0f} 红豆币，好好活着，别乱赌。"


def pay_dividends(
    state: dict[str, Any],
    rate: float,
    now: float | None = None,
) -> list[tuple[str, float]]:
    """给所有持仓用户分红，返回 [(uid, 金额)]。

    rate 是「每 tick 分红率」，插件层按分红周期换算后传入。
    """
    t = now if now is not None else time.time()
    out: list[tuple[str, float]] = []
    for uid, u in (state.get("users") or {}).items():
        hv = holding_value(state, u)
        if hv <= 0:
            continue
        amt = round(hv * rate, 2)
        if amt < 1:
            continue
        u["cash"] = round(float(u.get("cash", 0.0)) + amt, 2)
        u["last_dividend"] = t
        out.append((uid, amt))
    return out


def ranking(state: dict[str, Any], limit: int = 10) -> list[tuple[str, str, float]]:
    """按总资产排行，返回 [(uid, 名字, 总资产)]。"""
    rows = []
    for uid, u in (state.get("users") or {}).items():
        rows.append((uid, str(u.get("name", uid)), total_assets(state, u)))
    rows.sort(key=lambda x: x[2], reverse=True)
    return rows[:limit]
