# 估值数据(市值、市盈率、市净率等)

- 历史范围:2005年至今
- 更新时间:每天盘前(08:30)更新当日总股本及流通股本数据,便于用户盘中计算各类指标,其他字段置空
- 每天盘后(16:30)更新全部指标
`get_fundamentals(query_object, date=None, statDate=None)`

## 描述

查询valuation财务数据
市值数据每天更新,可以使用get_fundamentals(query(valuation),date),指定date为某一交易日,获取该交易日的估值数据。
获取多个标的在指定交易日范围内的市值表数据

## 注意

- date和statDate参数只能传入一个
- 传入date时, 查询指定日期date收盘后所能看到的最近(对市值表来说, 最近一天, 对其他表来说, 最近一个季度)的数据
- statDate: 财报统计的季度或者年份。
- 季度: 格式是年 + 'q' + 季度序号, 例如: '2015q1', '2013q4'. 年份: 格式就是年份的数字, 例如:'2015', '2016'.
- 为了防止返回数据量过大, 我们每次最多返回10000行
- 新增 get_table_info(table) 方法,支持查询数据表中的字段信息
query函数的更多用法详见:[query简易教程]

## valuation估值数据表

字段表已整理为 CSV:[`tables/valuation_fields.csv`](tables/valuation_fields.csv)。
