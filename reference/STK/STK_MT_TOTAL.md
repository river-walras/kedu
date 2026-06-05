融资融券汇总数据
历史范围：2010年至今；更新时间：下一个交易日9点之前更新
from jqdatasdk import *
finance.run_query(query(finance.STK_MT_TOTAL).filter(finance.STK_MT_TOTAL.date=='2019-05-23').limit(n))
描述

记录上海交易所和深圳交易所的融资融券汇总数据

返回结果

返回一个 dataframe，每一行对应数据表中的一条数据， 列索引是您所查询的字段名称

注意

为了防止返回数据量过大, 我们每次最多返回5000行
不能进行连表查询，即同时查询多张表的数据
新增 run_offset_query方法，支持分批从数据库提取超5000条的数据，上限20万条
新增 get_table_info(table) 方法，支持查询数据表中的字段信息

query函数的使用技巧

query函数的更多用法详见：query简易教程
参数
参数	含义	使用
query(finance.STK_MT_TOTAL)	表示从finance.STK_MT_TOTAL这张表查询融资融券汇总数据	指定所要查询的字段名，格式如下：
query(库名.表名.字段名1，库名.表名.字段名2），
多个字段用逗号分隔进行提取；
finance.STK_MT_TOTAL	融资融券汇总数据	具体信息如下表
filter(finance.STK_MT_TOTAL.date==date)	指定筛选条件	通过finance.STK_MT_TOTAL.date==date可以指定你想要查询的日期；
除此之外，还可以对表中其他字段指定筛选条件；多个筛选条件用英文逗号分隔。
limit(n)	限制返回的数据条数	n指定返回条数
字段设计
名称	类型	描述
date	date	交易日期
exchange_code	varchar(12)	交易市场。例如，XSHG-上海证券交易所；XSHE-深圳证券交易所。
fin_value	decimal(20,2)	融资余额（元）
fin_buy_value	decimal(20,2)	融资买入额（元）
sec_volume	int	融券余量（股）
sec_value	decimal(20,2)	融券余量金额（元）
sec_sell_volume	int	融券卖出量（股）
fin_sec_value	decimal(20,2)	融资融券余额（元）
示例：
#查询2019-05-23的融资融券汇总数据。
df=finance.run_query(query(finance.STK_MT_TOTAL).filter(finance.STK_MT_TOTAL.date=='2019-05-23'))
print(df)

     id        date exchange_code     fin_value  fin_buy_value  sec_volume  \
0  4445  2019-05-23          XSHE  3.601290e+11   1.460400e+10   136000000   
1  4446  2019-05-23          XSHG  5.605276e+11   1.906461e+10   930593681   

      sec_value  sec_sell_volume  fin_sec_value  
0  1.465000e+09         26000000   3.615940e+11  
1  6.018287e+09        144633497   5.665458e+11  