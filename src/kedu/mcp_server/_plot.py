"""绘图编译层: kedu 返回值 -> 规范化表格 -> 带版本的 ECharts option 信封.

三段分开, 与调用层(_invoke)解耦:

    invoke_kedu(api, params)      -> 原始返回值
    normalize_plot_data(value)    -> PlotFrame(表格 + JSON 安全值)
    compile_recipe(...)           -> PlotEnvelope(schema_version + option + meta)

「可调用」不等于「可绘图」。规范化这一步就是闸门: 规范不成表格、结果为空、或缺模板要的
列, 一律显式报错, 不画半张图。理由和 _render 的「绝不静默截断」是同一条 —— iframe 里
option 非法的表现是一片空白, 静默失败在图这条路上比在文本路上更贵。

模板(L1)按财经意图分类, 不按 ECharts series 分类: series 是渲染器概念, 用户要的是
「K 线」「相对表现」「估值分位带」。模板负责固定那些不该让模型即兴发挥的口径 —— 基期、
收益率算法、缺失值对齐、分位数定义、复权锚与标题说明。P0 只落 kline 一种。

显示项走白名单 (PlotDisplay, extra='forbid'): 任意 option 深合并会因数组覆盖与 series
重写把 L1 重新变成静默易错的 L2, 所以自由 option 只在未来的 chart='raw' 里开放。
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

# envelope 契约版本。改 option 结构或 meta 语义就要 +1, 并同步 static/chart.html 的
# SUPPORTED_SCHEMA —— 渲染器认不出版本时整张图拒绝渲染, 不去猜。
#
# v2: title/subtitle 从 ECharts option 里提到 envelope 顶层, 由渲染器画进 DOM。
#     canvas 里的文字不会换行, 窄屏(手机宽度)下副标题直接被裁掉, 而副标题正是承载
#     复权口径的地方 —— 裁掉等于把最该看见的信息弄丢。DOM 里交给 CSS 换行即可。
SCHEMA_VERSION = 2

# 单张图的数据点上限。超了不截断(截断的图会被当成完整的看), 直接报错让调用方收窄。
MAX_POINTS = 20_000

# A 股约定: 红涨绿跌。写死而不吃 ECharts 的版本默认值 —— 涨跌配色反了是财务事实错误,
# 不能取决于哪个版本的 dist 被 vendored 进来。
_UP_COLOR = "#eb5454"
_DOWN_COLOR = "#47b262"

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

# get_price 单代码返回窄表, index 名为 date(daily)或 datetime(1m); reset_index 后成列。
_TIME_COLUMNS = ("date", "datetime", "time", "day")

_OHLC = ("open", "close", "low", "high")


class PlotError(ValueError):
    """绘图请求无法编译成图 —— 数据形态不对、缺列、为空、超限."""


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------
def _json_safe(v: Any) -> Any:
    """把一个单元格转成 JSON 可序列化的值; NaN/NaT/±Inf 一律成 null.

    JSON 没有 NaN/Infinity, 让它们漏到 structuredContent 里会在 host 侧解析失败,
    表现为整张图不出来。宁可成 null 让 ECharts 断线。
    """
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, Decimal):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (pd.Timestamp, dt.datetime)):
        if pd.isna(v):
            return None
        # 1m bar 的时间部分是数据的一部分, 不能丢; 日线则不带零点噪声。
        if v.hour == 0 and v.minute == 0 and v.second == 0:
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, str):
        return v
    if isinstance(v, np.ndarray):
        return [_json_safe(x) for x in v.tolist()]
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return str(v)


@dataclass(frozen=True)
class PlotFrame:
    """规范化后的表格: 列名 + 行(每格已 JSON 安全)."""

    columns: list[str]
    rows: list[list[Any]]

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    def require(self, *names: str) -> None:
        """缺任何一列就报错, 并把实际有哪些列说清楚."""
        missing = [n for n in names if n not in self.columns]
        if missing:
            raise PlotError(
                f"数据缺少绘图需要的列: {', '.join(missing)}; "
                f"实际列为: {', '.join(self.columns)}。"
                f"请检查 fields 参数是否把这些列排除了。"
            )

    def column(self, name: str) -> list[Any]:
        i = self.columns.index(name)
        return [r[i] for r in self.rows]


def normalize_plot_data(value: Any) -> PlotFrame:
    """把 kedu 的返回值规范化成 PlotFrame; 规范不了就报错, 不猜."""
    if isinstance(value, pd.Series):
        value = value.to_frame(name=value.name or "value")
    if not isinstance(value, pd.DataFrame):
        raise PlotError(
            f"绘图要求表格型返回值, 实际拿到 {type(value).__name__}。"
            f"kedu 的 API 都可调用, 但只有能规范化成表格的返回值可绘图 —— "
            f"list/dict 类结果请改用文本 tool 查看。"
        )
    df = value
    # RangeIndex 是 0..n 的噪声; 其余(时间索引、代码索引)是数据的一部分, 必须提成列。
    # 判定与 _render 保持一致, 免得同一份数据在两条路上列不一样。
    if not isinstance(df.index, pd.RangeIndex):
        df = df.reset_index()
    if df.empty:
        raise PlotError(
            "查询结果为空, 无法绘图。请检查证券代码、日期区间, 或该标的在此区间是否有数据。"
        )
    if len(df) > MAX_POINTS:
        raise PlotError(
            f"数据点 {len(df)} 超过单图上限 {MAX_POINTS}。"
            f"不做截断 —— 截断后的图看不出缺了什么。请收窄日期区间, "
            f"或对分钟级数据改用更短的窗口。"
        )
    columns = [str(c) for c in df.columns]
    rows = [[_json_safe(v) for v in row] for row in df.itertuples(index=False, name=None)]
    return PlotFrame(columns=columns, rows=rows)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class PlotDisplay(BaseModel):
    """显示项白名单。extra='forbid' —— 未知键直接报错, 不静默忽略.

    这里只放不改变数据含义的开关。任何会改变「画的是什么」的东西(基期、复权、
    聚合口径)都属于模板参数, 不属于 display。
    """

    model_config = ConfigDict(extra="forbid")

    legend: bool = Field(default=True, description="是否显示图例")
    zoom: bool = Field(default=True, description="是否启用缩放(inside + slider)")
    animation: bool = Field(default=True, description="是否启用动画")
    y_min: float | None = Field(default=None, description="价格轴下限, 留空自适应")
    y_max: float | None = Field(default=None, description="价格轴上限, 留空自适应")
    up_color: str | None = Field(
        default=None, description=f"上涨颜色 hex, 默认 {_UP_COLOR}(A 股红涨)"
    )
    down_color: str | None = Field(
        default=None, description=f"下跌颜色 hex, 默认 {_DOWN_COLOR}(A 股绿跌)"
    )


class KlineSource(BaseModel):
    """K 线的数据来源。字段与 kedu.get_price 对齐, 语义完全一致."""

    model_config = ConfigDict(extra="forbid")

    security: str = Field(description="单个证券代码, 如 '000001.XSHE'")
    start_date: str | None = Field(default=None, description="起始日期 YYYY-MM-DD")
    end_date: str | None = Field(default=None, description="结束日期 YYYY-MM-DD")
    count: int | None = Field(
        default=None, description="取最近 N 根, 与 start_date 二选一"
    )
    frequency: Literal["daily", "1m"] = Field(default="daily", description="daily 或 1m")
    fq: Literal["pre", "post", "none"] = Field(
        default="pre", description="pre 前复权(动态锚) / post 后复权 / none 不复权"
    )


def _check_color(value: str | None, field: str) -> None:
    if value is not None and not _HEX_COLOR.match(value):
        raise PlotError(f"display.{field} 必须是 hex 颜色(如 #eb5454), 实际 {value!r}。")


# ---------------------------------------------------------------------------
# kline 模板
# ---------------------------------------------------------------------------
def _time_column(frame: PlotFrame) -> str:
    for name in _TIME_COLUMNS:
        if name in frame.columns:
            return name
    raise PlotError(
        f"数据里找不到时间列(期望 {' / '.join(_TIME_COLUMNS)} 之一); "
        f"实际列为: {', '.join(frame.columns)}。"
    )


def _check_time_axis(times: list[Any]) -> None:
    """时间必须严格递增: 乱序会把 K 线画成一团, 重复会让同一时刻出现两根."""
    if any(t is None for t in times):
        raise PlotError("时间列含空值, 无法作为 K 线的横轴。")
    dup = {t for i, t in enumerate(times[1:], 1) if t == times[i - 1]}
    if dup:
        sample = ", ".join(sorted(dup)[:5])
        raise PlotError(f"时间列有重复值({len(dup)} 个, 例如 {sample}), 无法绘制 K 线。")
    if any(times[i] < times[i - 1] for i in range(1, len(times))):
        raise PlotError("时间列非升序, 无法绘制 K 线。")


def _kline_subtitle(source: KlineSource, frame: PlotFrame, anchor: dict | None) -> str:
    """副标题必须自带口径: 请求区间、实际数据区间、复权方式与锚.

    图比 CSV 更容易被当成权威快照截图存档。前复权是动态值, 不把锚写在图上, 几天后
    同一张图的数值就对不上了, 而看图的人无从察觉。
    """
    times = frame.column(_time_column(frame))
    if source.count:
        requested = f"最近 {source.count} 根"
    else:
        requested = f"{source.start_date or '不限'} ~ {source.end_date or '不限'}"
    parts = [
        f"请求 {requested}",
        f"数据 {times[0]} ~ {times[-1]}({frame.n_rows} 根)",
    ]
    if source.fq == "pre":
        if anchor:
            parts.append(f"前复权 · 锚 {anchor['time']}(factor {anchor['factor']:.6g})")
        else:
            parts.append("前复权 · 锚未知")
    elif source.fq == "post":
        parts.append("后复权(基准同聚宽)")
    else:
        parts.append("不复权")
    return "  ·  ".join(parts)


# 画布布局(px)。标题不再占画布, 顶部只留图例。
_LEGEND_TOP = 4
_GRID_TOP = 26
_GRID_LEFT = 58
_GRID_RIGHT = 20
_SLIDER_H = 20
_SLIDER_BOTTOM = 8
_VOLUME_H = 58


def _grid_layout(has_volume: bool, zoom: bool) -> tuple[dict, dict | None]:
    """算价格网格与成交量网格的位置; 底部预留缩放条的高度."""
    reserved = _SLIDER_BOTTOM + _SLIDER_H + 18 if zoom else 20
    common = {"left": _GRID_LEFT, "right": _GRID_RIGHT}
    if not has_volume:
        return {**common, "top": _GRID_TOP, "bottom": reserved}, None
    return (
        {**common, "top": _GRID_TOP, "bottom": reserved + _VOLUME_H + 12},
        {**common, "height": _VOLUME_H, "bottom": reserved},
    )


def _compile_kline(
    source: KlineSource,
    display: PlotDisplay,
    frame: PlotFrame,
    anchor: dict | None,
) -> dict[str, Any]:
    frame.require(*_OHLC)
    tcol = _time_column(frame)
    times = frame.column(tcol)
    _check_time_axis(times)

    o, c, lo, hi = (frame.column(f) for f in _OHLC)
    # ECharts candlestick 的 defaultValueDimensions 是 [open, close, lowest, highest]
    # —— 不是 OHLC。顺序写错不会报错, 只会画出错误的实体与影线。
    ohlc = [[o[i], c[i], lo[i], hi[i]] for i in range(frame.n_rows)]
    if all(row == [None, None, None, None] for row in ohlc):
        raise PlotError("OHLC 全为空值, 没有可画的 K 线。")

    has_volume = "volume" in frame.columns
    volume = frame.column("volume") if has_volume else None

    up = display.up_color or _UP_COLOR
    down = display.down_color or _DOWN_COLOR
    price_grid, volume_grid = _grid_layout(has_volume, display.zoom)

    series: list[dict[str, Any]] = [
        {
            "name": "K线",
            "type": "candlestick",
            "data": ohlc,
            "itemStyle": {
                "color": up,
                "color0": down,
                "borderColor": up,
                "borderColor0": down,
            },
        }
    ]
    grids = [price_grid]
    x_axes: list[dict[str, Any]] = [
        {
            "type": "category",
            "data": times,
            "boundaryGap": True,
            "axisLine": {"onZero": False},
            "splitLine": {"show": False},
            "axisLabel": {"show": not has_volume},
        }
    ]
    y_axes: list[dict[str, Any]] = [
        {
            "type": "value",
            "scale": True,  # K 线纵轴不能从 0 起, 否则波动被压平
            "splitArea": {"show": False},
            **({"min": display.y_min} if display.y_min is not None else {}),
            **({"max": display.y_max} if display.y_max is not None else {}),
        }
    ]

    if has_volume and volume_grid is not None:
        grids.append(volume_grid)
        x_axes.append(
            {
                "type": "category",
                "gridIndex": 1,
                "data": times,
                "boundaryGap": True,
                "axisLine": {"onZero": False},
                "splitLine": {"show": False},
            }
        )
        y_axes.append(
            {
                "type": "value",
                "gridIndex": 1,
                "splitNumber": 2,
                "axisLabel": {"show": True},
                "splitLine": {"show": False},
            }
        )
        series.append(
            {
                "name": "成交量",
                "type": "bar",
                "xAxisIndex": 1,
                "yAxisIndex": 1,
                "data": volume,
                "itemStyle": {"color": "#9aa4b2"},
            }
        )

    option: dict[str, Any] = {
        "animation": display.animation,
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        "legend": {
            "show": display.legend,
            "top": _LEGEND_TOP,
            "data": [s["name"] for s in series],
        },
        "grid": grids,
        "xAxis": x_axes,
        "yAxis": y_axes,
        "series": series,
    }
    if display.zoom:
        x_indices = list(range(len(x_axes)))
        # 默认只展示最后约 120 根, 否则长区间下每根 K 线只有几个像素宽。
        start = max(0.0, 100.0 - 120.0 / frame.n_rows * 100.0) if frame.n_rows else 0.0
        option["dataZoom"] = [
            {"type": "inside", "xAxisIndex": x_indices, "start": start, "end": 100},
            {"type": "slider", "xAxisIndex": x_indices, "start": start, "end": 100,
             "bottom": _SLIDER_BOTTOM, "height": _SLIDER_H},
        ]
    return {
        "title": _kline_title(source),
        "subtitle": _kline_subtitle(source, frame, anchor),
        "option": option,
    }


def _kline_title(source: KlineSource) -> str:
    freq = "日线" if source.frequency == "daily" else "1 分钟"
    return f"{source.security}  {freq} K 线"


_RECIPES = {"kline": _compile_kline}
CHARTS = tuple(_RECIPES)


def compile_recipe(
    chart: str,
    source: BaseModel,
    display: PlotDisplay | None,
    frame: PlotFrame,
    anchor: dict | None = None,
) -> dict[str, Any]:
    """把 (模板, 来源, 显示项, 数据) 编译成带版本的 envelope."""
    recipe = _RECIPES.get(chart)
    if recipe is None:
        raise PlotError(f"未知图表模板 {chart!r}。可用: {', '.join(CHARTS)}")
    display = display or PlotDisplay()
    _check_color(display.up_color, "up_color")
    _check_color(display.down_color, "down_color")
    if display.y_min is not None and display.y_max is not None and display.y_min >= display.y_max:
        raise PlotError(f"display.y_min({display.y_min}) 必须小于 y_max({display.y_max})。")

    built = recipe(source, display, frame, anchor)
    return {
        "schema_version": SCHEMA_VERSION,
        "chart": chart,
        "renderer": "echarts",
        # 标题不进 option: canvas 里的文字不换行, 窄屏会把承载复权口径的副标题裁掉。
        "title": built["title"],
        "subtitle": built["subtitle"],
        "option": built["option"],
        "meta": {
            "source": source.model_dump(),
            "rows": frame.n_rows,
            "columns": frame.columns,
            "fq_anchor": anchor,
        },
    }


def envelope_summary(envelope: dict[str, Any]) -> str:
    """给模型看的一行摘要。全量数据只进 structuredContent, 不进这里."""
    return f"已渲染 {envelope['chart']} 图 · {envelope['title']} · {envelope['subtitle']}"
