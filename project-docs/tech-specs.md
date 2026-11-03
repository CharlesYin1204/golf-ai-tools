# 技术规格

## 架构与已验证迁移

前端为原生HTML、CSS、JavaScript，移动端与admin后台独立入口，无构建依赖。服务端为Python HTTP服务。当前数据库为MySQL 5.7.43，连接 `127.0.0.1:3306`，数据库与连接用户名均为 `gaoqiu`。管理员原账号和密码不变，迁移保留原密码哈希。

2026-10-12 21:33（北京时间）已完成MySQL迁移和新目录服务切换；原18表444条逐行一致，管理员密码哈希、会话与访客记录保留。前后台联动、重启持久化与备份恢复均已通过。MySQL兼容层、PyMySQL依赖与迁移实现已通过真实数据库测试。

初版采用SQLite外键、WAL和事务校验，已完成独立回归与部署；历史记录保留。旧SQLite文件保留用于迁移核对和回退，不作为新版本维护说明的主数据库。

## 数据与接口

MySQL已承接既有18张业务与系统表，另有事务锁表，合计19表。13个管理模型：courses、slots、users、caddies、orders、rounds、teams、events、registrations、service_tickets、bag_records、lessons、audit_logs。接口以 `{ok,data}` 或 `{ok:false,error:{message}}` 响应。`/api/admin` 需要管理员会话；`/api/mobile/state` 由访客Cookie隔离；`/api/public` 提供公开演示球场和库存。

金额由时段单价和人数计算；库存校验与写入必须处于同一事务；18洞1—20杆才能完成球局；球包按领取、归还顺序；报名校验状态与容量；revision避免旧页面覆盖后台修改。密码PBKDF2哈希、HttpOnly会话、写请求同源校验与登录失败限流。真实MySQL19项测试全部通过，覆盖约束、并发库存、事务、前后台联动与迁移。

## 后台入口修复

访问 `/admin` 时曾直接返回目录首页，相对 `admin.js`、`admin.css` 被浏览器解析为根路径 `/admin.js`、`/admin.css`，脚本未加载，界面一直“正在连接”。源码已对目录入口加302重定向至 `/admin/`，保留查询参数。公网 `/admin` 已验证302跳转 `/admin/`；8份前后台资源均200且SHA与本地源码一致。原管理员账号密码登录成功，透传浏览器后台已连接并显示统计与订单，控制台无error/warn。截图见 `outputs/admin-mysql.png`。

## 部署与备份

端口8088。代码目录 `/www/wwwroot/gaoqiu`，环境配置 `/etc/gaoqiu.env`，服务仍为 `golfera`，ExecStart为 `/www/wwwroot/gaoqiu/.venv/bin/python /www/wwwroot/gaoqiu/server.py`。MySQL密码由部署配置保存，不写入源码和公开说明。

每天北京时间03:15使用 `mysqldump` 一致性事务导出，保留14份，目录 `/www/wwwroot/gaoqiu/data/backups`。服务和timer已启用，下次执行为2026-10-13 03:15；首份备份权限600、gzip检查和隔离MySQL实际恢复均通过。手动备份：`systemctl start golfera-backup.service`。新路径WorkingDirectory/ExecStart已确认，服务与timer均enabled；21:37重启前后两条订单完全一致。

初版目录 `/opt/golfera` 与SQLite backup API属于历史方案；当前以新目录、MySQL和新备份任务为准。

## 功能边界

业务与用户资料为模拟数据，未接入真实球场。AI问答与复盘为固定知识和数字计算模板；多人同步PK、精准定位测距、实付退款、真实视频、通知与硬件均未接入。


2026-10-13 更新：AI 已由固定模板升级为真实模型，后台增加模型配置与测试，移动端增加多轮问答、球场推荐、成绩复盘及一周练习计划。早期演示说明保留作为历史记录，当前配置与验证以根目录 README 的 AI 接入章节为准。
