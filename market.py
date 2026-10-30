"""红豆币爆仓模拟器 · 交易引擎。

纯逻辑、纯同步、不碰 AstrBot，方便单测。插件层负责加锁和落盘。

═══ 和股票最大的区别 ═══

股票是「有多少钱买多少货」，外汇是**保证金交易**：

    你只需要交一小部分保证金，就能开一个比本金大得多的仓位。

默认 1 手 = 1000 红豆币名义价值，100 倍杠杆 → 开 1 手只要 10 红豆币保证金。
听起来很爽，但反过来：**价格反向走 1%，你的保证金就没了**。

所以这个游戏的核心不是「涨跌」，是**仓位管理**。

═══ 爆仓机制 ═══

每 tick 检查一次：

    权益 = 余额 + 所有持仓的浮动盈亏
    占用保证金 = Σ 各仓位保证金

    当 权益 < 占用保证金 × 强平线（默认 50%）→ 强制平仓，余额清零

强平线 50% 意味着：亏损达到保证金的一半就会被强平。
100 倍杠杆下，价格反向走 0.5% 就爆。

═══ 隔夜利息（Swap）═══

持仓过夜要付/收利息，按名义价值计算。
正数收钱、负数付钱，模拟真实市场的掉期。
这是防止玩家「开了不管」的机制。
"""
from __future__ import annotations

import math
import random
import time
from typing import Any

from .pairs import PAIRS, Pair, resolve

# ---- 合约与交易规则 ----
CONTRACT_SIZE = 1000.0     # 1 手 = 1000 红豆币名义价值
LOT_MIN = 0.01             # 最小 0.01 手
LOT_MAX = 100.0            # 单笔最大 100 手
SPREAD_RATIO = 0.00012     # 点差：单边 0.012%，来回 0.024%
COMMISSION_RATE = 0.0002   # 手续费 0.02% 名义价值，开平各收一次

# ---- 强平 ----
MARGIN_CALL_RATIO = 0.50   # 权益低于保证金的 50% 就强平

# ---- 历史 ----
HISTORY_KEEP = 288


def new_state(now: float | None = None) -> dict[str, Any]:
    """全新市场。"""
    t = now if now is not None else time.time()
    quotes: dict[str, dict[str, Any]] = {}
    for p in PAIRS:
        quotes[p.code] = {
            "price": p.base,
            "open": p.base,
            "prev_close": p.base,
            "history": [p.base],
        }
    return {
        "version": 2,
        "created_at": t,
        "last_tick": t,
        "tick_count": 0,
        "day": _day_key(t),
        "quotes": quotes,
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
    """推进一个 tick，返回异动新闻。"""
    r = rng or random.Random()
    t = now if now is not None else time.time()
    sq = math.sqrt(ticks_per_day(tick_seconds))

    news: list[str] = []
    today = _day_key(t)

    for p in PAIRS:
        q = state["quotes"].setdefault(
            p.code,
            {"price": p.base, "open": p.base, "prev_close": p.base,
             "history": [p.base]},
        )
        price = float(q.get("price", p.base))
        sigma = p.daily_vol / sq
        # 外汇没有长期漂移，纯随机游走
        shock = r.gauss(0.0, 1.0)
        new_price = price * math.exp(sigma * shock)

        # 单 tick 最大 3%，外汇极少出现更大的单 tick 波动
        new_price = max(price * 0.97, min(price * 1.03, new_price))
        q["price"] = round(new_price, p.digits + 1)
        hist = q.setdefault("history", [])
        hist.append(q["price"])
        if len(hist) > HISTORY_KEEP:
            del hist[: len(hist) - HISTORY_KEEP]

        chg = (q["price"] - price) / price if price > 0 else 0.0
        if abs(chg) >= 0.012:
            arrow = "📈" if chg > 0 else "📉"
            news.append(
                f"{arrow} {p.name} 剧烈波动 {chg * 100:+.2f}%，"
                f"现价 {q['price']:.{p.digits}f}"
            )

    if today != state.get("day"):
        for code, q in state["quotes"].items():
            q["prev_close"] = q.get("price", 0.0)
            q["open"] = q.get("price", 0.0)
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
    """补算停机期间漏掉的 tick，有上限。"""
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
# 报价
# ---------------------------------------------------------------------------

def price_of(state: dict[str, Any], code: str) -> float:
    q = state.get("quotes", {}).get(code)
    if not q:
        return 0.0
    return float(q.get("price", 0.0))


def bid_ask(state: dict[str, Any], code: str) -> tuple[float, float]:
    """买卖价。做多按 ask 开、bid 平；做空反过来。"""
    mid = price_of(state, code)
    half = mid * SPREAD_RATIO / 2
    return mid - half, mid + half


def change_pct(state: dict[str, Any], code: str) -> float:
    q = state.get("quotes", {}).get(code)
    if not q:
        return 0.0
    prev = float(q.get("prev_close") or q.get("price") or 0.0)
    cur = float(q.get("price") or 0.0)
    if prev <= 0:
        return 0.0
    return (cur - prev) / prev


def sparkline(state: dict[str, Any], code: str, width: int = 12) -> str:
    q = state.get("quotes", {}).get(code)
    if not q:
        return "▁" * width
    hist = list(q.get("history") or [])
    if len(hist) < 2:
        return "▁" * width
    hist = hist[-width:]
    lo, hi = min(hist), max(hist)
    blocks = "▁▂▃▄▅▆▇█"
    if hi - lo < 1e-12:
        return blocks[3] * len(hist)
    out = []
    for v in hist:
        idx = int((v - lo) / (hi - lo) * (len(blocks) - 1))
        out.append(blocks[max(0, min(len(blocks) - 1, idx))])
    return "".join(out)


# ---------------------------------------------------------------------------
# 账户
# ---------------------------------------------------------------------------

def user_of(
    state: dict[str, Any],
    uid: str,
    name: str = "",
    initial_cash: float = 0.0,
    now: float | None = None,
) -> dict[str, Any]:
    """取用户，没有就开户。"""
    users = state.setdefault("users", {})
    u = users.get(uid)
    if u is None:
        t = now if now is not None else time.time()
        u = {
            "name": name or uid,
            "balance": float(initial_cash),
            "positions": [],
            "created_at": t,
            "last_low_income": None,
            "last_swap": t,
            "trades": 0,
            "liquidations": 0,
            "realized": 0.0,
        }
        users[uid] = u
    elif name:
        u["name"] = name
    return u


def position_pnl(state: dict[str, Any], pos: dict[str, Any]) -> float:
    """单个仓位的浮动盈亏（红豆币）。"""
    pair = resolve(pos.get("pair", ""))
    if pair is None:
        return 0.0
    entry = float(pos.get("entry", 0.0))
    cur = price_of(state, pair.code)
    if entry <= 0 or cur <= 0:
        return 0.0
    notional = float(pos.get("notional", 0.0))
    direction = 1 if pos.get("side") == "long" else -1
    return notional * (cur - entry) / entry * direction


def floating_pnl(state: dict[str, Any], u: dict[str, Any]) -> float:
    return sum(position_pnl(state, p) for p in (u.get("positions") or []))


def used_margin(state: dict[str, Any], u: dict[str, Any]) -> float:
    return sum(float(p.get("margin", 0.0)) for p in (u.get("positions") or []))


def equity(state: dict[str, Any], u: dict[str, Any]) -> float:
    """权益 = 余额 + 浮动盈亏。"""
    return float(u.get("balance", 0.0)) + floating_pnl(state, u)


def free_margin(state: dict[str, Any], u: dict[str, Any]) -> float:
    return equity(state, u) - used_margin(state, u)


def margin_level(state: dict[str, Any], u: dict[str, Any]) -> float | None:
    """保证金比例（%）。没有持仓返回 None。"""
    used = used_margin(state, u)
    if used <= 0:
        return None
    return equity(state, u) / used * 100.0


def total_assets(state: dict[str, Any], u: dict[str, Any]) -> float:
    """总资产，用于排行和低保判定。"""
    return equity(state, u)


# ---------------------------------------------------------------------------
# 开仓 / 平仓
# ---------------------------------------------------------------------------

def open_position(
    state: dict[str, Any],
    u: dict[str, Any],
    code: str,
    side: str,
    lots: float,
    leverage: int,
) -> tuple[bool, str]:
    """开仓。side: long / short。"""
    pair = resolve(code)
    if pair is None:
        return False, "没有这个货币对。"
    if side not in ("long", "short"):
        return False, "方向只能是 做多(long) 或 做空(short)。"
    if lots < LOT_MIN:
        return False, f"最小 {LOT_MIN} 手。"
    if lots > LOT_MAX:
        return False, f"单笔最大 {LOT_MAX} 手，别太贪。"
    if leverage < 1 or leverage > 500:
        return False, "杠杆要在 1-500 之间。"

    notional = lots * CONTRACT_SIZE
    margin = notional / leverage
    commission = notional * COMMISSION_RATE

    bal = float(u.get("balance", 0.0))
    need = margin + commission
    free = free_margin(state, u)
    if free < need:
        return False, (
            f"可用保证金不够。需要 {need:.2f}（保证金 {margin:.2f} + "
            f"手续费 {commission:.2f}），你只有 {free:.2f}。"
        )

    bid, ask = bid_ask(state, pair.code)
    entry = ask if side == "long" else bid

    u["balance"] = round(bal - commission, 4)
    u.setdefault("positions", []).append({
        "pair": pair.code,
        "side": side,
        "lots": lots,
        "entry": entry,
        "notional": notional,
        "margin": margin,
        "leverage": leverage,
        "opened_at": time.time(),
    })
    u["trades"] = int(u.get("trades", 0)) + 1

    word = "做多" if side == "long" else "做空"
    return True, (
        f"{word} {pair.name} {lots:g} 手 @ {entry:.{pair.digits}f}\n"
        f"名义 {notional:.0f}　{leverage}x　保证金 {margin:.2f}　"
        f"手续费 {commission:.2f}\n"
        f"价格反向走 {100 / leverage:.2f}% 就爆仓"
    )


def close_position(
    state: dict[str, Any],
    u: dict[str, Any],
    index: int,
    reason: str = "手动平仓",
) -> tuple[bool, str]:
    """按索引平仓。"""
    pos_list = u.get("positions") or []
    if index < 0 or index >= len(pos_list):
        return False, "没有这个仓位的编号。"
    pos = pos_list[index]
    pair = resolve(pos.get("pair", ""))
    if pair is None:
        pos_list.pop(index)
        return False, "仓位数据异常，已清除。"

    pnl = position_pnl(state, pos)
    notional = float(pos.get("notional", 0.0))
    commission = notional * COMMISSION_RATE
    margin = float(pos.get("margin", 0.0))

    # 平仓时把保证金还回去，加上盈亏，扣掉手续费
    net = margin + pnl - commission
    # 亏损超过保证金的部分，从余额里扣（最多扣到余额为 0）
    if net < 0:
        net = max(-float(u.get("balance", 0.0)), net)

    u["balance"] = round(float(u.get("balance", 0.0)) + net, 4)
    u["realized"] = round(float(u.get("realized", 0.0)) + pnl - commission, 4)
    pos_list.pop(index)

    side = "多" if pos.get("side") == "long" else "空"
    pnl_word = "赚" if pnl >= 0 else "亏"
    return True, (
        f"{reason}：平掉 {pair.name} {side} {pos.get('lots', 0):g} 手\n"
        f"浮动盈亏 {pnl:+.2f}　手续费 {commission:.2f}　"
        f"净结算 {net:+.2f}　{pnl_word}了 {abs(pnl):.2f}"
    )


def check_liquidation(
    state: dict[str, Any],
    u: dict[str, Any],
) -> list[str]:
    """检查爆仓，触发则清空所有仓位。返回说明文字。"""
    pos_list = u.get("positions") or []
    if not pos_list:
        return []
    used = used_margin(state, u)
    if used <= 0:
        return []
    eq = equity(state, u)
    if eq >= used * MARGIN_CALL_RATIO:
        return []

    # 爆仓：所有仓位按当前价强平，余额清零
    total_pnl = floating_pnl(state, u)
    u["positions"] = []
    u["balance"] = 0.0
    u["liquidations"] = int(u.get("liquidations", 0)) + 1
    u["realized"] = round(float(u.get("realized", 0.0)) + total_pnl, 4)

    return [
        f"💥 爆仓！权益 {eq:.2f} 低于保证金 {used:.2f} 的 "
        f"{MARGIN_CALL_RATIO * 100:.0f}%，全部仓位被强制平仓，余额清零。"
    ]


# ---------------------------------------------------------------------------
# 隔夜利息
# ---------------------------------------------------------------------------

def pay_swap(
    state: dict[str, Any],
    rate_per_day: float,
    now: float | None = None,
) -> list[tuple[str, float]]:
    """对所有持仓收/付隔夜利息，返回 [(uid, 净额)]。

    rate_per_day 是「每日名义价值的比例」。
    做多付利息、做空收利息 —— 真实市场里美元多头通常付息，
    这里统一成：多头付、空头收，简单好懂。
    """
    out: list[tuple[str, float]] = []
    for uid, u in (state.get("users") or {}).items():
        pos_list = u.get("positions") or []
        if not pos_list:
            continue
        total = 0.0
        for p in pos_list:
            notional = float(p.get("notional", 0.0))
            amt = notional * rate_per_day
            if p.get("side") == "long":
                total -= amt      # 多头付
            else:
                total += amt      # 空头收
        if abs(total) < 0.01:
            continue
        u["balance"] = round(float(u.get("balance", 0.0)) + total, 4)
        u["last_swap"] = now if now is not None else time.time()
        out.append((uid, total))
    return out


# ---------------------------------------------------------------------------
# 低保
# ---------------------------------------------------------------------------

def can_low_income(
    state: dict[str, Any],
    u: dict[str, Any],
    threshold: float,
    now: float | None = None,
) -> tuple[bool, str]:
    t = now if now is not None else time.time()
    today = _day_key(t)
    if u.get("last_low_income") == today:
        return False, "今天已经领过了，明天再来。"
    assets = total_assets(state, u)
    if assets > threshold:
        return False, (
            f"低保是给爆仓的人的。你现在权益 {assets:.0f}，"
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
    u["balance"] = round(float(u.get("balance", 0.0)) + amount, 4)
    u["last_low_income"] = _day_key(t)
    return f"低保到账 {amount:.0f} 红豆币。别急着开 100 倍，先想清楚。"


# ---------------------------------------------------------------------------
# 排行
# ---------------------------------------------------------------------------

def ranking(state: dict[str, Any], limit: int = 10) -> list[tuple[str, str, float]]:
    rows = []
    for uid, u in (state.get("users") or {}).items():
        rows.append((uid, str(u.get("name", uid)), total_assets(state, u)))
    rows.sort(key=lambda x: x[2], reverse=True)
    return rows[:limit]
