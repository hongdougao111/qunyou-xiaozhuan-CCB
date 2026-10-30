"""红豆股市 · 模拟炒股插件 for AstrBot。

钱币叫「红豆币」。

设计取向（为什么这么写）：
  - **指令少**：只有 /股 一个指令，后面跟子命令。避免指令表刷屏。
  - **低保兜底**：身无分文也能每天领一笔，永远不至于「玩不下去了」。
  - **状态全落盘**：所有玩家数据存 data/plugin_data/ 下，重启不丢。
  - **纯本地模拟**：不联网、不请求任何真实行情，价格由随机游走生成。

指令：
    /股                     行情总览
    /股 买 <代码> <股数>      买入
    /股 卖 <代码> <股数>      卖出
    /股 仓                   我的持仓
    /股 榜                   富豪榜
    /股 低保                 领低保
    /股 看 <代码>            单只详情

没有任何 AstrBot 时也能跑 test_plugin.py 验证逻辑。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from . import market
from .stocks import STOCKS, resolve

# ---------------------------------------------------------------------------
# 配置默认值（与 _conf_schema.json 保持一致）
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "tick_minutes": 5,
    "initial_cash": 1000,
    "low_income_amount": 200,
    "low_income_threshold": 300,
    "dividend_hours": 6,
    "dividend_rate": 0.8,
    "price_decimals": 2,
}

MIN_TICK_MINUTES = 1
MAX_TICK_MINUTES = 120


@register(
    "astrbot_plugin_hongdou_stock",
    "hongdougao111",
    "红豆股市：用红豆币模拟炒股，带低保兜底",
    "1.0.0",
)
class HongdouStockPlugin(Star):
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
        """启动：建目录、读档、补算漏掉的 tick、起行情循环。"""
        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_hongdou_stock")
        except Exception as e:
            # 拿不到官方数据目录时退到插件自身目录，宁可脏一点也别丢档
            logger.warning(f"[hongdou-stock] 取数据目录失败，改用插件目录: {e}")
            data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = data_dir / "market.json"
        self._state = self._load()
        tick_sec = self._tick_seconds()

        # 停机期间漏掉的行情补算一小段，避免价格停在很久以前
        try:
            n = market.catch_up(self._state, tick_sec, max_ticks=240)
            if n:
                logger.info(f"[hongdou-stock] 补算了 {n} 个行情周期")
        except Exception as e:
            logger.error(f"[hongdou-stock] 补算行情失败: {e}")

        self._save(force=True)
        self._diag = {
            "state_path": str(self._state_path),
            "users": len(self._state.get("users") or {}),
            "tick_count": self._state.get("tick_count", 0),
        }
        logger.info(
            f"[hongdou-stock] 已加载：{self._diag['users']} 个账户 / "
            f"{self._diag['tick_count']} 个周期 / 存档 {self._state_path}"
        )

        self._task = asyncio.create_task(self._market_loop())

    async def terminate(self) -> None:
        """卸载：停循环、落盘。"""
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
            logger.error(f"[hongdou-stock] 存档损坏，重新开市: {e}")
            return market.new_state()
        if not isinstance(raw, dict) or "stocks" not in raw:
            logger.error("[hongdou-stock] 存档结构不对，重新开市")
            return market.new_state()
        # 补齐后来新增的股票，老存档不至于缺票
        stocks = raw.setdefault("stocks", {})
        for s in STOCKS:
            stocks.setdefault(
                s.code,
                {
                    "price": s.base_price,
                    "open": s.base_price,
                    "prev_close": s.base_price,
                    "history": [s.base_price],
                    "volume": 0,
                },
            )
        raw.setdefault("users", {})
        raw.setdefault("news", [])
        return raw

    def _save(self, force: bool = False) -> None:
        """写盘。非强制时最多 10 秒一次，避免频繁 IO。"""
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
            tmp.replace(self._state_path)   # 原子替换，断电不留半个文件
        except OSError as e:
            logger.error(f"[hongdou-stock] 存档写入失败: {e}")

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

    def _dividend_seconds(self) -> int:
        return int(self._cfg_num("dividend_hours", 1, 720) * 3600)

    def _dividend_rate(self) -> float:
        """每次分红的比例（占持仓市值），配置里是百分数。"""
        return self._cfg_num("dividend_rate", 0, 20) / 100.0

    # ---------------- 行情循环 ----------------

    async def _market_loop(self) -> None:
        """后台循环：按周期推进行情、分红、存档。"""
        while True:
            try:
                await asyncio.sleep(self._tick_seconds())
                async with self._lock:
                    if self._state is None:
                        continue
                    news = market.tick_state(self._state, self._tick_seconds())
                    self._maybe_dividend()
                    self._save()
                if news:
                    logger.info("[hongdou-stock] " + "；".join(news))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 单次失败不能让循环死掉，否则行情永远停住
                logger.error(f"[hongdou-stock] 行情循环异常: {e}")
                await asyncio.sleep(30)

    def _maybe_dividend(self) -> None:
        """到点了就给所有持仓用户发分红。"""
        if self._state is None:
            return
        now = time.time()
        period = self._dividend_seconds()
        last = float(self._state.get("last_dividend_at") or 0.0)
        if now - last < period:
            return
        self._state["last_dividend_at"] = now
        paid = market.pay_dividends(self._state, self._dividend_rate())
        if paid:
            total = sum(a for _, a in paid)
            logger.info(
                f"[hongdou-stock] 分红：{len(paid)} 个账户，共 {total:.0f} 红豆币"
            )

    # ---------------- 指令 ----------------

    @filter.command("股")
    async def stock(self, event: AstrMessageEvent, sub: str = "", arg1: str = "",
                    arg2: str = ""):
        """红豆股市。用法：/股 买|卖|仓|榜|低保|看"""
        async with self._lock:
            if self._state is None:
                self._state = market.new_state()

            uid = event.get_sender_id() or "unknown"
            name = event.get_sender_name() or uid
            u = market.user_of(
                self._state, uid, name, self._initial_cash()
            )

            sub = (sub or "").strip().lower()
            text = await self._dispatch(event, u, sub, arg1, arg2)
            self._save()
        yield event.plain_result(text)

    async def _dispatch(self, event: AstrMessageEvent, u: dict[str, Any],
                        sub: str, a1: str, a2: str) -> str:
        """子命令分发。返回要发的文本。"""
        assert self._state is not None
        st = self._state

        if sub in ("", "行情", "市", "q"):
            return self._render_market(st, u)
        if sub in ("买", "b", "buy"):
            return self._do_buy(st, u, a1, a2)
        if sub in ("卖", "s", "sell"):
            return self._do_sell(st, u, a1, a2)
        if sub in ("仓", "持仓", "c", "me"):
            return self._render_holdings(st, u)
        if sub in ("榜", "排行", "rank"):
            return self._render_rank(st, u, event.get_sender_id() or "")
        if sub in ("低保", "welfare"):
            return self._do_low_income(st, u)
        if sub in ("看", "详情", "d"):
            return self._render_detail(st, a1)
        return (
            "不认识这个子命令。\n"
            "/股 行情   /股 买 代码 股数   /股 卖 代码 股数\n"
            "/股 仓     /股 榜     /股 低保    /股 看 代码"
        )

    # ---------------- 各页面渲染 ----------------

    def _render_market(self, st: dict[str, Any], u: dict[str, Any]) -> str:
        lines = ["🍁 红豆股市 · 实时行情", ""]
        for s in STOCKS:
            px = market.price_of(st, s.code)
            chg = market.change_pct(st, s.code)
            arrow = "▲" if chg > 0.0005 else ("▼" if chg < -0.0005 else "—")
            spark = market.sparkline(st, s.code, 10)
            lines.append(
                f"{s.name} {s.code}  {px:>8.2f}  {arrow}{chg * 100:+.2f}%  {spark}"
            )
        cash = float(u.get("cash", 0.0))
        total = market.total_assets(st, u)
        lines.append("")
        lines.append(f"你的红豆币：{cash:.0f}   总资产：{total:.0f}")
        lines.append("买：/股 买 HD003 10　详情：/股 看 HD003")
        return "\n".join(lines)

    def _render_detail(self, st: dict[str, Any], code: str) -> str:
        s = resolve(code)
        if s is None:
            return "没有这只股票，用 /股 看行情表。"
        px = market.price_of(st, s.code)
        chg = market.change_pct(st, s.code)
        hist = list((st.get("stocks", {}).get(s.code) or {}).get("history") or [])
        hi = max(hist) if hist else px
        lo = min(hist) if hist else px
        return (
            f"【{s.name}】{s.code} · {s.sector}\n"
            f"现价 {px:.2f}　今日 {chg * 100:+.2f}%\n"
            f"近期区间 {lo:.2f} ~ {hi:.2f}\n"
            f"走势 {market.sparkline(st, s.code, 24)}\n"
            f"{s.desc}"
        )

    def _render_holdings(self, st: dict[str, Any], u: dict[str, Any]) -> str:
        hold = u.get("holdings") or {}
        cash = float(u.get("cash", 0.0))
        if not hold:
            return (
                f"你还没有持仓。\n红豆币：{cash:.0f}\n"
                f"看看行情：/股"
            )
        lines = ["🍁 我的持仓", ""]
        total_pnl = 0.0
        for code, sh in hold.items():
            s = resolve(code)
            px = market.price_of(st, code)
            mv = px * sh
            cost = float((u.get("cost") or {}).get(code, 0.0))
            pnl = mv - cost
            total_pnl += pnl
            pct = (pnl / cost * 100) if cost > 0 else 0.0
            nm = s.name if s else code
            lines.append(
                f"{nm} {code}  {sh} 股 @ {px:.2f}  市值 {mv:.0f}  "
                f"{'浮盈' if pnl >= 0 else '浮亏'} {pnl:+.0f} ({pct:+.1f}%)"
            )
        lines.append("")
        lines.append(
            f"现金 {cash:.0f}　持仓 {market.holding_value(st, u):.0f}　"
            f"总资产 {market.total_assets(st, u):.0f}"
        )
        lines.append(f"浮动盈亏合计 {total_pnl:+.0f}")
        return "\n".join(lines)

    def _render_rank(self, st: dict[str, Any], u: dict[str, Any],
                     my_uid: str) -> str:
        rows = market.ranking(st, 10)
        if not rows:
            return "还没有人入市，你可以做第一个。"
        lines = ["🍁 红豆富豪榜", ""]
        for i, (uid, nm, assets) in enumerate(rows, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            me = " ←你" if uid == my_uid else ""
            lines.append(f"{medal} {nm}  {assets:.0f}{me}")
        return "\n".join(lines)

    # ---------------- 交易 ----------------

    def _do_buy(self, st: dict[str, Any], u: dict[str, Any],
                code: str, shares_s: str) -> str:
        if not code or not shares_s:
            return "用法：/股 买 代码 股数　例：/股 买 HD003 10"
        s = resolve(code)
        if s is None:
            return f"没有 {code} 这只股票。"
        try:
            shares = int(float(shares_s))
        except (TypeError, ValueError):
            return "股数要写数字，比如 10。"
        ok, msg = market.buy(st, u, s.code, shares)
        if not ok:
            return msg
        return msg + f"\n现金余额 {float(u.get('cash', 0.0)):.0f}"

    def _do_sell(self, st: dict[str, Any], u: dict[str, Any],
                 code: str, shares_s: str) -> str:
        if not code or not shares_s:
            return "用法：/股 卖 代码 股数　例：/股 卖 HD003 10"
        s = resolve(code)
        if s is None:
            return f"没有 {code} 这只股票。"
        owned = int((u.get("holdings") or {}).get(s.code, 0))
        if shares_s.strip().lower() in ("all", "全", "全部"):
            shares = owned
        else:
            try:
                shares = int(float(shares_s))
            except (TypeError, ValueError):
                return "股数要写数字，或写 all 全卖。"
        ok, msg = market.sell(st, u, s.code, shares)
        if not ok:
            return msg
        return msg + f"\n现金余额 {float(u.get('cash', 0.0)):.0f}"

    # ---------------- 低保 ----------------

    def _do_low_income(self, st: dict[str, Any], u: dict[str, Any]) -> str:
        threshold = self._low_income_threshold()
        ok, why = market.can_low_income(st, u, threshold)
        if not ok:
            return why
        amount = self._low_income_amount()
        if amount <= 0:
            return "管理员把低保关掉了（金额为 0），自求多福吧。"
        msg = market.grant_low_income(st, u, amount)
        return f"🍚 {msg}\n现金余额 {float(u.get('cash', 0.0)):.0f}"

    # ---------------- 诊断 ----------------

    @filter.command("股诊断")
    async def diagnose(self, event: AstrMessageEvent):
        """检查红豆股市运行状态。"""
        assert self._state is not None or True
        st = self._state or {}
        users = len(st.get("users") or {})
        ticks = int(st.get("tick_count", 0))
        last = float(st.get("last_tick", 0.0))
        ago = int(time.time() - last) if last else -1
        yield event.plain_result(
            "【红豆股市 诊断】\n"
            f"存档：{self._diag.get('state_path', '(未初始化)')}\n"
            f"账户 {users} 个　行情周期 {ticks} 次\n"
            f"上次行情：{ago} 秒前　间隔 {self._tick_seconds()} 秒\n"
            f"低保 {self._low_income_amount():.0f}（资产低于 "
            f"{self._low_income_threshold():.0f} 可领，每天一次）\n"
            f"分红每 {self._cfg_num('dividend_hours', 1, 720):.0f} 小时，"
            f"持仓市值的 {self._cfg_num('dividend_rate', 0, 20):.2f}%\n"
            f"行情循环：{'运行中' if self._task and not self._task.done() else '已停止'}"
        )
