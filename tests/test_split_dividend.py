from __future__ import annotations

import datetime as dt

import pandas as pd

import kedu.split_dividend as sd


class _Field:
    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, value):  # noqa: ANN001
        return (self.name, "=", value)

    def __ge__(self, value):  # noqa: ANN001
        return (self.name, ">=", value)

    def __le__(self, value):  # noqa: ANN001
        return (self.name, "<=", value)


class _Table:
    code = _Field("code")
    a_xr_date = _Field("a_xr_date")
    change_date = _Field("change_date")
    change_reason_id = _Field("change_reason_id")


class _Query:
    def __init__(self, table) -> None:  # noqa: ANN001
        self.table = table
        self.filters = []

    def filter(self, *conditions):  # noqa: ANN001
        self.filters.extend(conditions)
        return self


class _Finance:
    STK_XR_XD = _Table()
    STK_CAPITAL_CHANGE = _Table()
    FUND_DIVIDEND = _Table()

    def __init__(self, frames: list[pd.DataFrame]) -> None:
        self.frames = list(frames)
        self.queries = []

    def run_query(self, query_object):  # noqa: ANN001
        self.queries.append(query_object)
        return self.frames.pop(0)


def _patch(monkeypatch, frames: list[pd.DataFrame]) -> _Finance:
    finance = _Finance(frames)
    monkeypatch.setattr(sd, "finance", finance)
    monkeypatch.setattr(sd, "query", lambda table: _Query(table))
    return finance


def test_get_split_dividend_maps_stock_xr_xd(monkeypatch) -> None:
    _patch(
        monkeypatch,
        [
            pd.DataFrame(
                [
                    {
                        "code": "000001.XSHE",
                        "a_xr_date": dt.date(2020, 6, 1),
                        "dividend_ratio": 2.0,
                        "transfer_ratio": 1.0,
                        "bonus_ratio_rmb": 4.0,
                    }
                ]
            )
        ],
    )

    records = sd.get_split_dividend("000001.XSHE", "2020-01-01", "2020-12-31")

    assert records == [
        {
            "date": dt.date(2020, 6, 1),
            "bonus_pre_tax": 0.4,
            "scale_factor": 1.3,
        }
    ]


def test_get_split_dividend_uses_implement_base_only_when_revised(
    monkeypatch,
) -> None:
    """No single base field is universally correct -- which one JoinQuant
    actually books depends on whether the distribution base was revised
    between shareholder approval and implementation:

    * 000898.XSHE 2021-06-23: ``distributed_share_base_implement``
      (798806.0178) was revised DOWN from the board/shareholders-approved
      939960.0178 -- JoinQuant books the revised implementation base:
      ``67099.7055 / 798806.0178 = 0.084`` (verified against the real
      position in ``tests/strategies/board_continuation/``: 开仓均价 4.76 ->
      4.676 exactly, cash += 8400*0.084*0.8 = 564.48 exactly).
    * 603798.XSHG 2021-06-10: all three distribution-base fields are
      identical (19726.8961, unrevised) -- JoinQuant instead books
      ``total_capital_before_transfer``: ``2367.2275 / 20000 = 0.1184``
      (verified against the real position in
      ``tests/strategies/small_cap/``: realized P&L on the 06-10 sell is
      141.24 = ``(10.50 - (10.49 - 0.1184)) * 1100`` exactly; the unrevised
      implement base would give 0.12, which reproduces neither)."""
    _patch(
        monkeypatch,
        [
            pd.DataFrame(
                [
                    {
                        "code": "000898.XSHE",
                        "a_xr_date": dt.date(2021, 6, 23),
                        "dividend_ratio": None,
                        "transfer_ratio": None,
                        "bonus_ratio_rmb": 0.84,
                        "bonus_amount_rmb": 67099.7055,
                        "total_capital_before_transfer": 940525.0201,
                        "distributed_share_base_board": 939960.0178,
                        "distributed_share_base_shareholders": 939960.0178,
                        "distributed_share_base_implement": 798806.0178,
                    },
                    {
                        "code": "603798.XSHG",
                        "a_xr_date": dt.date(2021, 6, 10),
                        "dividend_ratio": None,
                        "transfer_ratio": None,
                        "bonus_ratio_rmb": 1.2,
                        "bonus_amount_rmb": 2367.2275,
                        "total_capital_before_transfer": 20000.0,
                        "distributed_share_base_board": 19726.8961,
                        "distributed_share_base_shareholders": 19726.8961,
                        "distributed_share_base_implement": 19726.8961,
                    },
                ]
            )
        ],
    )

    records = sd.get_split_dividend("000898.XSHE", "2021-01-01", "2021-12-31")

    assert records == [
        {
            "date": dt.date(2021, 6, 10),
            "bonus_pre_tax": 0.1184,
            "scale_factor": 1.0,
        },
        {
            "date": dt.date(2021, 6, 23),
            "bonus_pre_tax": 0.084,
            "scale_factor": 1.0,
        },
    ]


def test_get_split_dividend_maps_fund_with_otc_sentinel(monkeypatch) -> None:
    _patch(
        monkeypatch,
        [
            pd.DataFrame(
                [
                    {
                        "code": "000001.OF",
                        "ex_date": dt.date(2200, 1, 1),
                        "otc_ex_date": dt.date(2020, 5, 7),
                        "proportion": 0.03,
                        "split_ratio": 1.2,
                    },
                    {
                        "code": "000001.OF",
                        "ex_date": dt.date(2020, 6, 8),
                        "otc_ex_date": dt.date(2020, 6, 9),
                        "proportion": 0.04,
                        "split_ratio": None,
                    },
                ]
            )
        ],
    )

    records = sd.get_split_dividend("000001.OF", "2020-05-01", "2020-06-30")

    assert records == [
        {
            "date": dt.date(2020, 5, 7),
            "bonus_pre_tax": 0.03,
            "bonus_post_tax": 0.03,
            "scale_factor": 1.2,
        },
        {
            "date": dt.date(2020, 6, 8),
            "bonus_pre_tax": 0.04,
            "bonus_post_tax": 0.04,
            "scale_factor": 1.0,
        },
    ]


def test_get_capital_reform_dates_filters_reason(monkeypatch) -> None:
    finance = _patch(
        monkeypatch,
        [
            pd.DataFrame(
                [
                    {"code": "000001.XSHE", "change_date": dt.date(2007, 1, 4)},
                    {"code": "000001.XSHE", "change_date": dt.date(2007, 1, 5)},
                ]
            )
        ],
    )

    dates = sd.get_capital_reform_dates("000001.XSHE", "2007-01-01", "2007-12-31")

    assert dates == [dt.date(2007, 1, 4), dt.date(2007, 1, 5)]
    assert ("change_reason_id", "=", 306020) in finance.queries[0].filters


def test_public_exports() -> None:
    import kedu

    assert kedu.get_split_dividend is sd.get_split_dividend
    assert kedu.get_capital_reform_dates is sd.get_capital_reform_dates
