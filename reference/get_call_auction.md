获取股票集合竞价
历史范围：2005年至今；更新时间：盘后15点,24:00校对完成入库
get_call_auction(security, start_date, end_date, fields=None)
描述

支持股票（2010年至今）的集合竞价，当日的集合竞价数据于盘后15点返回。
为了防止返回数据量过大, 我们每次最多返回10000行。
股票集合竞价字段

参数：
security: 股票（2010年至今）
start_date: 开始日期，YYYY-MM-DD格式
end_date: 结束日期，YYYY-MM-DD格式
fields: 选择要获取的行情数据字段，参数为list格式，默认为None，返回全部字段。
fields字段说明
返回指定时间区间标的集合竞价tick数据，返回字段结果如下：
字段名	说明	字段类型
time	时间	datetime
current	当前价（不复权）	float
volume	累计成交量（股）	float
money	累计成交额（元）	float
a1_v~a5_v	五档卖量	float
a1_p~a5_p	五档卖价	float
b1_v~b5_v	五档买量	float
b1_p~b5_p	五档买价	float
#获取平安银行2019-09-02至2019-09-05期间的集合竞价数据
df=get_call_auction('000001.XSHE','2022-09-02','2022-09-05')
print(df)

          code                time  current    volume      money   a1_p  \
0  000001.XSHE 2022-09-02 09:25:00    12.62  369458.0  4662559.0  12.63   
1  000001.XSHE 2022-09-05 09:25:00    12.46  394700.0  4917962.0  12.46   

     a1_v   a2_p     a2_v   a3_p     a3_v   a4_p    a4_v   a5_p     a5_v  \
0  8200.0  12.64   5000.0  12.65  12900.0  12.66  4500.0  12.67   4900.0   
1  9355.0  12.47  28000.0  12.48  58100.0  12.49  4600.0  12.50  13400.0   

    b1_p      b1_v   b2_p     b2_v   b3_p     b3_v   b4_p     b4_v   b5_p  \
0  12.62   95142.0  12.61  26200.0  12.60  83500.0  12.59  15500.0  12.58   
1  12.45  638300.0  12.44  95900.0  12.43  66000.0  12.42  59000.0  12.41   

       b5_v  
0  107600.0  
1  217500.0  

获取场内基金集合竞价
历史范围：2017-01-01至今；
get_call_auction(security, start_date, end_date, fields=None)
描述

支持场内基金（2019年至今）的集合竞价，当日的集合竞价数据于盘后15点返回。
为了防止返回数据量过大, 我们每次最多返回10000行。
基金集合竞价

**参数**：
security: 场内基金（2019年至今）
start_date: 开始日期，YYYY-MM-DD格式
end_date: 结束日期，YYYY-MM-DD格式
fields: 选择要获取的行情数据字段，参数为list格式，默认为None，返回全部字段。
返回值：
返回指定时间区间标的集合竞价tick数据，返回字段结果如下：
字段名	说明	字段类型
time	时间	datetime
current	当前价（不复权）	float
volume	累计成交量（股）	float
money	累计成交额（元）	float
a1_v~a5_v	五档卖量	float
a1_p~a5_p	五档卖价	float
b1_v~b5_v	五档买量	float
b1_p~b5_p	五档买价	float
#获取159003.XSHE招商快线2019-3-05至2019-3-06期间的集合竞价数据
df=get_call_auction('159003.XSHE','2019-3-05','2019-3-06')
print(df)

          code                time  current   volume      money   a1_p  \
0  159003.XSHE 2019-03-05 09:25:03    100.0  16300.0  1629951.1  100.0   
1  159003.XSHE 2019-03-06 09:25:03    100.0  29300.0  2929941.4  100.0   

      a1_v   a2_p     a2_v   a3_p   ...     b1_p    b1_v    b2_p    b2_v  \
0  19412.0  100.0  21915.0  100.0   ...    100.0  1500.0   99.99  5300.0   
1   9199.0  100.0  49813.0  100.0   ...    100.0   300.0  100.00   500.0   

     b3_p    b3_v   b4_p    b4_v   b5_p    b5_v  
0   99.99   900.0  99.99  3000.0  99.99  1000.0  
1  100.00  1200.0  99.99  6500.0  99.99  9400.0  

[2 rows x 25 columns]


获取指数集合竞价
历史范围：2017年至今；
get_call_auction(security, start_date, end_date, fields=None)
描述

支持指数（2017年至今）的集合竞价，当日的集合竞价数据于盘后15点返回。
为了防止返回数据量过大, 我们每次最多返回5000行。
指数集合竞价信息

参数：
security: 指数（20117年至今）
start_date: 开始日期，YYYY-MM-DD格式
end_date: 结束日期，YYYY-MM-DD格式
fields: 选择要获取的行情数据字段，参数为list格式，默认为None，返回全部字段。
返回值：
返回指定时间区间标的集合竞价tick数据，返回字段结果如下：
字段名	说明	字段类型
time	时间	datetime
current	当前价（不复权）	float
volume	累计成交量（股）	float
money	累计成交额（元）	float
a1_v~a5_v	五档卖量	float
a1_p~a5_p	五档卖价	float
b1_v~b5_v	五档买量	float
b1_p~b5_p	五档买价	float
#获取沪深300指数2017-09-01至2017-09-05期间的集合竞价数据
df=get_call_auction('000300.XSHG','2017-09-01','2017-09-05')
print(df)

          code                time  current       volume         money  a1_p  \
0  000300.XSHG 2017-09-01 09:25:12  3825.34   60406600.0  6.963988e+08  None   
1  000300.XSHG 2017-09-04 09:25:10  3828.54  145635600.0  1.222139e+09  None   
2  000300.XSHG 2017-09-05 09:25:13  3845.55   59488700.0  6.133801e+08  None   

   a1_v  a2_p  a2_v  a3_p  ...   b1_p  b1_v  b2_p  b2_v  b3_p  b3_v  b4_p  \
0  None  None  None  None  ...   None  None  None  None  None  None  None   
1  None  None  None  None  ...   None  None  None  None  None  None  None   
2  None  None  None  None  ...   None  None  None  None  None  None  None   

   b4_v  b5_p  b5_v  
0  None  None  None  
1  None  None  None  
2  None  None  None  

[3 rows x 25 columns]