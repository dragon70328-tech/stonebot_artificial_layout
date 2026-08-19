# 读图子系统设计（当前第一里程碑）

> **范围调整（2026-08-19）**：当前阶段只接受 **DXF** 一种输入格式。
> DWG 转换链路（accoreconsole/LibreDWG/ODA）经尝试后放弃，PDF 与手绘草图
> 目标同时挂起；用户须自行从 CAD 导出 DXF 后上传。文中 DWG/PDF/手绘相关
> 内容保留作为后续恢复的设计参考，当前不作为开发依据；相关探索代码归档于
> `_archive/legacy/intake_dwg_pdf/`。

## 目标

把当前单一的 `read_dxf()` 升级为可审计、可修正、可确认的读图子系统。输入支持
DXF（其他格式均已放弃，见文首范围调整）；输出包括标准加工规划数据、审图报告、
问题高亮图以及用户确认后的原位 DXF。

读图子系统是 `docs/saas-architecture-goal.md` 的第一个里程碑，先于排板、后处理和
SaaS 多租户能力建设。

## 输入优先级

| 优先级 | 格式 | 策略 |
|---|---:|---|
| 1 | DXF | ezdxf 原生解析，几何可信度最高 |
| 2 | DWG | （放弃）不再做系统内转换，用户自行导出 DXF |
| 3 | PDF | （放弃）可用时优先走 `PDFIMPORT` / `-PDFIMPORT` 转 CAD；否则提取矢量路径/文字，扫描页先转图像 |
| 4 | 手绘草图 | （放弃）视觉理解 + 矢量重建，必须人工确认尺寸与比例 |

原则：AI 视觉只负责语义判断和候选标注，几何坐标必须由解析、矢量化和约束求解回填，
不能由模型直接生成。

## 主流程

1. 输入归一化：识别格式，生成标准化中间文件，保留源文件和转换日志。
2. 读图解析：提取图层、线型、块、标注、实体、单位、坐标、比例和图纸版本。
3. 语义识别：识别规格件、孔洞、编号、材料、纹理方向、加工特征和数量关系。
4. 审图：发现封闭性、编号、材料、尺寸、重叠、单位、无效实体等问题。
5. 问题反馈：输出结构化审图报告、高亮 DXF 和问题视觉证据。
6. 用户修正：用户手动改 CAD 后重传，或系统按结构化规则生成修正候选。
7. 复检：修正后重新审图，按问题状态确认是否闭环。
8. 提取加工数据：输出标准几何、编号、孔洞、材料、数量、方向约束等数据契约。
9. 深化图纸：生成带标注、孔位表、图层标准的原位 DXF。
10. 用户确认：确认原位 DXF 后才进入排板、后处理和输出。

## 数据契约

读图阶段需要新增以下契约对象，并与现有 `Part`、`Sheet`、`NestingResult` 解耦：

- `DrawingSource`：源格式、文件哈希、转换器版本、原文件路径、图纸版本。
- `DrawingRevision`：修订号、父修订号、变更摘要、用户或 AI 修正来源。
- `RecognizedDrawing`：识别出的面板/孔洞/文字/标注/材料/方向及每条识别的置信度。
- `DrawingIssue`：问题类型、严重度、实体句柄、图层、坐标、证据图、建议、状态。
- `CorrectionEvent`：修正动作、作用实体、修正前摘要、修正后摘要、触发人。
- `ConfirmedDrawing`：用户确认后的图纸快照，作为后续排板唯一输入。

所有契约必须包含 `schema_version`、`revision`、`input_digest` 和 `output_digest`。

## 模块设计

- `IngestionService`：格式识别、DWG/PDF 转换、原图存档、版本管理。
- `GeometryExtractor`：封闭图形、孔洞、圆弧、样条、块、标注、单位和坐标偏移。
- `SemanticRecognizer`：编号归属、材料前缀、文字-图形关联、加工特征识别。
- `AuditEngine`：复用并扩展 `drawing_profile.audit_drawing()`。
- `VisualEvidenceService`：问题定位、裁剪截图、渲染标注。
- `CorrectionService`：结构化修正、原图变更 diff、复检。
- `DrawingDataExtractor`：输出排板与加工规划所需的标准数据契约。

## PDFIMPORT 适配策略（已放弃，保留备查）

`PDFIMPORT` / `-PDFIMPORT` 可以用于把 PDF 转成 CAD 实体，但应作为可选转换适配器，
不作为 SaaS 唯一依赖。

- 最适合从 CAD 导出的矢量 PDF，能保留线、圆、弧、多段线和部分文字。
- 扫描 PDF 和手绘草图不适合，仍走视觉理解 + 矢量化流程。
- 比例尺通过用户指定参考长度或已知尺寸来校准，转换结果必须记录
  `scale_mode`、`reference_length`、`rotation` 和转换器版本。
- 服务端自动化需要 AutoCAD/兼容软件的授权和可脚本化入口；是否可作为 SaaS 后端
  使用必须以 Autodesk/第三方授权条款为准。
- 默认开发链路建议抽象 `PdfToCadAdapter` 接口，支持：
  - `AutoCadCoreConsoleAdapter`：有授权环境，用 `accoreconsole` 执行脚本。
  - `PyMuPdfReconstructionAdapter`：无 AutoCAD 环境，用路径提取 + ezdxf 重建。
  - `VisionVectorizationAdapter`：扫描 PDF 或手绘草图。
- 无论使用哪种适配器，输出统一为 DXF，再进入现有 `dxf_reader` / `audit_drawing` 链路。
- 转换器命令通过 `DWG_TO_DXF_COMMAND`、`PDF_TO_DXF_COMMAND` 环境变量配置，
  样例见 `converter.env.example`，部署说明见 `docs/converter-adapters.md`。

## PDF 比例尺与尺寸标注规则（已放弃，保留备查）

PDF 转 DXF 时不能由系统自行假设比例尺，必须依据 PDF 图上已经存在的信息：

1. 优先识别图框或标题栏中的文字比例，例如 `SCALE 1:50`、`比例 1:100`。
2. 其次识别图形比例尺条：提取比例尺线段和端点标注值。
3. 如果只有尺寸标注，则从尺寸标注中恢复比例：
   - OCR/文字提取识别尺寸值，例如 `1500`、`3200`。
   - 匹配尺寸值附近的标注线、延长线或两点间 PDF 几何距离。
   - 使用多条尺寸标注投票，取一致的中位数比例。
4. 如果既没有比例尺，也没有可用尺寸标注，则标记 `scale_unknown`，
   暂停流程并要求用户提供已知参考长度，不能直接按 1:1 输出。

PDF 比例信息应记录为 `scale_source`，可选值：

- `text_scale`
- `scale_bar`
- `dimension_annotation`
- `user_defined`
- `unknown`

对应置信度写入 `scale_confidence`，低置信度必须进入审图问题并提示用户确认。

## 审图问题模型

当前 `audit_drawing()` 已有：未闭合、重复几何、无效几何、缺编号、无面板编号、
面板内编号冲突、可疑小面积、非面板文字、排除实体和排除线型。

建议补充以下问题类型：

- `scale_or_unit_mismatch`
- `dimension_geometry_mismatch`
- `self_intersecting_geometry`
- `open_chain`
- `duplicate_label`
- `material_conflict`
- `number_outside_panel`
- `hole_outside_panel`
- `unsupported_entity`
- `low_confidence_entity`

每个问题必须能回链到原图实体和坐标，并支持状态：

`new -> accepted -> fixed -> verified`

以及非闭环状态：

`ignored / needs_manual_fix`

## 人工修正与复检

- 当前第一版修正重传只接受 DXF；DWG/PDF 若允许上传，也必须先由输入适配器
  转换为 DXF，再做版本 diff。
- 用户在原 CAD 中修改后，建议直接导出 DXF 再上传，以保证实体句柄、坐标和图层
  可用于稳定比较。
- 用户可以在 CAD 中修改后重新上传，系统按稳定实体 ID 生成 diff。
- 用户可以接受、忽略或标记问题，系统保留所有审图历史。
- AI 修正只能生成结构化候选，不直接改写源 DXF；用户确认后由工具执行。
- 修正完成后必须重新运行审图，只有问题状态全部闭环才能进入深化阶段。

## 当前代码对应关系

| 能力 | 当前实现 | 差距 |
|---|---|---|
| DXF 几何提取 | `src/dxf_reader.py` | 只覆盖部分实体，缺少 DWG/PDF/草图 |
| 层级与孔洞 | `_build_part_hierarchy()` | 可用，但需增加置信度和证据 |
| 多图层编号 | `_collect_number_texts()` | 主流程可用，画像级 bbox 归属未接入 |
| 图纸画像 | `src/drawing_profile.py` | 画像匹配和审图已有，但未完全接入 `main.run()` |
| 审图报告 | `audit_drawing()` / `write_audit_json()` | 尚无 CLI 入口和问题状态管理 |
| 问题高亮 | `write_audit_dxf()` | 已实现，缺视觉截图和交互反馈 |
| 原位输出 | `write_numbered_parts_dxf()` | 当前主流程写 `numbered_check.dxf`，需统一命名 |

## 实施顺序

1. 先把原生 DXF 读图做强：接入 `audit_drawing()`，增加 CLI 审图入口。
2. 增加问题状态、视觉证据和修正复检闭环。
3. 把画像级 HATCH/LINE 提取和 `point_then_bbox` 归属接入主读图链路。
4. （放弃）实现 DWG 转 DXF 的输入归一化。
5. （放弃）实现 PDF 矢量提取和扫描页 OCR/矢量化。
6. （放弃）最后实现手绘草图辅助导入，默认强制人工确认。

当前进度：步骤 1、2 已完成（`main.py --audit` + `review_state.json` 复检闭环）。
步骤 3 为当前主攻方向；步骤 4-6 随 2026-08-19 范围调整放弃。

## 验收标准

- 同一文件版本多次读图得到同一份结构化结果和审图报告。
- 每个审图问题都有原图坐标、实体句柄、视觉证据和修复建议。
- 编号、材料和面板归属都带置信度，低置信度必须提示用户确认。
- 用户重传修正图纸后，系统能识别新增、删除和保留实体，并复检问题状态。
- 用户确认后生成的原位 DXF 包含面板、孔洞、编号、材料和问题标记，坐标不变。
- 不支持的格式或超限文件给出明确错误，不进入排板流程。
