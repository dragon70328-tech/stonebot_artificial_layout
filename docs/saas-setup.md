# Stonebot SaaS 搭建记录

> 最后更新: 2026-08-03 18:30

---

## 目标

将现有的本地命令行排板系统，改造为可多用户在线使用的 SaaS 平台。

---

## 技术架构方案

| 层 | 技术选型 |
|---|---|
| 前端 | 待定（HTML/React/Vue） |
| 后端 API | FastAPI |
| 异步任务 | Celery + Redis |
| 数据库 | PostgreSQL |
| 部署 | 腾讯云，Nginx 反向代理，systemd |

**SaaS 化阶段：**

| 阶段 | 内容 | 状态 |
|------|------|------|
| MVP | FastAPI + 简单上传/下载界面 | 待开始 |
| 多用户 | 认证 + 任务队列 + 数据库 | 待开始 |
| SaaS | 支付 + 多租户 + 正式上线 | 待开始 |

---

## 服务器信息

| 项目 | 详情 |
|------|------|
| 云服务商 | 腾讯云 |
| 操作系统 | OpenCloudOS 9.4 |
| 管理面板 | 宝塔面板（端口 23524） |
| Web 服务器 | Nginx 1.26.3 |
| Docker | 未安装（暂不需要） |

**端口占用：**

| 端口 | 用途 |
|------|------|
| 80 | Nginx |
| 888 | Nginx |
| 8000 | 现有 stonebot 项目（Python） |
| 23524 | 宝塔面板 |

**现有项目：**

- 路径：`/opt/stonebot/`
- 运行方式：Python 进程，端口 8000
- 保留不动，与新 SaaS 共存

---

## 共用服务器方案

```
域名
├── stonebot.yourdomain.com  → 8000（旧版，保留）
└── nesting.yourdomain.com  → 8001（新版 SaaS）
```

两个项目通过 Nginx 子域名区分，互不影响。

---

## 网站配置步骤

### DNS 解析

在腾讯云 DNS 控制台添加 A 记录：

```
主机记录: nesting
记录类型: A
记录值:   服务器 IP
```

### 方式一：宝塔面板

1. 网站 → 添加站点 → 域名填 `nesting.你的域名.com`，根目录 `/opt/stonebot_saas`，PHP 选纯静态
2. 设置 → 反向代理 → 目标 URL: `http://127.0.0.1:8001`

> ❗ 待解决：宝塔面板没找到「添加站点」按钮

### 方式二：命令行（备用）

```bash
mkdir -p /opt/stonebot_saas

cat > /etc/nginx/conf.d/nesting.conf << 'EOF'
server {
    listen 80;
    server_name nesting.你的域名.com;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

nginx -t && nginx -s reload
```

---

## 当前阻塞

- 宝塔面板未找到添加站点入口（待用户确认版本或改用命令行）
- 域名待确认，DNS 待解析
- SaaS 代码尚未开始编写

---

## 待办

1. 确认域名，添加 DNS 解析
2. 配置 Nginx 反向代理（宝塔或命令行）
3. 本地搭建 FastAPI 项目框架
4. 实现 DXF 上传 → 排板 → 下载核心流程
5. 部署到服务器 /opt/stonebot_saas/
6. 添加用户认证
7. 添加任务队列（处理长时间排板）
8. 添加数据库（项目历史记录）
