业绩快报(新)
历史范围:2005年至今;更新时间:交易日24:00更新
finance.run_query(query(finance.STK_PERFORMANCE_LETTERS).filter(finance.STK_PERFORMANCE_LETTERS.code==code).limit(n))
描述

获取上市公司业绩快报信息

参数

query(finance.STK_PERFORMANCE_LETTERS):表示从finance.STK_PERFORMANCE_LETTERS这张表中查询上市公司业绩报告的字段信息,还可以指定所要查询的字段名,格式如下:query(库名.表名.字段名1,库名.表名.字段名2),多个字段用逗号分隔进行提取;query函数的更多用法详见:sqlalchemy.orm.query.Query对象
finance.STK_PERFORMANCE_LETTERS:代表上市公司业绩预告表,收录了上市公司的业绩预告信息,表结构和字段信息如下:
注意

为了防止返回数据量过大, 我们每次最多返回5000行
不能进行连表查询,即同时查询多张表的数据
新增 run_offset_query方法,支持分批从数据库提取超5000条的数据,上限20万条
新增 get_table_info(table) 方法,支持查询数据表中的字段信息

query函数的使用技巧

query函数的更多用法详见:query简易教程
业绩快报
字段	名称	类型	注释
company_id	机构ID	INTEGER(11)	
company_name	公司名称	VARCHAR(100)	
code	股票代码	VARCHAR(12)	
name	股票简称	VARCHAR(12)	
pub_date	公布日期	DATE	
start_date	开始日期	DATE	
end_date	截至日期	DATE	
report_date	报告期	DATE	
report_type	报告期类型	int	0:本期,1:上期
total_operating_revenue	营业总收入	DECIMAL(20, 4)	
operating_revenue	营业收入	DECIMAL(20, 4)	
operating_profit	营业利润	DECIMAL(20, 4)	
total_profit	利润总额	DECIMAL(20, 4)	
np_parent_company_owners	归属于母公司所有者的净利润	DECIMAL(20, 4)	
total_assets	总资产	DECIMAL(20, 4)	
equities_parent_company_owners	归属于上市公司股东的所有者权益	DECIMAL(20, 4)	
basic_eps	基本每股收益	DECIMAL(20, 4)	
weight_roe	净资产收益(加权)	DECIMAL(20, 4)	
示例
from jqdatasdk import finance 
a=finance.run_query(query(finance.STK_PERFORMANCE_LETTERS).filter(finance.STK_PERFORMANCE_LETTERS.code=='000001.XSHE').limit(3))
print(a)

   id  company_id company_name         code  name    pub_date  start_date  \
0   1   430000001   平安银行股份有限公司  000001.XSHE  平安银行  2019-01-04  2018-01-01   
1   2   430000001   平安银行股份有限公司  000001.XSHE  平安银行  2019-01-04  2017-01-01   
2   3   430000001   平安银行股份有限公司  000001.XSHE  平安银行  2020-01-14  2019-01-01   

     end_date report_date  report_type total_operating_revenue  \
0  2018-12-31  2018-12-31            0                    None   
1  2017-12-31  2018-12-31            1                    None   
2  2019-12-31  2019-12-31            0                    None   

   operating_revenue  operating_profit  total_profit  \
0       1.167160e+11      3.230500e+10  3.223100e+10   
1       1.057860e+11      3.022300e+10  3.015700e+10   
2       1.379580e+11      3.628900e+10  3.624000e+10   

   np_parent_company_owners  total_assets  equities_parent_company_owners  \
0              2.481800e+10  3.420753e+12                             NaN   
1              2.318900e+10  3.248474e+12                             NaN   
2              2.819500e+10  3.939070e+12                    3.129830e+11   

   basic_eps  weight_roe  
0        NaN       11.49  
1        NaN       11.62  
2    16.1282       11.30 