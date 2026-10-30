"""FX战士久留美 · 外汇保证金交易模拟插件 for AstrBot。

钱币还是「红豆币」，玩法换成了外汇保证金交易。

═══ 这个插件想表达什么 ═══

「FX战士久留美」这个名字来自那个著名的梗：一个把外汇当赌场、
杠杆开到最大、然后一路爆仓的战士。

所以这个插件的核心不是「怎么赚钱」，而是**仓位管理**：

  - 默认 1 手 = 1000 红豆币名义价值
  - 杠杆可以开到 500 倍（1000 倍是真实世界的自杀选项，这里不给）
  - 100 倍杠杆下，价格反向走 1% 就爆仓
  - 每 tick 检查权益，跌破保证金 50% 直接强平，余额清零

**低保兜底**：爆仓了也能每天领一笔，永远玩得下去。
这不是慈善，是让游戏不会因为一个人破产而结束。

指令（只有一个 /fx）：
    /fx                      行情
    /fx 多 <货币对> <手数> [杠杆]    做多
    /fx 空 <货币对> <手数> [杠杆]    做空
    /fx 平 <编号>             平仓
    /fx 全平                  一键平掉全部
    /fx 仓                    我的仓位
    /fx 榜                    富豪榜
    /fx 低保                  领低保
    /fx 看 <货币对>            单对详情
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from . import market
from .pairs import PAIRS, fmt_price, resolve

DEFAULTS: dict[str, Any] = {
    "tick_minutes": 5,
    "initial_cash": 1000,
    "low_income_amount": 300,
    "low_income_threshold": 300,
    "default_leverage": 100,
    "max_leverage": 500,
    "swap_rate_daily": 0.02,
}

MIN_TICK_MINUTES = 1
MAX_TICK_MINUTES = 120


@register(
    "astrbot_plugin_hongdou_stock",
    "hongdougao111",
    "FX战士久留美：红豆币外汇保证金交易，会爆仓，也有低保",
    "2.0.0",
)
class FxKurumiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] | None = None
        self._state_path: Path | None = None
        self._task: asyncio.Task | None = None
        self._last_save = 0.0
        self._diag: dict[str, Any] = {}

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_hongdou_stock")
        except Exception as e:
            logger.warning(f"[fx-kurumi] 取数据目录失败，改用插件目录: {e}")
            data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = data_dir / "fx.json"

        self._state = self._load()
        tick_sec = self._tick_seconds()

        try:
            n = market.catch_up(self._state, tick_sec, max_ticks=240)
            if n:
                logger.info(f"[fx-kurumi] 补算了 {n} 个行情周期")
        except Exception as e:
            logger.error(f"[fx-kurumi] 补算行情失败: {e}")

        self._save(force=True)
        self._diag = {
            "state_path": str(self._state_path),
            "users": len(self._state.get("users") or {}),
            "tick_count": self._state.get("tick_count", 0),
        }
        logger.info(
            f"[fx-kurumi] 已加载：{self._diag['users']} 个账户 / "
            f"{self._diag['tick_count']} 个周期"
        )

        self._task = asyncio.create_task(self._market_loop())

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._save(force=True)

    # ---------------- 存档 ----------------

    def _load(self) -> dict[str, Any]:
        p = self._state_path
        if p is None or not p.exists():
            return market.new_state()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"[fx-kurumi] 存档损坏，重新开市: {e}")
            return market.new_state()

        # 旧版（股票版）存档没有 quotes 字段，直接重开，不尝试迁移
        if not isinstance(raw, dict) or "quotes" not in raw:
            logger.warning("[fx-kurumi] 检测到旧版存档（股票版），已重置为外汇市场")
            return market.new_state()

        quotes = raw.setdefault("quotes", {})
        for p in PAIRS:
            quotes.setdefault(
                p.code,
                {"price": p.base, "open": p.base, "prev_close": p.base,
                 "history": [p.base]},
            )
        raw.setdefault("users", {})
        raw.setdefault("news", [])
        return raw

    def _save(self, force: bool = False) -> None:
        if self._state is None or self._state_path is None:
            return
        now = time.time()
        if not force and now - self._last_save < 10:
            return
        self._last_save = now
        tmp = self._state_path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)
        except OSError as e:
            logger.error(f"[fx-kurumi] 存档写入失败: {e}")

    # ---------------- 配置 ----------------

    def _cfg(self, key: str):
        default = DEFAULTS.get(key)
        if self.config is None:
            return default
        try:
            v = self.config.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def _cfg_num(self, key: str, lo: float, hi: float) -> float:
        try:
            v = float(self._cfg(key))
        except (TypeError, ValueError):
            v = float(DEFAULTS.get(key, lo))
        return max(lo, min(hi, v))

    def _tick_seconds(self) -> int:
        return int(self._cfg_num("tick_minutes", MIN_TICK_MINUTES, MAX_TICK_MINUTES) * 60)

    def _initial_cash(self) -> float:
        return self._cfg_num("initial_cash", 0, 10_000_000)

    def _low_income_amount(self) -> float:
        return self._cfg_num("low_income_amount", 0, 1_000_000)

    def _low_income_threshold(self) -> float:
        return self._cfg_num("low_income_threshold", 0, 100_000_000)

    def _default_leverage(self) -> int:
        return int(self._cfg_num("default_leverage", 1, 500))

    def _max_leverage(self) -> int:
        return int(self._cfg_num("max_leverage", 1, 500))

    def _swap_rate(self) -> float:
        """每日隔夜利息率（配置里是百分数）。"""
        return self._cfg_num("swap_rate_daily", 0, 1) / 100.0

    # ---------------- 行情循环 ----------------

    async def _market_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._tick_seconds())
                async with self._lock:
                    if self._state is None:
                        continue
                    news = market.tick_state(self._state, self._tick_seconds())
                    self._settle_swap()
                    liquidated = self._liquidate_all()
                    self._save()
                for line in liquidated:
                    logger.warning("[fx-kurumi] " + line)
                if news:
                    logger.info("[fx-kurumi] " + "；".join(news))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[fx-kurumi] 行情循环异常: {e}")
                await asyncio.sleep(30)

    def _settle_swap(self) -> None:
        """每天结算一次隔夜利息。"""
        if self._state is None:
            return
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if self._state.get("last_swap_day") == today:
            return
        self._state["last_swap_day"] = today
        paid = market.pay_swap(self._state, self._swap_rate(), now=now)
        if paid:
            logger.info(f"[fx-kurumi] 隔夜利息结算 {len(paid)} 个账户")

    def _liquidate_all(self) -> list[str]:
        """检查所有用户爆仓。"""
        if self._state is None:
            return []
        out: list[str] = []
        for uid, u in (self._state.get("users") or {}).items():
            msgs = market.check_liquidation(self._state, u)
            for m in msgs:
                out.append(f"{u.get('name', uid)} {m}")
        return out

    # ---------------- 指令 ----------------

    @filter.command("fx")
    async def fx(self, event: AstrMessageEvent, sub: str = "", arg1: str = "",
                 arg2: str = "", arg3: str = ""):
        """FX战士久留美。用法：/fx 多|空|平|全平|仓|榜|低保|看"""
        async with self._lock:
            if self._state is None:
                self._state = market.new_state()

            uid = event.get_sender_id() or "unknown"
            name = event.get_sender_name() or uid
            u = market.user_of(self._state, uid, name, self._initial_cash())

            text = await self._dispatch(event, u, sub.strip().lower(),
                                        arg1, arg2, arg3)
            self._save()
        yield event.plain_result(text)

    async def _dispatch(self, event: AstrMessageEvent, u: dict[str, Any],
                        sub: str, a1: str, a2: str, a3: str) -> str:
        assert self._state is not None
        st = self._state

        if sub in ("", "行情", "市", "q"):
            return self._render_market(st, u)
        if sub in ("多", "long", "l"):
            return self._do_open(st, u, a1, a2, a3, "long")
        if sub in ("空", "short", "s"):
            return self._do_open(st, u, a1, a2, a3, "short")
        if sub in ("平", "close", "c"):
            return self._do_close(st, u, a1)
        if sub in ("全平", "平全", "closeall", "ca"):
            return self._do_close_all(st, u)
        if sub in ("仓", "仓位", "pos"):
            return self._render_positions(st, u)
        if sub in ("榜", "排行", "rank"):
            return self._render_rank(st, u, event.get_sender_id() or "")
        if sub in ("低保", "welfare"):
            return self._do_low_income(st, u)
        if sub in ("看", "详情", "d"):
            return self._render_detail(st, a1)
        return (
            "不认识这个子命令。\n"
            "/fx 行情　/fx 多 USDJPY 0.5 100　/fx 空 XAUUSD 0.1\n"
            "/fx 平 1　/fx 全平　/fx 仓　/fx 榜　/fx 低保　/fx 看 USDJPY"
        )

    # ---------------- 渲染 ----------------

    def _render_market(self, st: dict[str, Any], u: dict[str, Any]) -> str:
        lines = ["🍁 FX战士久留美 · 实时报价", ""]
        for p in PAIRS:
            px = market.price_of(st, p.code)
            chg = market.change_pct(st, p.code)
            arrow = "▲" if chg > 0.00005 else ("▼" if chg < -0.00005 else "—")
            spark = market.sparkline(st, p.code, 10)
            lines.append(
                f"{p.name:<9}{fmt_price(p, px):>11}  {arrow}{chg * 100:+.2f}%  {spark}"
            )
        eq = market.equity(st, u)
        used = market.used_margin(st, u)
        lvl = market.margin_level(st, u)
        lines.append("")
        lines.append(f"权益 {eq:.2f}　占用保证金 {used:.2f}")
        if lvl is not None:
            warn = " ⚠️危险" if lvl < 200 else ""
            lines.append(f"保证金比例 {lvl:.0f}%{warn}")
        lines.append("开仓：/fx 多 USDJPY 0.5 100　（手数 杠杆）")
        return "\n".join(lines)

    def _render_detail(self, st: dict[str, Any], code: str) -> str:
        p = resolve(code)
        if p is None:
            return "没有这个货币对，用 /fx 看行情表。"
        px = market.price_of(st, p.code)
        chg = market.change_pct(st, p.code)
        hist = list((st.get("quotes", {}).get(p.code) or {}).get("history") or [])
        hi = max(hist) if hist else px
        lo = min(hist) if hist else px
        bid, ask = market.bid_ask(st, p.code)
        return (
            f"【{p.name}】{p.code}\n"
            f"中间价 {fmt_price(p, px)}　今日 {chg * 100:+.2f}%\n"
            f"买价 {fmt_price(p, bid)}　卖价 {fmt_price(p, ask)}\n"
            f"近期区间 {fmt_price(p, lo)} ~ {fmt_price(p, hi)}\n"
            f"走势 {market.sparkline(st, p.code, 24)}\n"
            f"{p.desc}"
        )

    def _render_positions(self, st: dict[str, Any], u: dict[str, Any]) -> str:
        pos_list = u.get("positions") or []
        bal = float(u.get("balance", 0.0))
        if not pos_list:
            return f"你还没有仓位。\n余额 {bal:.2f}　权益 {market.equity(st, u):.2f}"

        lines = ["🍁 我的仓位", ""]
        for i, pos in enumerate(pos_list, 1):
            p = resolve(pos.get("pair", ""))
            side = "多" if pos.get("side") == "long" else "空"
            pnl = market.position_pnl(st, pos)
            px = market.price_of(st, pos.get("pair", ""))
            digits = p.digits if p else 3
            mark = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"[{i}] {pos.get('pair')} {side} {pos.get('lots', 0):g}手 "
                f"{pos.get('leverage')}x {mark}{pnl:+.2f}"
            )
            lines.append(
                f"    开 {pos.get('entry', 0):.{digits}f} → 现 {px:.{digits}f}　"
                f"保证金 {pos.get('margin', 0):.2f}"
            )

        eq = market.equity(st, u)
        used = market.used_margin(st, u)
        lvl = market.margin_level(st, u)
        lines.append("")
        lines.append(f"余额 {bal:.2f}　浮动 {market.floating_pnl(st, u):+.2f}")
        lines.append(f"权益 {eq:.2f}　占用 {used:.2f}　可用 {market.free_margin(st, u):.2f}")
        if lvl is not None:
            warn = "　⚠️ 保证金不足，随时可能爆仓" if lvl < 150 else ""
            lines.append(f"保证金比例 {lvl:.0f}%{warn}")
        return "\n".join(lines)

    def _render_rank(self, st: dict[str, Any], u: dict[str, Any],
                     my_uid: str) -> str:
        rows = market.ranking(st, 10)
        if not rows:
            return "还没有人入市。"
        lines = ["🍁 久留美富豪榜", ""]
        for i, (uid, nm, assets) in enumerate(rows, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            me = " ←你" if uid == my_uid else ""
            lines.append(f"{medal} {nm}  {assets:.2f}{me}")
        return "\n".join(lines)

    # ---------------- 开平仓 ----------------

    def _do_open(self, st: dict[str, Any], u: dict[str, Any],
                 code: str, lots_s: str, lev_s: str, side: str) -> str:
        word = "多" if side == "long" else "空"
        if not code or not lots_s:
            return (
                f"用法：/fx {word} 货币对 手数 [杠杆]\n"
                f"例：/fx {word} USDJPY 0.5 100"
            )
        p = resolve(code)
        if p is None:
            return f"没有 {code} 这个货币对。"

        try:
            lots = float(lots_s)
        except (TypeError, ValueError):
            return "手数要写数字，比如 0.5。"

        # 杠杆：没填就用默认值
        if lev_s.strip():
            try:
                lev = int(float(lev_s))
            except (TypeError, ValueError):
                return "杠杆要写整数，比如 100。"
        else:
            lev = self._default_leverage()

        cap = self._max_leverage()
        if lev > cap:
            return f"杠杆最高 {cap} 倍。想再高？那是在赌命。"
        if lev < 1:
            return "杠杆至少 1 倍。"

        ok, msg = market.open_position(st, u, p.code, side, lots, lev)
        if not ok:
            return msg
        # 开仓后立刻检查一次爆仓（极端情况）
        liq = market.check_liquidation(st, u)
        if liq:
            return msg + "\n" + "\n".join(liq)
        return msg

    def _do_close(self, st: dict[str, Any], u: dict[str, Any], idx_s: str) -> str:
        if not idx_s:
            return "用法：/fx 平 编号　（编号看 /fx 仓）"
        try:
            idx = int(idx_s)
        except (TypeError, ValueError):
            return "编号要写数字，比如 1。"
        ok, msg = market.close_position(st, u, idx - 1)
        if not ok:
            return msg
        return msg + f"\n余额 {float(u.get('balance', 0.0)):.2f}"

    def _do_close_all(self, st: dict[str, Any], u: dict[str, Any]) -> str:
        pos_list = u.get("positions") or []
        if not pos_list:
            return "你没有仓位。"
        n = len(pos_list)
        lines = []
        # 倒序平，避免索引错位
        for i in range(n - 1, -1, -1):
            ok, msg = market.close_position(st, u, i, reason="一键平仓")
            if ok:
                lines.append(msg)
        return f"平掉 {len(lines)} 个仓位\n" + "\n".join(lines) + \
               f"\n余额 {float(u.get('balance', 0.0)):.2f}"

    # ---------------- 低保 ----------------

    def _do_low_income(self, st: dict[str, Any], u: dict[str, Any]) -> str:
        threshold = self._low_income_threshold()
        ok, why = market.can_low_income(st, u, threshold)
        if not ok:
            return why
        amount = self._low_income_amount()
        if amount <= 0:
            return "管理员把低保关掉了，自求多福吧。"
        msg = market.grant_low_income(st, u, amount)
        return f"🍚 {msg}\n余额 {float(u.get('balance', 0.0)):.2f}"

    # ---------------- 诊断 ----------------

    @filter.command("fx诊断")
    async def diagnose(self, event: AstrMessageEvent):
        """检查 FX战士久留美 运行状态。"""
        st = self._state or {}
        users = len(st.get("users") or {})
        ticks = int(st.get("tick_count", 0))
        last = float(st.get("last_tick", 0.0))
        ago = int(time.time() - last) if last else -1
        yield event.plain_result(
            "【FX战士久留美 诊断】\n"
            f"存档：{self._diag.get('state_path', '(未初始化)')}\n"
            f"账户 {users} 个　行情周期 {ticks} 次\n"
            f"上次行情：{ago} 秒前　间隔 {self._tick_seconds()} 秒\n"
            f"默认杠杆 {self._default_leverage()}x　上限 {self._max_leverage()}x\n"
            f"强平线：权益 < 保证金 × {market.MARGIN_CALL_RATIO * 100:.0f}%\n"
            f"低保 {self._low_income_amount():.0f}（权益低于 "
            f"{self._low_income_threshold():.0f} 可领，每天一次）\n"
            f"行情循环：{'运行中' if self._task and not self._task.done() else '已停止'}"
        )
