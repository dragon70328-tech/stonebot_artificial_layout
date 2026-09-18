# Portal 轻门户

统一入口首页，只负责模块选择和路由，不承载业务逻辑。

## 使用

直接托管 `portal/` 静态文件即可：

```bash
cd portal
python -m http.server 8080
```

生产环境由 Nginx/Caddy 托管。

识图排板的极简聊天原型位于 `portal/nesting/index.html`。直接运行 FastAPI
时，`src/http_api.py` 会把该目录挂载到根路径，可打开 `/` 使用。

## 模块配置

编辑 `modules.json`：

```json
[
  {
    "id": "nesting",
    "name": "识图排板",
    "description": "DXF 读图、审图、排板和排板后回看",
    "path": "/nesting/",
    "enabled": true
  }
]
```

新增模块时只需增加一个对象，不需要修改首页 HTML 和 JS。


## 前端测试

- 纯 UI 状态逻辑：`node tests/frontend_ui.test.cjs`
- 静态资源与操作按钮契约：`python -X utf8 -m pytest -q tests/test_http_api.py::test_root_serves_portal_ui_module tests/test_http_api.py::test_nesting_frontend_has_action_contract`