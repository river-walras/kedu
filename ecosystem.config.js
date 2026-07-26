// pm2 编排:盘后增量更新本地聚宽数据。
//   pm2 start ecosystem.config.js            # 注册(首次必须执行)
//   pm2 save                                 # 持久化,开机/重启后自动拉起
//   pm2 logs kedu-update                     # 看日志
//   pm2 restart kedu-update                  # 手动触发一次
//
// PM2 daemon 当前按 UTC 解析 cron；10:30 UTC 即北京时间工作日 18:30（A 股盘后）。
// `uv run --env-file .env` 把 .env 注入进程环境(项目已去 python-dotenv,凭证仅留未提交的 .env)。
// autorestart:false 表示跑完即停(cron 再唤起)。依赖顺序由 update_jqdata.py 内部保证。
module.exports = {
  apps: [
    {
      name: "kedu-update",
      cwd: __dirname,
      script: "uv",
      args: "run --env-file .env python -u scripts/update_jqdata.py",
      interpreter: "none",
      autorestart: false,
      cron_restart: "30 10 * * 1-5",
      max_memory_restart: "2G",
      time: true,
    },
  ],
};
