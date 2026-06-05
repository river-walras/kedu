沪深市场每日成交概况
历史范围：2010年至今；更新时间：交易日20:30-24:00更新
from jqdatasdk import finance
finance.run_query(query(finance.STK_EXCHANGE_TRADE_INFO).filter(finance.STK_EXCHANGE_TRADE_INFO.exchange_code==exchange_code).limit(n)
描述

记录沪深两市股票交易的成交情况，包括市值、成交量，市盈率等情况。
返回结果

返回一个dataframe，每一行对应数据表中的一条数据，列索引是你所查询的字段名称。

注意

为了防止返回数据量过大, 我们每次最多返回5000行
不能进行连表查询，即同时查询多张表的数据
新增 run_offset_query方法，支持分批从数据库提取超5000条的数据，上限20万条
新增 get_table_info(table) 方法，支持查询数据表中的字段信息

query函数的使用技巧

query函数的更多用法详见：query简易教程
参数

query(finance.STK_EXCHANGE_TRADE_INFO)：表示从finance.STK_EXCHANGE_TRADE_INFO这张表中查询沪深两市股票交易的成交情况，还可以指定所要查询的字段名，格式如下：query(库名.表名.字段名1，库名.表名.字段名2），多个字段用英文逗号进行分隔；query函数的更多用法详见：[query简易教程]
finance.STK_EXCHANGE_TRADE_INFO：代表沪深市场每日成交概况表，记录沪深两市股票交易的成交情况，包括市值、成交量，市盈率等情况，表结构和字段信息如下：
filter(finance.STK_EXCHANGE_TRADE_INFO.date==date)：指定筛选条件，通过finance.STK_EXCHANGE_TRADE_INFO.date==date可以指定你想要查询的交易日期；除此之外，还可以对表中其他字段指定筛选条件，如finance.STK_EXCHANGE_TRADE_INFO.exchange_code==322001，表示筛选市场编码为322001（上海市场）的数据；多个筛选条件用英文逗号分隔。
limit(n)：限制返回的数据条数，n指定返回条数。
表字段信息
字段名称	中文名称	字段类型	能否为空	含义
exchange_code	市场编码	varchar(12)	N	编码规则见下表
exchange_name	市场名称	varchar(100)		上海市场，上海A股，上海B股；
深圳市场，深市主板； 中小企业板，创业板
date	交易日期	date	N	
total_market_cap	市价总值	decimal(20,8)		单位：亿
circulating_market_cap	流通市值	decimal(20,8)		单位：亿
volume	成交量	decimal(20,4)		单位：万
money	成交金额	decimal(20,8)		单位：亿
deal_number	成交笔数	decimal(20,4)		单位：万笔
pe_average	平均市盈率	decimal(20,4)		上海市场市盈率计算方法：
市盈率＝∑(收盘价×发行数量)/∑(每股收益×发行数量)，统计时剔除亏损及暂停上市的上市公司。

深圳市场市盈率计算方法：
市盈率＝∑市价总值/∑(总股本×上年每股利润)，剔除上年利润为负的公司。
turnover_ratio	换手率	decimal(10,4)		单位：％
市场编码名称对照表
市场编码	交易市场名称	备注
322001	上海市场	
322002	上海A股	
322003	上海B股	
322004	深圳市场	该市场交易所未公布成交量和成交笔数
322005	深市主板	
322006	中小企业板	
322007	创业板	
示例
q=query(finance.STK_EXCHANGE_TRADE_INFO).filter(finance.STK_EXCHANGE_TRADE_INFO.date>='2022-01-01').limit(4)
df=finance.run_query(q)
print(df)

      id exchange_code exchange_name        date  total_market_cap  \
0  26844        322002          上海A股  2022-01-04     462131.962085   
1  26845        322003          上海B股  2022-01-04        789.618496   
2  26846        322001          上海市场  2022-01-04     518204.093361   
3  26847        322005          深市主板  2022-01-04     257369.250000   

   circulating_market_cap        volume        money  deal_number  pe_average  \
0           411793.297689  3.958120e+06  4572.352665    3648.5003      16.588   
1              789.618496  3.810280e+03     1.631958       1.6420      10.695   
2           434852.435733  4.064966e+06  5112.591514    3839.1096      17.959   
3           219041.670000  3.935100e+06  4588.340000    3661.3500      26.620   

   turnover_ratio  
0          1.1104  
1          0.2067  
2          1.1752  
3             NaN  