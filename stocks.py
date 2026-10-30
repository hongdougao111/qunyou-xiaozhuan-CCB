"""红豆股市 · 标的数据。

所有股票都是虚构的，代码固定、不增删，方便玩家记忆。
base_price 是「上市基准价」，市场引擎会在此基础上随机游走。

字段说明：
  code        股票代码，全局唯一，用户输入的也是它
  name        股票名
  sector      板块，行情页按板块分组显示
  base_price  基准价（红豆币）
  daily_vol   日波动率，0.02 = 每天典型波动 2%
  daily_drift 长期漂移（每日），正数代表长期看涨
  desc        一句话简介，行情详情里显示
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    sector: str
    base_price: float
    daily_vol: float
    daily_drift: float
    desc: str


STOCKS: tuple[Stock, ...] = (
    Stock("HD001", "红豆银行", "金融", 100.0, 0.012, 0.00003,
          "全服最稳的票，涨得慢也跌得慢，适合躺平。"),
    Stock("HD002", "鲸鱼重工", "工业", 88.0, 0.022, 0.00004,
          "造桥修路挖矿一条龙，订单看天吃饭。"),
    Stock("HD003", "尾鳍科技", "科技", 260.0, 0.045, 0.00008,
          "波动之王，一天能让你上天堂也能让你下海沟。"),
    Stock("HD004", "干饭集团", "消费", 65.0, 0.018, 0.00005,
          "民以食为天，业绩稳定，逢年过节必涨。"),
    Stock("HD005", "星尘能源", "能源", 150.0, 0.030, 0.00002,
          "油价一涨它就笑，油价一跌它就哭。"),
    Stock("HD006", "摸鱼传媒", "传媒", 42.0, 0.038, -0.00001,
          "热点来得快去得也快，典型的消息驱动股。"),
    Stock("HD007", "深海矿业", "资源", 120.0, 0.026, 0.00003,
          "挖到宝就飞天，挖到石头就沉底。"),
    Stock("HD008", "傲娇生物", "医药", 190.0, 0.034, 0.00006,
          "新药一旦过审，直接给你表演一个涨停。"),
    Stock("HD009", "云朵航空", "交运", 76.0, 0.024, 0.00001,
          "淡旺季分明，节假日前后两副面孔。"),
    Stock("HD010", "债务黑洞", "投机", 33.0, 0.070, -0.00006,
          "高风险的妖股，赌对了翻身，赌错了归零。"),
)

BY_CODE: dict[str, Stock] = {s.code: s for s in STOCKS}
# 也允许用名字查，用户懒得记代码
BY_NAME: dict[str, Stock] = {s.name: s for s in STOCKS}


def resolve(text: str) -> Stock | None:
    """把用户输入解析成股票：代码、名字、大小写都认。"""
    t = (text or "").strip()
    if not t:
        return None
    if t in BY_CODE:
        return BY_CODE[t]
    up = t.upper()
    if up in BY_CODE:
        return BY_CODE[up]
    if t in BY_NAME:
        return BY_NAME[t]
    # 模糊：名字里包含
    for s in STOCKS:
        if t in s.name:
            return s
    return None


SECTORS: tuple[str, ...] = tuple(dict.fromkeys(s.sector for s in STOCKS))
