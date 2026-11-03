# 进度
2026-10-12：读取原文及8张截图；核对官网；生成约7300字产品分析草稿；草稿检查通过；正在创建并验证飞书文档。
创建成功并回读验证关键章节完整。交付：https://kkcym7x9wb.feishu.cn/docx/StQYdsJDUoaq9fxu6LHcEqH1nsf
后续改进：以跨端演示、经营指标和实际用户反馈补充证据，再确认迭代优先级。

2026-10-12：完成frontend-design移动UI、18洞记分/弱网、约球教学、球童任务与AI交互；新增13模型后台、真实SQLite与模拟数据，部署154.40.47.111:8088。服务golfera和备份timer正常，自启动已启用，数据库完整性ok。13项HTTP集成测试、28项后台VM、6项边界回归通过；浏览器实测前后台联动，320/375/390/414移动尺寸及390/768/1440后台无横向溢出。公网直接HTTP访问200。浏览器自动化无法直接检查远程IP页面，采用本地仅透传远程资产/API的检查窗口，仍操作同一台服务器数据库，并核对部署文件哈希一致。
反思：先把订场、记分、服务、库存和状态做可靠；AI保持明确演示范围。后续正式上线可增加真实身份/权限、支付和通知接入、真实球场地图及模型评测，按业务需要规划。

2026-10-12 21:33 后续调整（已完成）：用户指定代码目录 `/www/wwwroot/gaoqiu`，环境配置 `/etc/gaoqiu.env`，服务仍为 `golfera`，ExecStart为 `/www/wwwroot/gaoqiu/.venv/bin/python /www/wwwroot/gaoqiu/server.py`。MySQL 5.7.43，连接 `127.0.0.1:3306`，数据库与连接用户名均为 `gaoqiu`。管理员原账号和密码不变，迁移保留原密码哈希。每天北京时间03:15使用 `mysqldump` 一致性事务导出，保留14份，目录 `/www/wwwroot/gaoqiu/data/backups`。服务和timer已启用，下次执行为2026-10-13 03:15；首份备份权限600、gzip检查和隔离MySQL实际恢复均通过。手动备份：`systemctl start golfera-backup.service`。2026-10-12 21:33（北京时间）已完成MySQL迁移和新目录服务切换；原18表444条逐行一致，管理员密码哈希、会话与访客记录保留。前后台联动、重启持久化与备份恢复均已通过。

同时修复后台入口：访问 `/admin` 时曾直接返回目录首页，相对 `admin.js`、`admin.css` 被浏览器解析为根路径 `/admin.js`、`/admin.css`，脚本未加载，界面一直“正在连接”。源码已对目录入口加302重定向至 `/admin/`，保留查询参数。公网 `/admin` 已验证302跳转 `/admin/`；8份前后台资源均200且SHA与本地源码一致。原管理员账号密码登录成功，透传浏览器后台已连接并显示统计与订单，控制台无error/warn。截图见 `outputs/admin-mysql.png`。此前SQLite验收继续保留；本次MySQL独立验收及恢复证据已补充。

## 2026-10-12 MySQL交付与恢复完成证据

- 21:33迁移完成：原18张表444条逐行相同，管理员密码哈希、session和visitor全部保留；加入事务锁表后MySQL合计19表。
- 当前服务器MySQL 5.7.43健康ready；WorkingDirectory和ExecStart使用 `/www/wwwroot/gaoqiu`，服务与timer均enabled。21:37重启前后两条订单记录完全相同。
- 原 `admin` 账号密码登录成功；移动下单 `GE-LIVE-QA-1791812086` 在后台可查，后台到场修改在前台可回读，72杆球局成功归档。
- `/admin` 公网302至 `/admin/`；8份前后台资源200且哈希与本地源码一致；未登录管理员接口401，源码与备份URL404。透传浏览器后台已连接、显示统计与订单，控制台无error/warn，截图 `outputs/admin-mysql.png`。
- SQLite最新15项测试、实际MySQL19项测试全部通过。
- 首份备份 `/www/wwwroot/gaoqiu/data/backups/gaoqiu-20261012-213423-958660.sql.gz` 权限600，gzip校验通过。在隔离MySQL 5.7.24实际恢复19表、原18表444条、24条外键；受引用 `qizhong` 删除被1451拒绝；订单 `GE1791807777782` 仍confirmed、checked=1、total=1760。隔离测试库及临时实例已关闭，未修改线上数据库。
- 每日03:15 timer已启用，下次2026-10-13 03:15；保留14份。手动备份命令 `systemctl start golfera-backup.service`。

记录证据：`work/mysql-migration-verification.json`、`work/mysql-deployed-assets.json`、`work/gaoqiu-backup-restore-evidence.json`。旧 `/opt/golfera` 只作回退资料保留。

## 2026-11-03 GitHub 项目介绍整理

- README 改为正式产品介绍，补齐技术架构、部署步骤、目录结构和联系方式。
- GitHub 介绍页仅展示电脑端产品界面，不再展示移动端图片。
- 完成敏感信息治理：移除 AI 凭据、明文登录交付文件、旧源码副本与真实调用结果；私有资料仅保存在本机忽略目录。
