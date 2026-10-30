"""红豆股市 · 离线自测。

用伪造的 astrbot 模块跑一遍，不需要安装 AstrBot。

    python test_plugin.py
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import tempfile
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


# ---------------------------------------------------------------------------
# 伪造 astrbot
# ---------------------------------------------------------------------------
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
    return asyncio.get_event_loop().run_until_complete(coro)


def call(p, event, *args):
    """调 /股 指令，返回文本。"""
    async def go():
        async for r in p.stock(event, *args):
            return r.text
        return ""
    return run(go())


# ---------------------------------------------------------------------------
def main():
    # 用工作区内的临时目录，避免系统 temp 在某些沙箱下不可列
    import shutil
    tmp = HERE / "_testdata"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    Plain, FakeConfig = install_fake_astrbot(tmp)
    sys.path.insert(0, str(HERE.parent))   # 为了 `from astrbot_plugin_hongdou_stock import`

    # 包导入：插件目录本身就是包
    import importlib
    pkg = importlib.import_module("astrbot_plugin_hongdou_stock.market")
    stk = importlib.import_module("astrbot_plugin_hongdou_stock.stocks")
    plugin_main = importlib.import_module("astrbot_plugin_hongdou_stock.main")
    P = plugin_main.HongdouStockPlugin

    # ------------------------------------------------------------------
    print("[1] 配置 Schema")
    schema = json.loads((HERE / "_conf_schema.json").read_text(encoding="utf-8"))
    for key in ("tick_minutes", "initial_cash", "low_income_amount",
                "low_income_threshold", "dividend_hours", "dividend_rate"):
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

    # DEFAULTS 必须和 schema 对得上
    for k, v in schema.items():
        check(f"DEFAULTS 含 {k}", k in plugin_main.DEFAULTS, k)
        check(f"{k} 默认值一致",
              plugin_main.DEFAULTS.get(k) == v.get("default"),
              f"{plugin_main.DEFAULTS.get(k)} vs {v.get('default')}")

    # ------------------------------------------------------------------
    print("\n[2] 股票数据")
    codes = [s.code for s in stk.STOCKS]
    check("代码唯一", len(codes) == len(set(codes)))
    check("名字唯一", len({s.name for s in stk.STOCKS}) == len(stk.STOCKS))
    check("基准价为正", all(s.base_price > 0 for s in stk.STOCKS))
    check("波动率为正", all(s.daily_vol > 0 for s in stk.STOCKS))
    check("能按代码查", stk.resolve("HD001") is not None)
    check("能按小写代码查", stk.resolve("hd001") is not None)
    check("能按名字查", stk.resolve("红豆银行") is not None)
    check("能模糊查", stk.resolve("鲸鱼") is not None)
    check("查不到返回 None", stk.resolve("不存在的票") is None)
    check("空输入返回 None", stk.resolve("") is None)

    # ------------------------------------------------------------------
    print("\n[3] 行情推进")
    st = pkg.new_state(now=1000000.0)
    check("初始有全部股票", len(st["stocks"]) == len(stk.STOCKS))
    r = random.Random(42)
    for _ in range(200):
        pkg.tick_state(st, 300, rng=r, now=1000000.0)
    check("价格全部为正", all(v["price"] > 0 for v in st["stocks"].values()))
    check("tick 计数正确", st["tick_count"] == 200, str(st["tick_count"]))
    # 软边界：跑很多 tick 后价格不应爆炸或归零
    r2 = random.Random(7)
    st2 = pkg.new_state(now=1000000.0)
    for _ in range(2000):
        pkg.tick_state(st2, 300, rng=r2, now=1000000.0)
    ratios = [st2["stocks"][s.code]["price"] / s.base_price for s in stk.STOCKS]
    check("长期不归零", min(ratios) > 0.05, f"min={min(ratios):.3f}")
    check("长期不爆炸", max(ratios) < 20, f"max={max(ratios):.3f}")

    # 历史长度有上限
    hist = st2["stocks"]["HD001"]["history"]
    check("历史长度有上限", len(hist) <= pkg.HISTORY_KEEP, str(len(hist)))

    # 补算
    st3 = pkg.new_state(now=1000000.0)
    n = pkg.catch_up(st3, 300, max_ticks=240, now=1000000.0 + 300 * 500)
    check("补算有上限", n == 240, str(n))

    # ------------------------------------------------------------------
    print("\n[4] 交易")
    st = pkg.new_state(now=1000000.0)
    u = pkg.user_of(st, "u1", "阿鲸", 1000.0, now=1000000.0)
    check("开户送钱", u["cash"] == 1000.0)

    px = pkg.price_of(st, "HD001")
    ok, msg = pkg.buy(st, u, "HD001", 5)
    check("买成功", ok, msg)
    check("持仓增加", u["holdings"]["HD001"] == 5)
    expect = 1000.0 - px * 5 - max(1.0, px * 5 * pkg.FEE_RATE)
    check("现金扣对", abs(u["cash"] - expect) < 0.02, f"{u['cash']} vs {expect}")

    ok, msg = pkg.buy(st, u, "HD001", 10 ** 6)
    check("钱不够被拒", not ok, msg)

    ok, msg = pkg.buy(st, u, "不存在", 1)
    check("无此股票被拒", not ok, msg)

    ok, msg = pkg.buy(st, u, "HD001", 0)
    check("0 股被拒", not ok, msg)

    ok, msg = pkg.sell(st, u, "HD001", 5)
    check("卖成功", ok, msg)
    check("持仓清空", "HD001" not in u["holdings"], str(u["holdings"]))

    ok, msg = pkg.sell(st, u, "HD001", 1)
    check("没持仓卖不出", not ok, msg)

    ok, msg = pkg.sell(st, u, "HD002", 1)
    check("卖没买的票被拒", not ok, msg)

    # 部分卖出，成本按比例结转
    st = pkg.new_state(now=1000000.0)
    u = pkg.user_of(st, "u2", "小尾", 10000.0, now=1000000.0)
    ok, m = pkg.buy(st, u, "HD001", 50)
    check("部分卖出测试建仓成功", ok, m)
    cost_all = u["cost"]["HD001"]
    pkg.sell(st, u, "HD001", 20)
    check("部分卖出后仍持仓", u["holdings"]["HD001"] == 30)
    check("成本按比例结转",
          abs(u["cost"]["HD001"] - cost_all * 0.6) < 1.0,
          f"{u['cost']['HD001']} vs {cost_all * 0.6}")

    # ------------------------------------------------------------------
    print("\n[5] 低保")
    st = pkg.new_state(now=1000000.0)
    u = pkg.user_of(st, "poor", "穷光蛋", 0.0, now=1000000.0)
    ok, why = pkg.can_low_income(st, u, 300.0, now=1000000.0)
    check("穷人能领", ok, why)
    pkg.grant_low_income(st, u, 200.0, now=1000000.0)
    check("低保到账", u["cash"] == 200.0, str(u["cash"]))
    ok, why = pkg.can_low_income(st, u, 300.0, now=1000000.0)
    check("当天不能重复领", not ok, why)
    ok, why = pkg.can_low_income(st, u, 300.0, now=1000000.0 + 86400)
    check("第二天能再领", ok, why)

    rich = pkg.user_of(st, "rich", "有钱人", 100000.0, now=1000000.0)
    ok, why = pkg.can_low_income(st, rich, 300.0, now=1000000.0)
    check("富人领不到", not ok, why)

    # 资产上限按「现金+持仓」算，不是只看现金
    mid = pkg.user_of(st, "mid", "中产", 0.0, now=1000000.0)
    pkg.buy(st, mid, "HD001", 5)   # 现金不够，先给钱
    mid["cash"] = 100.0
    mid["holdings"] = {"HD003": 10}   # 10*260 = 2600 市值
    ok, why = pkg.can_low_income(st, mid, 300.0, now=1000000.0)
    check("持仓算进资产", not ok, why)

    # ------------------------------------------------------------------
    print("\n[6] 分红")
    st = pkg.new_state(now=1000000.0)
    u = pkg.user_of(st, "h", "持股人", 0.0, now=1000000.0)
    u["holdings"] = {"HD001": 100}     # 100 * 100 = 10000 市值
    paid = pkg.pay_dividends(st, 0.01, now=1000000.0)
    check("分红有记录", len(paid) == 1, str(paid))
    check("分红金额对", abs(paid[0][1] - 100.0) < 0.5, str(paid))
    check("现金增加", abs(u["cash"] - 100.0) < 0.5, str(u["cash"]))

    empty = pkg.user_of(st, "e", "空仓", 0.0, now=1000000.0)
    paid2 = pkg.pay_dividends(st, 0.01, now=1000000.0)
    check("空仓不分红", all(uid != "e" for uid, _ in paid2))

    # ------------------------------------------------------------------
    print("\n[7] 排行与辅助")
    st = pkg.new_state(now=1000000.0)
    a = pkg.user_of(st, "a", "甲", 100.0, now=1000000.0)
    b = pkg.user_of(st, "b", "乙", 900.0, now=1000000.0)
    rows = pkg.ranking(st, 10)
    check("排行按资产降序", rows[0][0] == "b", str(rows))

    check("sparkline 非空", len(pkg.sparkline(st, "HD001", 10)) > 0)
    check("sparkline 无历史不崩", len(pkg.sparkline({"stocks": {}}, "X", 10)) == 10)

    # ------------------------------------------------------------------
    print("\n[8] 插件层")
    cfg = FakeConfig({
        "tick_minutes": 5,
        "initial_cash": 1000,
        "low_income_amount": 200,
        "low_income_threshold": 300,
        "dividend_hours": 6,
        "dividend_rate": 0.8,
        "price_decimals": 2,
    })
    p = P(context=None, config=cfg)

    async def boot():
        await p.initialize()
    run(boot())
    check("初始化后有状态", p._state is not None)
    check("存档文件已建", p._state_path.exists(), str(p._state_path))
    check("后台循环在跑", p._task is not None and not p._task.done())

    ev = FakeEvent()
    txt = call(p, ev)
    check("行情页有内容", "红豆股市" in txt, txt[:80])
    check("行情页含股票名", "红豆银行" in txt)

    txt = call(p, ev, "买", "HD001", "5")
    check("买提示成功", "买入" in txt, txt[:80])

    txt = call(p, ev, "仓")
    check("持仓页显示股票", "红豆银行" in txt, txt[:120])

    txt = call(p, ev, "榜")
    check("排行榜有内容", "富豪榜" in txt, txt[:80])

    txt = call(p, ev, "看", "HD003")
    check("详情页有板块", "科技" in txt, txt[:80])

    txt = call(p, ev, "卖", "HD001", "all")
    check("全卖成功", "卖出" in txt, txt[:80])

    txt = call(p, ev, "不存在的子命令")
    check("未知子命令有提示", "不认识" in txt, txt[:80])

    txt = call(p, ev, "买")
    check("缺参数有用法", "用法" in txt, txt[:80])

    # 越界配置
    bad = P(context=None, config=FakeConfig({
        "tick_minutes": 9999,
        "initial_cash": -5,
        "low_income_amount": -1,
        "dividend_rate": 99,
    }))
    check("tick 钳到上限", bad._tick_seconds() == 120 * 60, str(bad._tick_seconds()))
    check("初始资金不为负", bad._initial_cash() >= 0)
    check("低保不为负", bad._low_income_amount() >= 0)
    check("分红率钳到 20%", abs(bad._dividend_rate() - 0.2) < 1e-9)

    # 存档往返
    async def roundtrip():
        await p.terminate()
    run(roundtrip())
    p2 = P(context=None, config=cfg)

    async def boot2():
        await p2.initialize()
    run(boot2())
    check("重启后账户还在", "1001" in (p2._state.get("users") or {}))
    run(roundtrip())

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
