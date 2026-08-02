# Agent Instructions - 人造石台面 DXF 排板系统

## Role
人造石台面排版系统，读取 CAD 导出的 DXF 文件，识别实线封闭图形作为规格台面板，
自动匹配原始编号，在指定尺寸大板内进行二维不规则排样，输出排板 DXF 和出材率报告。

## Project Structure
```
stonebot_artificial_layout/
  agent.md              # 本文件
  main.py               # CLI 入口（命令行排板）
  run_app.py            # Web 入口
  requirements.txt      # Python 依赖
  src/
    dxf_reader.py       # DXF 读取：提取封闭图形、构建层级、匹配编号
    dxf_writer.py       # DXF 写入：排板结果、编号检查文件
    models.py           # 数据模型：Part / Sheet / NestingResult
    nesting.py          # 排板引擎：多轮贪心 BFD + LNS 优化
    numbering.py        # 编号管理：保留原编号 / 自动生成
    units.py            # 单位制切换
  app/
    server.py           # Flask Web 服务
    workflow.py         # 对话工作流引擎
    templates/          # 前端模板
  output/               # 输出目录
  tests/                # 测试
```

## 工作步骤

### Step 1: 读取 DXF，筛选实线封闭图形

1. 遍历 modelspace 所有实体，按 dxftype() 分类
2. 线型过滤：只保留 linetype 为 CONTINUOUS 或 BYLAYER（所在层为实线）的实体
   排除 ZIGZAG（大样图标记）、DASHED（虚线）等非规格板图形
3. 封闭判断：LWPolyline.closed=True 或首尾点距离 < 0.01
4. 实体转 Polygon：LWPolyline -> 顶点坐标；Circle -> 64段近似；Spline -> flattening(0.1)

### Step 2: 构建面板层级（外轮廓 vs 孔洞）

核心：质心包容检测 + 交集面积比验证（不使用 shapely.contains，容错性差）

1. 所有实线封闭图形按面积降序排列
2. LWPOLYLINE 逐一处理：
   - 质心在已有外轮廓内 AND 交集面积 > 自身面积 * 90% -> 归类为孔洞
   - 否则 -> 作为新外轮廓
3. CIRCLE 永远是孔洞，分配给包含它的外轮廓（同上双条件）
4. 结果：203 外轮廓 + 306 孔洞（CGR45），100 外轮廓（countertop1 无孔洞）

### Step 3: 匹配原始编号

1. 从"编号"层收集短文本（<15 字符）作为候选池
2. 每个外轮廓取最近未用编号（距离 < 5000 单位）
3. 贪心 1:1 匹配保证唯一性
4. 无匹配的自动编号 P-XXXX

### Step 4: 生成编号检查 DXF（原位）

输出文件：`output/{name}_numbered_原位.dxf`

- 单位制与原文件一致
- 面板外轮廓 + 孔洞（独立图层）+ 编号 TEXT（质心居中）
- 坐标保持原位，不做平移旋转
- 用途：用户检查面板识别和编号准确性

### Step 5: 排板

算法：多轮贪心 BFD + 大邻域搜索（LNS）

1. 创建 Part 对象：outer_polygon 用于碰撞，holes 跟随变换
2. 多轮配置：sort（short/area/long/jitter）× mode（skyline/column/contact）× seed
3. improve_budget：120~180 秒大邻域搜索
4. 每轮按排列键贪心放置，支持 0°/90°/180°/270° 旋转
5. 取最优（先比板数，再比紧凑度）
6. 精确校验：边界 + DE-9IM 重叠检测

### Step 6: 生成排板 DXF

输出文件：`output/{name}_nested_{W}x{H}.dxf`

图层结构：
- SHEET_BORDER：大板外框（红色）+ Sheet 编号
- OUTER：台面板外轮廓（黑色）
- HOLES：盆孔/水龙头孔（蓝色），不参与排板但跟随面板
- NUMBERS：板件编号（居中）

每个面板 + 孔洞 + 编号组成 GROUP（`doc.groups.new(name).extend(entities)`），选中即全选。

## 关键经验

### 线型过滤是第一步
ZIGZAG 线型的大样图标记必须排除，否则会被误识别为规格板。
实线 = CONTINUOUS 或 BYLAYER（且所在层线型为实线）。

### 孔洞检测不能用 shapely.contains()
`contains()` 对精度敏感，改用 centroid 包容 + 交集面积比（>90%）更可靠。

### 编号匹配需考虑坐标偏移
原图坐标可能很大（几十万单位），search_radius 默认 50 不够。按实际情况调整（如 5000）。

### CIRCLE 实体单独处理
CIRCLE 永远是孔洞（水龙头/盆孔），不能作为外轮廓，但必须包含在层级检测中。

### GROUP 组织面板
排板 DXF 中每个面板外框+孔洞+编号用 GROUP 绑定，方便后续 CNC 操作时选中。

### 出材率解读
出材率 = 面板净面积 / 大板总面积。面板净面积已扣除孔洞，所以出材率会比看起来低一些。
实际材料消耗看大板张数，出材率主要用于不同方案间比较。

## 依赖
- Python 3.10+
- ezdxf（DXF 读写）
- shapely（几何计算）
- numpy
- Flask（Web 界面可选）
