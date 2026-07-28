# Vendored 前端资产

这两个 bundle 被内联进 `ui://kedu/chart/...` 资源。**内联而不是走 CDN,也不是由 kedu 自己
开 HTTP 托管**:

- 走 CDN 要求渲染 iframe 的浏览器能出网,kedu 的定位是全本地;
- 自己开 HTTP 要求把 MCP 端口暴露给浏览器,和 `_dsl` 那套「受限 eval 假定调用方可信、
  只绑 127.0.0.1」的前提直接冲突(见 `ecosystem.config.js` 的 kedu-mcp 注释)。

MCP Apps 的 UI 资源经 `resources/read` 走协议下发,内联后两个要求都不成立。

## 清单

| 文件 | 版本 | 大小 | 许可 | 来源 |
|---|---|---:|---|---|
| `echarts.min.js` | 6.1.0 | 1,121,883 B | Apache-2.0 | `https://cdn.jsdelivr.net/npm/echarts@6.1.0/dist/echarts.min.js` |
| `ext-apps.js` | 1.7.5 | 337,419 B | MIT | `https://cdn.jsdelivr.net/npm/@modelcontextprotocol/ext-apps@1.7.5/dist/src/app-with-deps.js` |

`ext-apps.js` 取的是 `app-with-deps` 入口(自带依赖的 ESM bundle),不是默认的 `.` 入口 ——
后者把依赖留在外部 import 上,内联环境里解析不了。

## 为什么必须是这两个具体构建

- **ECharts 用全量 `echarts.min.js`**,不用 `common`/`simple` 裁剪版:实测 `common` 无
  heatmap/boxplot,`simple` 连 candlestick 都没有。按需自定义构建要引 node 构建链,
  对纯 Python 项目不划算。
- **ECharts 必须以 classic `<script>` 载入**:它是 UMD,在 `type="module"` 作用域下拿不到
  全局挂载点。ext-apps 是 ESM,只能以 `type="module"` 载入。所以模板里是两个 script。

## CSP:两处 `new Function` 在装配期被改掉

MCP Apps 规范强制 host 构造的 CSP 是 `script-src 'self' 'unsafe-inline' <resourceDomains>`,
**没有 `unsafe-eval` 且服务端无法申请**。两个 bundle 各含 1 处 `new Function`:

| 文件 | 片段 | 是否执行 | 装配期改写为 |
|---|---|---|---|
| `ext-apps.js` | `try{return new Function(""),!0}catch(r){return!1}` | **会执行** | `return!1` |
| `echarts.min.js` | `new Function("return ("+i+");")()` | 不可达 | `JSON.parse(i)` |

ext-apps 那处是 Zod 的 JIT 可用性探针,**它真的会执行**。虽然 `try/catch` 吞掉了 EvalError、
图也照常显示,但每次加载都会在浏览器留下一条真实的 CSP 违规报告 —— MCPJam 的 Strict 模式
实测捕获到了它(报告列号与该片段在 bundle 内的偏移 11631 精确吻合)。直接返回 `false` 的
结果与 CSP 下的实际行为完全一致,只是少一条报告。

echarts 那处在 `"undefined"!=typeof JSON&&JSON.parse` 为假时才走,现代浏览器不可达;换成
`JSON.parse(i)` 语义不变。改它的意义是让「装配产物里零个 `new Function`」成立 —— 这是个
可证的不变量,而「计数没变」证明不了任何关于热路径的事。

改写在 `_app._PATCHES` 里,每条都断言目标片段恰好出现一次,**换版本后片段一变就 fail-fast**,
不会静默不打补丁。两者仍然都不需要声明任何 `csp.resourceDomains`。

## 更新方式

直接覆盖文件即可 —— `_app.py` 按内容哈希算 URI,资源体一变 URI 就变。注意内容哈希只保证
**会缓存的 host** 不会拿旧渲染器配新 envelope;它不保证 host 真的缓存(实测 MCPJam 1.5.17
每次工具调用都重新 `resources/read`,1.39 MiB 每次都传)。

改完记得同步本文件的版本与字节数、重新确认上面两处补丁片段是否还在,并重跑
`tests/test_plot.py`。
