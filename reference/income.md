# income利润表

- 历史范围:2005年至今
- 更新时间:交易日24:00更新
`get_fundamentals(query_object, date=None, statDate=None)`

## 描述

查询income利润表
按季度更新, 统计周期是一季度。可以使用get_fundamentals(query_object, date=None, statDate=None)查询
获取多日财务数据get_fundamentals_continuously
获取多个季度/年度的历史财务数据get_history_fundamentals

## 注意

- date和statDate参数只能传入一个
- 传入date时, 查询指定日期date收盘后所能看到的最近(对市值表来说, 最近一天, 对其他表来说, 最近一个季度)的数据
- statDate: 财报统计的季度或者年份。
- 季度: 格式是年 + 'q' + 季度序号, 例如: '2015q1', '2013q4'. 年份: 格式就是年份的数字, 例如:'2015', '2016'.
- 为了防止返回数据量过大, 我们每次最多返回5000行
- 新增 get_table_info(table) 方法,支持查询数据表中的字段信息

## query 函数的使用技巧

query函数的更多用法详见:query简易教程

## income利润表

字段表已整理为 CSV:[`tables/income_fields.csv`](tables/income_fields.csv)。
