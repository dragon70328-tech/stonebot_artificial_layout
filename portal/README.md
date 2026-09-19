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


## 排板参数

识图排板页在发送首个排板任务前显示：

- 加工方法：桥切、水刀或激光；
- 大板标称尺寸和可利用尺寸；
- 大板厚度；
- 让尺是否利用、让尺长度和宽度、让尺口径；
- 桥切刀厚或有效刀缝宽度；
- 根据让尺实时计算的最终可用尺寸。

这些参数作为结构化 `parameters` 随会话消息保存，并直接合并到后端建单请求，
不依赖 LLM 猜测。桥切未输入有效刀缝时，前端和后端都会拒绝建单。


## 前端测试

- 纯 UI 状态逻辑：`node tests/frontend_ui.test.cjs`
- 静态资源与操作按钮契约：`python -X utf8 -m pytest -q tests/test_http_api.py::test_root_serves_portal_ui_module tests/test_http_api.py::test_nesting_frontend_has_action_contract`
## 原位预览与点选

任务生成可用的原位 DXF 后，识图排板页会显示在线几何预览：

- 蓝色轮廓为已识别规格板，橙色为孔洞，红色虚线为未识别封闭实体；
- 单击图形可选中或取消对应纠错目标，并与下方多选列表双向同步；
- 面板编号显示在轮廓中心，支持滚轮缩放和“适应”全图；
- 可直接下载当前纠错版本的原位编号 DXF。

预览数据由 `GET /jobs/{job_id}/drawing-preview` 提供，实体 ID 与
`POST /jobs/{job_id}/corrections` 接受的 `target_id` 完全一致。
