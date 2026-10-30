"""FX战士久留美 · 货币对数据。

全部虚构行情，由本地随机游走生成，不联网、不接真实报价。

报价约定：
  所有货币对都用「中间价」表示，两位/三位/五位小数看 digits。
  日元对（USDJPY、EURJPY、GBPJPY）习惯报 3 位小数，
  其他货币对报 5 位小数，黄金报 2 位。

  盈亏不涉及汇率换算 —— 本插件假设红豆币就是计价货币，
  价格变动直接按「名义价值 × 涨跌幅」折算成红豆币。
  这是刻意的简化：真做交叉汇率换算会把玩家绕晕，而且没有意义。

字段：
  code        代码，用户输入的就是它（USDJPY / usdjpy 都认）
  name        中文名
  base        基准中间价
  daily_vol   日波动率，0.005 = 每天典型波动 0.5%
  digits      报价小数位
  desc        一句话简介
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pair:
    code: str
    name: str
    base: float
    daily_vol: float
    digits: int
    desc: str

    @property
    def tick_size(self) -> float:
        """最小报价单位。"""
        return 10 ** (-self.digits)


# 波动率按真实外汇市场的相对关系设定：
# 主要货币对温和，英镑/日元这种交叉盘是「野兽」。
PAIRS: tuple[Pair, ...] = (
    Pair("USDJPY", "美元/日元", 150.000, 0.0055, 3,
         "全服最主流的货币对，日本散户的最爱。"),
    Pair("EURUSD", "欧元/美元", 1.08500, 0.0050, 5,
         "全球成交量最大的货币对，走势相对温和。"),
    Pair("GBPUSD", "英镑/美元", 1.27000, 0.0062, 5,
         "英镑脾气大，一根针能扎穿你的止损。"),
    Pair("AUDUSD", "澳元/美元", 0.66000, 0.0070, 5,
         "商品货币，跟着铁矿石和大宗走。"),
    Pair("USDCHF", "美元/瑞郎", 0.88000, 0.0052, 5,
         "避险货币，出事的时候它最硬。"),
    Pair("EURJPY", "欧元/日元", 163.000, 0.0075, 3,
         "交叉盘，波动比直盘大一圈。"),
    Pair("GBPJPY", "英镑/日元", 207.000, 0.0095, 3,
         "人称「野兽」，一天能走两百点，也能把你打爆。"),
    Pair("XAUUSD", "黄金/美元", 2350.00, 0.0110, 2,
         "不是外汇但人人都在炒，避险之王，也是爆仓之王。"),
)

BY_CODE: dict[str, Pair] = {p.code: p for p in PAIRS}
BY_NAME: dict[str, Pair] = {p.name: p for p in PAIRS}


def resolve(text: str) -> Pair | None:
    """解析用户输入：代码、中文名、大小写、斜杠写法都认。"""
    t = (text or "").strip()
    if not t:
        return None
    # 去掉斜杠和空格：USD/JPY -> USDJPY
    compact = t.replace("/", "").replace(" ", "").replace("_", "").upper()
    if compact in BY_CODE:
        return BY_CODE[compact]
    if t in BY_NAME:
        return BY_NAME[t]
    if compact in BY_NAME:
        return BY_NAME[compact]
    # 模糊：中文名包含
    for p in PAIRS:
        if t in p.name:
            return p
    return None


def fmt_price(pair: Pair, price: float) -> str:
    """按货币对的习惯小数位格式化报价。"""
    return f"{price:.{pair.digits}f}"
