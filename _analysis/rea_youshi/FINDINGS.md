# 有石CAD（GstoneCAD）排板逻辑与算法 — REA 分析台账

分析日期：2026-10-14　目标：`C:\Program Files (x86)\有石CAD`
范围：**只研究排板逻辑与算法**，不涉及破解 / 注册 / 绕过授权。

---

## 1. 目标身份与产物清单

| 文件 | 大小 | SHA-256(前12) | 形态 | 备注 |
|---|---|---|---|---|
| GSTONE.dll | 1 985 024 | 9272F5691F42 | 托管 x86_64 | **AutoCAD 2023(64 位)实际加载的插件**；526 类型 / 4724 方法 |
| GSTONE 2008.dll | 1 992 192 | E998AF71EC76 | 托管 x86 | 老版 CAD 适配；517 / 4725 |
| GSTONEv8.dll | 2 039 296 | 089691CF5468 | 托管 x86 | 431 / 3971；**方法体未被剥离，是本次唯一的可读实现** |
| Gstonedcl.dll | 11 117 056 | 6AD13E4B6BCB | 托管 x86 | 主程序（WinForms UI + 业务）；1452 / 7134；242 内嵌资源 |
| GsTools.dll | 9 367 040 | 0E9DE595E80D | 托管 x86 | 加载器（SmartAssembly 保护）；35 / 139 |
| IsRegist.dll | 1 178 112 | 4AFCF2A48033 | 托管 x86 | 授权/注册；439 / 2077 |
| Newtonsoft.Json / DevComponents.* | — | — | 托管 | 第三方依赖 |
| delftCadLinelist.json | 13 801 | 90DC2F625233 | 数据 | 线型清单（DelftCAD 体系线索） |

字节级副本保存在 `targets/`（REA 记录里的 SHA-256 与原始文件一致）。
原始安装目录全部 DLL 带 Hidden 属性（`Get-ChildItem` 必须加 `-Force`）。

### REA 证据（evidence_id）

| 记录 | evidence_id |
|---|---|
| inspect_managed_artifact · GSTONE.dll (x64) | `ev_11b633df9df2931862cf071fbe81b2a3acb97941ec8377fcdfc3d4c4abc04254` |
| inspect_managed_artifact · GSTONE 2008.dll | `ev_10a7f9b8be8656b6bc21097737c9a07bc6d759cff847f73fdce028aee8252944` |
| inspect_managed_artifact · GSTONEv8.dll | `ev_4bf87a111a209316279b7377856c464b515aec032dac348ffa043fd2fc46ed92` |
| inspect_managed_artifact · Gstonedcl.dll | `ev_efc44722d308741e93b04ea66f3f410ffc7c5df89adc268dc20de93fdbd21d73` |
| inspect_managed_artifact · GsTools.dll | `ev_9b16b3b4d41130d1772fd7a555cf2655c2fd1677b337296d49e9f162791f8ba5` |
| inspect_managed_members · GsTools.dll（唯一可用） | `ev_0585d19b327df075a1dcf1ad0788438ec68f1ee006c5e69f28b0da6c272f2f87` |
| inspect_managed_artifact · IsRegist.dll | `ev_cf6ad18a4b8aaf7834f9bca1edfa66bf171890f97073de348224e5481b83f174` |
| inspect_managed_artifact · DevComponents.DotNetBar2.dll | `ev_a6f2c209ac3e580936542fe588db29b09f386bf4616d87de2b110473fb0344b0` |
| inspect_managed_artifact · DotNetBar.SuperGrid.dll | `ev_8dcd9721615d160d2b97fc3dc5ee1388916a21fb8457ca75a4a7c4b2576898b7` |
| inspect_managed_artifact · Newtonsoft.Json.dll | `ev_7ba46b829020824a554da51e8f25ace85d788b70b3141f20d8eff15cdbab27d4` |

其它原始证据 JSON 落在同目录：`ident_*.json` / `members_*.json`（`dump_evidence.ps1` 生成）。

---

## 2. 工具链与限制（重要）

- REA MCP 未注册、`rea` CLI 未安装 → 按技能指引用 `npx -y rea-agents@6.4.0` 走命令行。
  本机 npm 源是 npmmirror，每次调用都需联网（已获批准的前缀规则）。
- **REA 在本目标上的硬限制**：`inspect_managed_members` 对 9 个程序集里的 8 个返回
  `resource-limit: Managed metadata exceeds the 67108864 byte representation budget`
  （类型/方法清单为空）。只有 GsTools.dll 出得来。所以**成员级 REA 证据只覆盖加载器**，
  排板引擎的证据来自 REA 的 artifact 层（身份/规模/凭据）+ 本地反编译。
- `rea decompile` 只接受原生 provider（ghidra/hopper/ida），托管 C# 按 REA 设计属
  "自带反编译产物再回灌校验"（`import-managed-reconstruction`）。
- 本地反编译器：ILSpy 8.2（`dotnet tool install --tool-path _analysis/rea_youshi/tools ilspycmd --version 8.2.0.7535`，**只装在工作区**，未动全局工具链）。

### 混淆/保护形态（观测）

| 模块 | 名称 | 方法体 | 字符串 | 备注 |
|---|---|---|---|---|
| GSTONEv8.dll | SheetNesting 公开 API 可读；辅助命名空间随机 | 完好 | 加密（`.UwJtiVn6kqr(key)` 解密器） | 本次可读实现 |
| GSTONE.dll / GSTONE 2008.dll | 同上 | **全部无效 RVA**（2956/2976 处） | 加密 | 节表被清零、RVA 空间重排 |
| Gstonedcl.dll | 多数类型名可读（`*dcl` 结尾） | 完好 | 加密 + 资源里无中文 | 控制流平坦化（`switch(num)` 状态机） |
| GsTools.dll | 全随机名（`Q0e4TUgBIHdqkMRX1x.*`） | 完好 | 加密 | 带 `SmartAssembly` 标记 |

宿主节表自相矛盾（GSTONE.dll）：`.text`/`.JQG` 的 `raw_ptr/raw_size` 被清零，
另加随机名节 `._wu`（`raw_ptr=1024`、`raw_size` 覆盖几乎整个文件）。唯一能自洽的映射是
`file_off = rva - 0x1D8000 + 1024`（CLI 头 `metadata_rva=0x32C0BC` → 文件 1393852 处命中 `BSJB`，
ILSpy 正是靠这条映射才读到了类型/方法清单）。但方法体 RVA 既**不满足 CLR 的 4 字节对齐要求**
（如 `0xC6A5A` ≡ 2 mod 4），也落不进任何真实映射区间 → 观测结论：**这些 RVA 由运行时修复，
静态不可还原**（不是我们没试对偏移）。这也是选 GSTONEv8.dll 做算法阅读的原因。

判定：`SheetNesting` 类型集在 x64 / 2008 / v8 三个构建间**完全一致**（23 个共同类型，
x64 仅多 `NestLog`），可认为同一套引擎的不同 CAD 版本适配。

---

## 3. 排板子系统结构（观测）

命名空间 `SheetNesting`（在插件 DLL 内，24 个类型）：

| 文件 | 行数/大小 | 作用 |
|---|---|---|
| `Nester.cs` | 12 909 行 | 引擎主体（几何、候选、判定、压实、回填、图案学习） |
| `NestingPlanner.cs` | 7.4 KB | 运行编排：矩形快速通道 `NestRectFast`、8 套"配方"枚举、预览渲染、Live 渲染 |
| `ContinuousPlanner.cs` | 2.4 KB | 后台线程连续出方案，`SchemeDone/Finished` 事件 + `Best` + 分阶段 Top3 |
| `IterativeNesting.cs` | 0.9 KB | 人机迭代：每轮问用户是否继续，保留 bestUtil/bestRes |
| `Geo.cs` / `Polygon2d.cs` | 6.1 / 1.9 KB | 2D 几何工具箱（含 `MaxInscribedRect`、`Fingerprint`、`ShapeKey`、`TryGetSectorInfo`） |
| `ClipperLib/*` | 95 KB | 第三方 Clipper（多边形布尔/偏移） |
| `NestingForm.cs` / `ClusterDetectForm.cs` / `NestingUICommands.cs` | — | 交互界面 |

命令入口：`SheetNestingCommands.DoNesting`（选板 + 选件 → 排板 → 写回图纸）、
`SheetNestingCommands.DumpLayout`（把板/件多边形导出成文本，写到 temp + AppData + 桌面）、
`IterativeCommands.DoIterativeNesting`（迭代版，默认间距提示 3.0mm）。

### 数据模型

```text
SheetInfo  { Poly, InnerPoly(按边距内缩后的板), Placed, OccupiedConvex, FreeCells(+FreeCellsR), Holes }
PartInfo   { Poly, MinW/MinH, FastPoly(简化多边形), IsCircle/Radius,
             Members/MemberPolys/MemberPreOff/CombCenter/CombArea,  ← 组合件
             LabelIds }
PartResult { Part, Sheet, Place }
PlaceResult{ Poly, Pos, AngleDeg, FitLength, PartFit, SameFit, EdgeFit, AngleSameFit }
NestingScheme { Index, Utilization, Compactness, PlacedCount/TotalParts,
                UsedSheets/TotalSheets, TimeSec, Note, Stats, NfpStats, Results, Preview }
NestingOptions { Gap, EdgeGap, AllowRotation, UtilizationFirst, Quality, Seed }
```

`LabelIds` 说明编号文本随零件一起变换；`Members/CombArea` 说明"组合件"是一等公民。

---

## 4. 算法（观测 → 推断）

**流程**：`ExtractSheets/ExtractParts` → `BuildLearnedComposites` → 每轮
（模式配方 + 角/候选预算）→ NFP 候选 → 判定 → 放置 → 压实 → 出 `NestingScheme`
→ 比较保留最优 → `ApplyResults` 写回图纸。

### 4.1 预处理：组合件来自"原图相邻关系"

`DetectTightClusters(parts, gap)`：并查集合并**彼此距离 ≤ max(gap, 1.0)** 的零件
（`Geo.PolyDistance`），每组成为 `LearnedPattern`；`BuildLearnedComposites(parts, gap)`
在 `Nest()` 内**无条件调用**，把命中图案的零件合成一个 `PartInfo`（`Members/MemberPolys/
CombCenter/CombArea`）整体摆放。默认 `EnableLearning=false`（图案库为空时无效果）。

推断：这是"沿用图纸里原本挨着的件一起走"的机制（配套件不拆散）。`ClusterDetectForm`
提供 3 选项（结果码 0/1/2）供人挑选/删除图案。

### 4.2 几何与碰撞

- 弧用 bulge 折线化：`ArcDiscSag=0.6`、`ArcSagTol=max(0.5, gap*1.2)`；`BuildFastPoly`
  （`FastPolyMaxPts=100`、`FastPolyEps=0.2`）做粗筛。
- 判定分级：AABB 预筛 → 顶点严格内嵌（`AnyVertexStrictInside`）→ Clipper 布尔重叠
  （`ClipperOverlaps`）→ 弧形入侵（`PurgeArcInvade`）。计数器 `RjAabbProt/RjVertexIn/
  RjClipOverlap/RjArcInvade` 记录各级拒绝量。
- **NFP 是核心**：`USE_NFP=true`（`static readonly`），NFP 缓存
  `Dictionary<string, List<List<IntPoint>>>`，`NfpCacheKeepAcrossRuns` 默认 false（每轮清空），
  `NfpStatsText` 对外汇报。→ 与 SVGNest/DeepNest 同族的"无碰撞多边形"路线。
- `Geo.MaxInscribedRect(...)`：在空区里找**最大内接矩形**（空区利用）。
- `Geo.Fingerprint/ShapeKey`：形状指纹（同形判定/去重）。

### 4.3 候选与评分（近视 + 多目标拟合）

- `NearCount = 20`：只在最近 20 个已放件周围找候选位置；
- `PerPartCheckBudget = 2000`（`NestingPlanner` 运行时抬到 4000）：每件候选检查上限；
- 评分字段 `PartFit / SameFit / EdgeFit / AngleSameFit / FitLength`：贴合度、同形贴合、
  板边贴合、同角度贴合；`LegacyJudge` 在新旧判定间切换（默认新判定）。

### 4.4 旋转与工艺模式

- `AngleLimit`：默认 4 个候选角；利用率优先 10；`DeepMode` 24；快速预览 3。
  `AllowRotation` 默认 true（`NestingOptions.AllowRotation`）。
- 弧/扇形专用：`AllowArcAlign`、`SectorAlignMode`、`AllSectorMode`、`TryGetSectorInfo`
  （把"圆弧+两条直边"识别成扇形）。
- 水刀模式：`WaterjetMode`、`WaterjetFast`、`WaterjetPartDeadlineMs=3000`（**每件 3 秒预算**）。

### 4.5 放置策略族（同一引擎内可切换）

`BlMode`（左下）、`BlAxisMode`、`GravityMode`（重力下沉）、`SkylineMode` 0..4、
`ShelfCompress`（货架压缩）、`RectVariant` 0/1、`PreferTopRight`（人排法默认右上）、
`SheetStrategy`、`PadLimit`、`NestMode`。

矩形专用快通道 `NestRectFast`：最小包围盒角度（`FindMinAabbAngle`，可选 +90°），
货架式候选（x = 各已放件右边与板边；y = 该 x 处重叠件的上边），取"最低 y，其次最左 x"，
全板结束后整体左下压实；`IsRectPart` / `ShelfPackRectsV2` 配套。

`NestHumanLike`：先把矩形件与异形件分流，矩形按"最大边长降序（差 ≤0.05 时按面积）"排、
`PreferTopRight=true`，模拟人工排版顺序。

### 4.6 压实与后处理

`Slide`（滑动，含命中/有效/耗时细分计时）、`Tetris`（下落）、`Bca`、`PushOpen`（推开）、
`FillGaps`（填缝）、`RefitUnplaced`（未放件重试）、`PurgeOutOfSheet`（越界件回收）、
`CheckOverlapReal`（真重叠复核）、`CalcFreeCells`（空区栅格化 `FreeCells/FreeCellsR`）。

### 4.7 搜索循环（他们的"优化器"）

- `NestingPlanner.yoLCKLo1BD(i % 8)`：**8 套配方**（SkylineMode 4/2/3/1、SkylineMode 0 +
  RectVariant/BlAxisMode 组合），配合 `ofBClH7rdV` 变体计数轮换。
- `DeepNest`：**随机重启的迭代改进**，最多 30 轮，随机扰动"排序键（4 种：最大边长/面积/
  最长边/反向）+ BlMode/BlAxisMode/SheetStrategy + 尾部子序列重排"，保留最优。
- `ContinuousPlanner`：后台线程持续出方案（`MinSchemeIntervalMs`、`MaxSchemes`），
  按 `StagePlacedBest/StageUtilBest/StageScore/StageTop3/BestRectVariant` 打分保留最好。
- `IterativeNesting.RunInteractive`：人机在环，每轮问"继续吗"。
- `SA_ITERATIONS = 0`、`ROUNDS = 1`：**模拟退火在发布版被关掉**（代码位保留）。

### 4.8 目标函数与可复现性

- `Utilization` = 已放件面积 / 已用板面积（×100）；`Compactness`（`CalcCompactness`）；
  `PlacedCount / UsedSheets`。`UtilizationFirst` 决定"利用率优先还是板数优先"（Quality 0..2）。
- 默认种子是**固定值** `NestRandomSeed = 20240901`，但 `RunSalt = Environment.TickCount & 0x7FFFFFF`
  → 实际每次运行都带时间盐；配合后台线程与随机重启，**同输入不保证同输出**。

---

## 5. 与我们系统的对比

| 维度 | 有石CAD（观测） | 我们（现状） | 谁更强 |
|---|---|---|---|
| 碰撞几何 | **NFP + Clipper 布尔**，AABB→顶点→布尔→弧入侵四级判定 | 桥切：空闲矩形减法 + DE-9IM 精确复核（无 NFP）；水刀：BLF + 滑动压实 | 凹形互锁：**他们**；矩形化场景：我们更结构化 |
| 矩形件 | 专用快通道（最小包围盒 + 90° + 货架 + 全局左下压实） | 束搜索尺寸档配方（跨档互锁、缺口回收、尾板耦合） | 我们（配方维度更深） |
| 不规则件旋转 | 4 / 10 / 24 个候选角 + 弧对齐 + 扇形识别 + 水刀模式 | 边对齐（≤8 条长边）+ 4 角；任意角仅水刀 BLF | **他们**（异形/弧边先天更强） |
| 排序 | 组合件 → 面积 → 矩形优先 → 长边 → 长宽比（±0.15 视为同档）→ … 多键 | sort（short/area/long/jitter）× mode × seed 多轮 | 各有千秋（他们都带容差分组，我们多轮枚举） |
| 搜索 | 8 套配方轮换 + 随机重启 ILS ≤30 轮 + 后台持续出方案 + 人工挑选；SA 关闭 | 束搜索（每板全局配方）+ LNS 子集重排（束搜索求解）+ 回看减板 | **我们**（结构化搜索、确定性收益） |
| 学习/复用 | 原图相邻关系 → 组合件；图案库（默认关） | 已验证模板/案例库哈希复用 | 思路相似、触发点不同：他们看"图纸内相邻"，我们看"历史布局" |
| 人工在环 | CAD 内命令 + 实时预览 + 方案卡 + 拖动微调 + 每轮确认 | CLI/SaaS + 三个确认点 + 报告 | **他们**（交互与可控性） |
| 工艺约束 | 水刀模式、弧离散容差、弧入侵；间距/边距（gap/edgeGap） | 刀缝材料校验（贯穿通切计数）、让尺/可利用三层、纹理 profile、180° 共边配对 | **我们**（口径与工艺正确性更严谨） |
| 可复现/审计 | 固定默认种子但时间盐 + 多线程 + 随机重启 → 不可复现 | seed/trials/budget 显式记录、job key、artifact、报告 | **我们** |
| 输出 | 直接改图（`ApplyResults`）+ 文本 dump | DXF + 报告 + 独立校验脚本 | 我们（可离线审计） |

---

## 6. 讨论点（可落地的杠杆）

1. **组合件（原图相邻关系）**：我们目前把每个零件独立排，配套件可能被拆到不同板。
   `DetectTightClusters` 的做法很轻（并查集 + 距离阈值 = max(gap,1)），对 citizen
   这类"同房号多件"的图纸可能天然适用，值得作为 Group 能力评估。
2. **NFP 是差距最大的一处**：我们桥切路径没有 NFP，凹形互锁靠"空闲矩形减法"。
   异形件（弧边、造型边）目前只能走水刀 BLF。是否给异形件补 NFP 值得单列议题。
3. **`_MAX_PIECES_PER_SHEET = 12`**：citizen ST-6 每板件数中位数正好 12，是硬上限。
   他们那侧没有每板件数上限，只有"每件候选预算"（2000/4000）与"最近 20 件"。
4. **多方案卡与人工挑选**：他们的 `ContinuousPlanner` + 缩略图 + Top3 阶段评分，
   对应我们"报告 + 回看"，但缺少"多方案并排给人工挑"。我们有 artifact 基础设施，成本不高。
5. **尾板卡死**是双方共同形态：他们的对策是 `RefitUnplaced` + `PushOpen` + `FillGaps`
   + 随机重启；我们的对策是束搜索尾解 + 回看。可以对照各自命中率再定取舍。
6. **可复现性别学他们**：`RunSalt=TickCount` + 随机重启对 SaaS 是负资产（我们已把
   seed/budget 写进契约，应保持）。

---

## 7. 复现方式

```powershell
# 0) targets/ 与 tools/ 不入库（.gitignore），本地重建：
#    把 "C:\Program Files (x86)\有石CAD" 的 *.dll/*.exe 复制到 _analysis\rea_youshi\targets\
#    （注意这些文件带 Hidden 属性，Get-ChildItem 要加 -Force），ILSpy 用
#    `dotnet tool install --tool-path _analysis\rea_youshi\tools ilspycmd --version 8.2.0.7535`
# 1) 证据导出（需联网解析 npm 包）
powershell -NoProfile -ExecutionPolicy Bypass -File _analysis\rea_youshi\dump_evidence.ps1
.venv\Scripts\python.exe -X utf8 _analysis\rea_youshi\summarize.py
# 2) 反编译（ILSpy 8.2，装在工作区 tools\）
_analysis\rea_youshi\tools\ilspycmd.exe -p -o _analysis\rea_youshi\proj\GSTONEv8 -r _analysis\rea_youshi\targets "_analysis\rea_youshi\targets\GSTONEv8.dll"
# 3) 节表/方法体异常与字符串探查
.venv\Scripts\python.exe -X utf8 _analysis\rea_youshi\probe_pe_mapping.py "_analysis\rea_youshi\targets\GSTONE.dll"
.venv\Scripts\python.exe -X utf8 _analysis\rea_youshi\extract_strings.py "_analysis\rea_youshi\targets\Gstonedcl.dll" _analysis\rea_youshi\gstonedcl.strings.txt
```

未完成/未验证项：UI 文案（字符串解密器 `UwJtiVn6kqr`）未还原，因此 8 套配方/模式的
**中文名称**仍未确认（其行为已由代码路径确认）；GSTONE.dll(x64) 真实方法体未能还原，
其与 v8 版的**行为差异**只能靠类型级一致性与版本日期推断。
