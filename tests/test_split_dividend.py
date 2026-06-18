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
