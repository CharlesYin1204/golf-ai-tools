# 高尔夫AI工具

高尔夫AI工具是一套覆盖球友、球童、球场和运营人员的高尔夫业务平台，将球场时段、预约订单、球局成绩、球队活动、球童服务、教学内容和 AI 复盘连接到统一系统中。

## 主要功能

- 球场、时段库存、预约、订单和到场管理。
- 18 洞记分、历史成绩、复盘和训练计划。
- 球队、活动、报名、教学内容和个人收藏。
- 球童任务、服务记录、异常反馈和球包管理。
- AI 问答、成绩复盘、球场推荐、知识库和运营分析。
- 用户端、球童端、运营后台和 Android 客户端。

## 产品界面

![电脑端首页](outputs/ui-desktop.png)

![运营后台](outputs/ui-admin.png)

## 技术架构

- Python 3.10+ 原生 HTTP 服务。
- 原生 HTML、CSS、JavaScript 前端。
- MySQL 5.7/8.0、PyMySQL。
- Pillow、二维码处理、Android WebView。
- Nginx、systemd、Certbot 和定时备份。

## 环境要求

- Python 3.10+、MySQL 5.7/8.0。
- Nginx 与 systemd 用于生产部署；AI 功能需要私有模型配置。

## 本地安装

```bash
git clone https://github.com/CharlesYin1204/golf-ai-tools.git
cd golf-ai-tools
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp deploy/gaoqiu.env.example .env
set -a && source .env && set +a
python server.py
```

默认访问 `http://localhost:8088`。首次空库可通过 `GOLFERA_ADMIN_PASSWORD` 创建管理员；已有数据库不要重新设置初始化密码。

## 环境变量

| 配置 | 说明 |
| --- | --- |
| `GOLFERA_MYSQL_HOST`、`GOLFERA_MYSQL_PORT` | MySQL 地址和端口 |
| `GOLFERA_MYSQL_USER`、`GOLFERA_MYSQL_PASSWORD`、`GOLFERA_MYSQL_DATABASE` | 数据库凭据 |
| `GOLFERA_STATIC_DIR` | `dist` 静态资源绝对路径 |
| `GOLFERA_BACKUP_DIR` | 数据库备份目录 |
| `HOST`、`PORT` | 监听地址和端口 |
| `GOLFERA_ADMIN_PASSWORD` | 仅全新空库首次初始化使用 |

AI 服务地址、密钥、Android 签名和管理员交付信息均应保存在私有配置中，不提交 Git。

## 测试与验证

```bash
python tests/backend_test.py
```

项目还包含数据库迁移、AI、权限和浏览器流程测试。涉及真实数据库的测试必须使用隔离实例，并在执行前确认不会修改生产数据。

## 生产部署

1. 安装 Python、MySQL、Nginx，创建 `golfera` 系统用户和 `gaoqiu` 数据库用户。
2. 将代码部署到固定目录，创建 `.venv` 并安装 `requirements.txt`。
3. 将 `deploy/gaoqiu.env.example` 复制到 `/etc/gaoqiu.env`，设置 `root:golfera` 和权限 `640`。
4. 创建数据库；如从 SQLite 迁移，先备份并使用项目迁移/校验脚本核对数量和哈希。
5. 安装 `deploy/golfera.service`，确认服务以非 root 用户运行。
6. 使用 `deploy/gaoqiu-https.conf` 配置 Nginx 与 HTTPS。
7. 启用 `golfera-backup.timer` 定时备份；按需启用 AI 留存清理和证书续期 timer。
8. 检查健康接口、登录、预约、记分、后台回读和服务重启后的数据持久化。

更新前先执行数据库备份，保存环境配置和 Android 签名，再替换代码、更新依赖并重启服务。数据库结构变更必须先在隔离库验证。

## 项目结构

- `server.py`：服务入口和 HTTP 路由。
- `ai_service.py`、`ai_features.py`：AI 接入与业务能力。
- `dist/`：用户端和管理后台静态资源。
- `tests/`：后端、数据库、AI 和权限测试。
- `deploy/`：环境模板、Nginx、systemd、备份与证书任务。
- `android/`：Android 客户端相关文件。

## 常见问题

- 服务无法启动：检查 `/etc/gaoqiu.env` 权限、虚拟环境和数据库连接。
- 后台一直连接中：确保访问 `/admin/` 且静态资源反向代理路径正确。
- AI 功能失败：检查私有模型配置、网络、超时和用量限制。
- 数据备份失败：检查 `mysqldump`、备份目录权限和磁盘空间。

## 联系方式

微信：`charlesyin6015`
