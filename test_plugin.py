"""红豆币爆仓模拟器 · 离线自测。

用伪造的 astrbot 模块跑一遍，不需要安装 AstrBot。

    python test_plugin.py
"""
from __future__ import annotations

import asyncio
import json
import random
import shutil
import sys
import types
from pathlib import Path

HERE = Path(__file__).parent

PASS = 0
FAIL = 0
FAILS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def install_fake_astrbot(data_dir: Path):
    class Plain:
        def __init__(self, text=""):
            self.text = text

    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.Plain = Plain

    class FakeConfig(dict):
        def save_config(self):
            pass

    api_mod = types.ModuleType("astrbot.api")
    api_mod.AstrBotConfig = FakeConfig
    api_mod.logger = types.SimpleNamespace(
        error=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
    )

    ev_mod = types.ModuleType("astrbot.api.event")

    class _Filter:
        @staticmethod
        def command(*a, **k):
            def deco(f):
                return f
            return deco

    ev_mod.filter = _Filter
    ev_mod.AstrMessageEvent = object

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None, config=None):
            pass

    class StarTools:
        @classmethod
        def get_data_dir(cls, name=None):
            return data_dir

    def register(*a, **k):
        def deco(cls):
            return cls
        return deco

    star_mod.Star = Star
    star_mod.Context = object
    star_mod.StarTools = StarTools
    star_mod.register = register

    sys.modules["astrbot"] = types.ModuleType("astrbot")
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.message_components"] = comp_mod
    sys.modules["astrbot.api.event"] = ev_mod
    sys.modules["astrbot.api.star"] = star_mod
    return Plain, FakeConfig


class FakeEvent:
    def __init__(self, uid="1001", name="测试用户"):
        self._uid = uid
        self._name = name

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return self._name

    def plain_result(self, text):
        return types.SimpleNamespace(text=text)


def run(coro):
    try:
        return asyncio.get_event_loop().run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def call(p, event, *args):
    async def go():
        async for r in p.fx(event, *args):
            return r.text
        return ""
    return run(go())


def set_price(st, code, price):
    """测试辅助：直接改价格，模拟行情走到某处。"""
    st["quotes"][code]["price"] = price


# ---------------------------------------------------------------------------
def main():
    tmp = HERE / "_testdata"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    Plain, FakeConfig = install_fake_astrbot(tmp)
    sys.path.insert(0, str(HERE.parent))

    import importlib
    mkt = importlib.import_module("astrbot_plugin_hongdou_stock.market")
    prs = importlib.import_module("astrbot_plugin_hongdou_stock.pairs")
    plugin_main = importlib.import_module("astrbot_plugin_hongdou_stock.main")
    P = plugin_main.FxKurumiPlugin

    # ------------------------------------------------------------------
    print("[1] 配置 Schema")
    schema = json.loads((HERE / "_conf_schema.json").read_text(encoding="utf-8"))
    for key in ("tick_minutes", "initial_cash", "default_leverage",
                "max_leverage", "low_income_amount", "low_income_threshold",
                "swap_rate_daily"):
        check(f"Schema 含 {key}", key in schema, str(list(schema)))
    missing_type = [k for k, v in schema.items() if "type" not in v]
    check("所有项都有 type", not missing_type, str(missing_type))
    bad_slider = [k for k, v in schema.items()
                  if "slider" in v and ("min" not in v["slider"] or "max" not in v["slider"])]
    check("slider 配置完整", not bad_slider, str(bad_slider))
    bad_default = [k for k, v in schema.items()
                   if "slider" in v and "default" in v
                   and not (v["slider"]["min"] <= v["default"] <= v["slider"]["max"])]
    check("默认值在范围内", not bad_default, str(bad_default))
    for k, v in schema.items():
        check(f"DEFAULTS 含 {k}", k in plugin_main.DEFAULTS, k)
        check(f"{k} 默认值一致",
              plugin_main.DEFAULTS.get(k) == v.get("default"),
              f"{plugin_main.DEFAULTS.get(k)} vs {v.get('default')}")

    # ------------------------------------------------------------------
    print("\n[2] 货币对数据")
    codes = [p.code for p in prs.PAIRS]
    check("代码唯一", len(codes) == len(set(codes)))
    check("基准价为正", all(p.base > 0 for p in prs.PAIRS))
    check("波动率为正", all(p.daily_vol > 0 for p in prs.PAIRS))
    check("小数位合法", all(0 <= p.digits <= 5 for p in prs.PAIRS))
    check("按代码查", prs.resolve("USDJPY") is not None)
    check("按小写查", prs.resolve("usdjpy") is not None)
    check("按斜杠查", prs.resolve("USD/JPY") is not None)
    check("按中文名查", prs.resolve("英镑/日元") is not None)
    check("按中文模糊查", prs.resolve("黄金") is not None)
    check("查不到返回 None", prs.resolve("不存在的对") is None)
    check("空输入返回 None", prs.resolve("") is None)

    p_jpy = prs.resolve("USDJPY")
    check("日元对 3 位小数", p_jpy.digits == 3)
    check("报价格式化", prs.fmt_price(p_jpy, 150.123456) == "150.123",
          prs.fmt_price(p_jpy, 150.123456))

    # ------------------------------------------------------------------
    print("\n[3] 行情推进")
    st = mkt.new_state(now=1000000.0)
    check("初始有全部货币对", len(st["quotes"]) == len(prs.PAIRS))
    r = random.Random(42)
    for _ in range(200):
        mkt.tick_state(st, 300, rng=r, now=1000000.0)
    check("价格全部为正", all(v["price"] > 0 for v in st["quotes"].values()))
    check("tick 计数正确", st["tick_count"] == 200, str(st["tick_count"]))

    # 长期不应爆炸（外汇无漂移，应该回到基准附近）
    r2 = random.Random(7)
    st2 = mkt.new_state(now=1000000.0)
    for _ in range(2000):
        mkt.tick_state(st2, 300, rng=r2, now=1000000.0)
    ratios = [st2["quotes"][p.code]["price"] / p.base for p in prs.PAIRS]
    check("长期不归零", min(ratios) > 0.3, f"min={min(ratios):.3f}")
    check("长期不爆炸", max(ratios) < 3.0, f"max={max(ratios):.3f}")

    hist = st2["quotes"]["USDJPY"]["history"]
    check("历史长度有上限", len(hist) <= mkt.HISTORY_KEEP, str(len(hist)))

    st3 = mkt.new_state(now=1000000.0)
    n = mkt.catch_up(st3, 300, max_ticks=240, now=1000000.0 + 300 * 500)
    check("补算有上限", n == 240, str(n))

    # ------------------------------------------------------------------
    print("\n[4] 点差")
    st = mkt.new_state(now=1000000.0)
    set_price(st, "USDJPY", 150.0)
    bid, ask = mkt.bid_ask(st, "USDJPY")
    check("买价低于卖价", bid < ask, f"{bid} / {ask}")
    check("点差合理", 0 < (ask - bid) / 150 < 0.001, str((ask - bid) / 150))

    # ------------------------------------------------------------------
    print("\n[5] 开仓")
    st = mkt.new_state(now=1000000.0)
    set_price(st, "USDJPY", 150.0)
    u = mkt.user_of(st, "u1", "测试用户", 1000.0, now=1000000.0)
    check("开户送钱", u["balance"] == 1000.0)

    ok, msg = mkt.open_position(st, u, "USDJPY", "long", 0.5, 100)
    check("开多成功", ok, msg)
    check("有 1 个仓位", len(u["positions"]) == 1)
    pos = u["positions"][0]
    check("保证金正确", abs(pos["margin"] - 500 / 100) < 0.01, str(pos["margin"]))
    check("名义价值正确", pos["notional"] == 500, str(pos["notional"]))
    check("开仓扣了手续费", u["balance"] < 1000, str(u["balance"]))

    # 杠杆不足被拒
    u2 = mkt.user_of(st, "u2", "穷鬼", 5.0, now=1000000.0)
    ok, msg = mkt.open_position(st, u2, "USDJPY", "long", 1.0, 100)
    check("保证金不够被拒", not ok, msg)

    # 参数校验
    ok, _ = mkt.open_position(st, u, "USDJPY", "long", 0.001, 100)
    check("手数太小被拒", not ok)
    ok, _ = mkt.open_position(st, u, "USDJPY", "long", 0.5, 0)
    check("杠杆 0 被拒", not ok)
    ok, _ = mkt.open_position(st, u, "USDJPY", "long", 0.5, 1000)
    check("杠杆过大被拒", not ok)
    ok, _ = mkt.open_position(st, u, "NOPE", "long", 0.5, 100)
    check("无此货币对被拒", not ok)

    # 做空
    st = mkt.new_state(now=1000000.0)
    set_price(st, "USDJPY", 150.0)
    us = mkt.user_of(st, "u3", "空军", 1000.0, now=1000000.0)
    ok, msg = mkt.open_position(st, us, "USDJPY", "short", 0.5, 100)
    check("开空成功", ok, msg)
    check("空单方向正确", us["positions"][0]["side"] == "short")

    # ------------------------------------------------------------------
    print("\n[6] 盈亏计算")
    st = mkt.new_state(now=1000000.0)
    set_price(st, "USDJPY", 150.0)
    u = mkt.user_of(st, "p", "测盈亏", 10000.0, now=1000000.0)
    mkt.open_position(st, u, "USDJPY", "long", 1.0, 100)   # 名义 1000
    entry = u["positions"][0]["entry"]

    # 价格涨 1% → 盈利 10 红豆币
    set_price(st, "USDJPY", entry * 1.01)
    pnl = mkt.position_pnl(st, u["positions"][0])
    check("多单涨 1% 盈利约 10", abs(pnl - 10) < 0.2, str(pnl))

    # 价格跌 1% → 亏损 10
    set_price(st, "USDJPY", entry * 0.99)
    pnl = mkt.position_pnl(st, u["positions"][0])
    check("多单跌 1% 亏损约 10", abs(pnl + 10) < 0.2, str(pnl))

    # 空单方向相反
    st2 = mkt.new_state(now=1000000.0)
    set_price(st2, "USDJPY", 150.0)
    us = mkt.user_of(st2, "s", "空单", 10000.0, now=1000000.0)
    mkt.open_position(st2, us, "USDJPY", "short", 1.0, 100)
    e2 = us["positions"][0]["entry"]
    set_price(st2, "USDJPY", e2 * 0.99)
    pnl2 = mkt.position_pnl(st2, us["positions"][0])
    check("空单跌 1% 盈利约 10", abs(pnl2 - 10) < 0.2, str(pnl2))

    # ------------------------------------------------------------------
    print("\n[7] 爆仓")
    st = mkt.new_state(now=1000000.0)
    set_price(st, "USDJPY", 150.0)
    u = mkt.user_of(st, "liq", "赌狗", 100.0, now=1000000.0)
    # 100 红豆币，100 倍杠杆，开 5 手 = 5000 名义，保证金 50
    ok, msg = mkt.open_position(st, u, "USDJPY", "long", 5.0, 100)
    check("重仓开成功", ok, msg)
    entry = u["positions"][0]["entry"]

    check("一开始没爆", mkt.check_liquidation(st, u) == [])

    # 价格反向走 1% → 亏 50，权益跌到保证金的一半 → 爆仓
    set_price(st, "USDJPY", entry * 0.985)
    liq = mkt.check_liquidation(st, u)
    check("爆仓被触发", len(liq) > 0, str(liq))
    check("爆仓后没仓位", u["positions"] == [])
    check("爆仓后余额清零", u["balance"] == 0.0, str(u["balance"]))
    check("爆仓计数 +1", u["liquidations"] == 1)

    # 空仓不会爆
    u_none = mkt.user_of(st, "none", "空仓", 100.0, now=1000000.0)
    check("空仓不爆", mkt.check_liquidation(st, u_none) == [])

    # ------------------------------------------------------------------
    print("\n[8] 平仓")
    st = mkt.new_state(now=1000000.0)
    set_price(st, "USDJPY", 150.0)
    u = mkt.user_of(st, "c", "平仓", 1000.0, now=1000000.0)
    mkt.open_position(st, u, "USDJPY", "long", 1.0, 100)
    entry = u["positions"][0]["entry"]
    set_price(st, "USDJPY", entry * 1.01)   # 赚 10

    bal_before = u["balance"]
    ok, msg = mkt.close_position(st, u, 0)
    check("平仓成功", ok, msg)
    check("平仓后无仓位", u["positions"] == [])
    check("平仓后余额增加", u["balance"] > bal_before, f"{bal_before} -> {u['balance']}")

    ok, _ = mkt.close_position(st, u, 0)
    check("平不存在的仓被拒", not ok)

    # 多个仓位，倒序平
    st = mkt.new_state(now=1000000.0)
    u = mkt.user_of(st, "m", "多仓", 10000.0, now=1000000.0)
    for _ in range(3):
        mkt.open_position(st, u, "USDJPY", "long", 0.5, 100)
    check("开了 3 个仓", len(u["positions"]) == 3)
    for i in range(2, -1, -1):
        mkt.close_position(st, u, i)
    check("全部平掉", u["positions"] == [])

    # ------------------------------------------------------------------
    print("\n[9] 隔夜利息")
    st = mkt.new_state(now=1000000.0)
    set_price(st, "USDJPY", 150.0)
    u = mkt.user_of(st, "sw", "持仓", 10000.0, now=1000000.0)
    mkt.open_position(st, u, "USDJPY", "long", 1.0, 100)   # 名义 1000
    bal0 = u["balance"]
    paid = mkt.pay_swap(st, 0.001, now=1000000.0)          # 0.1%
    check("利息有记录", len(paid) == 1, str(paid))
    check("多头付利息", u["balance"] < bal0, f"{bal0} -> {u['balance']}")
    check("利息金额对", abs(paid[0][1] + 1.0) < 0.05, str(paid))

    st = mkt.new_state(now=1000000.0)
    u2 = mkt.user_of(st, "sw2", "空头", 10000.0, now=1000000.0)
    mkt.open_position(st, u2, "USDJPY", "short", 1.0, 100)
    bal0 = u2["balance"]
    mkt.pay_swap(st, 0.001, now=1000000.0)
    check("空头收利息", u2["balance"] > bal0, f"{bal0} -> {u2['balance']}")

    # 空仓不参与
    st = mkt.new_state(now=1000000.0)
    mkt.user_of(st, "nosw", "空仓", 1000.0, now=1000000.0)
    check("空仓不收利息", mkt.pay_swap(st, 0.001) == [])

    # ------------------------------------------------------------------
    print("\n[10] 保证金比例与低保")
    st = mkt.new_state(now=1000000.0)
    set_price(st, "USDJPY", 150.0)
    u = mkt.user_of(st, "ml", "比例", 1000.0, now=1000000.0)
    check("空仓时比例为 None", mkt.margin_level(st, u) is None)
    mkt.open_position(st, u, "USDJPY", "long", 1.0, 100)
    lvl = mkt.margin_level(st, u)
    check("有仓后有比例", lvl is not None and lvl > 0, str(lvl))

    # 低保
    st = mkt.new_state(now=1000000.0)
    poor = mkt.user_of(st, "poor", "爆仓仔", 0.0, now=1000000.0)
    ok, why = mkt.can_low_income(st, poor, 300.0, now=1000000.0)
    check("穷人能领", ok, why)
    mkt.grant_low_income(st, poor, 300.0, now=1000000.0)
    check("低保到账", poor["balance"] == 300.0, str(poor["balance"]))
    ok, _ = mkt.can_low_income(st, poor, 300.0, now=1000000.0)
    check("当天不能重复领", not ok)
    ok, _ = mkt.can_low_income(st, poor, 300.0, now=1000000.0 + 86400)
    check("第二天能再领", ok)

    rich = mkt.user_of(st, "rich", "有钱人", 100000.0, now=1000000.0)
    ok, why = mkt.can_low_income(st, rich, 300.0, now=1000000.0)
    check("富人领不到", not ok, why)

    # 有仓位时按权益判定
    mid = mkt.user_of(st, "mid", "有仓", 3000.0, now=1000000.0)
    mkt.open_position(st, mid, "USDJPY", "long", 1.0, 100)
    ok, why = mkt.can_low_income(st, mid, 300.0, now=1000000.0)
    check("有仓按权益判", not ok, why)

    # ------------------------------------------------------------------
    print("\n[11] 排行")
    st = mkt.new_state(now=1000000.0)
    mkt.user_of(st, "a", "甲", 100.0, now=1000000.0)
    mkt.user_of(st, "b", "乙", 900.0, now=1000000.0)
    rows = mkt.ranking(st, 10)
    check("排行按权益降序", rows[0][0] == "b", str(rows))

    check("sparkline 非空", len(mkt.sparkline(st, "USDJPY", 10)) > 0)
    check("sparkline 无数据不崩", len(mkt.sparkline({"quotes": {}}, "X", 10)) == 10)

    # ------------------------------------------------------------------
    print("\n[12] 插件层")
    cfg = FakeConfig({
        "tick_minutes": 5,
        "initial_cash": 1000,
        "default_leverage": 100,
        "max_leverage": 500,
        "low_income_amount": 300,
        "low_income_threshold": 300,
        "swap_rate_daily": 0.02,
    })
    p = P(context=None, config=cfg)
    run(p.initialize())
    check("初始化后有状态", p._state is not None)
    check("存档文件已建", p._state_path.exists(), str(p._state_path))
    check("后台循环在跑", p._task is not None and not p._task.done())

    ev = FakeEvent()
    txt = call(p, ev)
    check("行情页有内容", "红豆币爆仓模拟器" in txt, txt[:80])
    check("行情页含货币对", "美元/日元" in txt)

    txt = call(p, ev, "多", "USDJPY", "0.5", "100")
    check("做多提示成功", "做多" in txt, txt[:100])

    txt = call(p, ev, "仓")
    check("仓位页显示", "USDJPY" in txt, txt[:120])

    txt = call(p, ev, "看", "XAUUSD")
    check("详情页有黄金", "黄金" in txt, txt[:80])

    txt = call(p, ev, "空", "EURUSD", "0.2")
    check("省略杠杆用默认", "做空" in txt, txt[:100])

    txt = call(p, ev, "多", "USDJPY", "0.5", "9999")
    check("超杠杆被拒", "最高" in txt, txt[:80])

    txt = call(p, ev, "全平")
    check("一键平仓", "平掉" in txt, txt[:80])

    txt = call(p, ev, "榜")
    check("排行榜有内容", "红豆币富豪榜" in txt, txt[:80])

    txt = call(p, ev, "不存在的子命令")
    check("未知子命令有提示", "不认识" in txt, txt[:80])

    txt = call(p, ev, "多")
    check("缺参数有用法", "用法" in txt, txt[:80])

    # 越界配置
    bad = P(context=None, config=FakeConfig({
        "tick_minutes": 9999,
        "initial_cash": -5,
        "max_leverage": 99999,
        "swap_rate_daily": 50,
    }))
    check("tick 钳到上限", bad._tick_seconds() == 120 * 60, str(bad._tick_seconds()))
    check("初始资金不为负", bad._initial_cash() >= 0)
    check("杠杆上限钳到 500", bad._max_leverage() == 500)
    check("利率钳到 1%", abs(bad._swap_rate() - 0.01) < 1e-9, str(bad._swap_rate()))

    # 存档往返
    run(p.terminate())
    p2 = P(context=None, config=cfg)
    run(p2.initialize())
    check("重启后账户还在", "1001" in (p2._state.get("users") or {}))
    run(p2.terminate())

    # 旧版（股票）存档应被识别并重置
    old_path = tmp / "fx.json"
    old_path.write_text(json.dumps({"version": 1, "stocks": {"HD001": {}}}),
                        encoding="utf-8")
    p3 = P(context=None, config=cfg)
    run(p3.initialize())
    check("旧存档被重置", "quotes" in (p3._state or {}))
    run(p3.terminate())

    # ------------------------------------------------------------------
    print(f"\n{'=' * 46}")
    print(f"通过 {PASS}　失败 {FAIL}")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
