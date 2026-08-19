# DXF 视觉理解中间层评估

> 状态：方案评估。对应 `docs/ai-workflow-improvement-plan.md` 第二阶段
> “视觉辅助描述”，但不替代确定性 DXF 解析。

## 结论

当前不建议把多模态视觉模型放进读图关键路径。应先把确定性读图、审图和视觉证据做
闭环，再把视觉理解作为可选增强层，只处理低置信度实体和语义歧义，且模型只能输出
结构化候选，几何坐标仍由 DXF 解析回填。

## 约束

- 当前输入只有 DXF，没有 PDF/手绘草图恢复几何的压力。
- AI/Agent 只做规划、语义理解和结构化意图，不直接写文件或执行生产代码。
- 排板、后处理和 DXF 写入必须保持确定性、可复现、可审计。
- 视觉模型的输出不是几何源，不能直接生成坐标、多段线或孔洞。

## 中间层目标

在确定性解析结果和用户确认之间增加一个可选的视觉语义层：

- 把 DXF 渲染为带图层、颜色和编号标记的检查图。
- 让视觉模型判断面板轮廓、孔洞位置、编号归属、辅助图形和标注关系。
- 将视觉判断与 `audit_drawing()`、`read_dxf_with_profile()` 的结构化结果交叉验证。
- 差异进入审图问题，而不是静默改写几何。

## 建议契约

### VisualObservation

```text
observation_id
source_image_artifact_id
source_drawing_revision
entity_handle
entity_type
observed_semantic
confidence
conflicts_with
evidence_region
review_status
```

关键规则：

- `entity_handle` 必须回链 DXF 实体，无法回链时只能输出为 `unlinked_candidate`。
- `observed_semantic` 只使用枚举值，例如 `panel_boundary`、`hole`、`number_label`。
- `evidence_region` 只用于渲染高亮，不作为最终几何坐标。
- `confidence` 低或 `conflicts_with` 非空时必须进入审图问题并等待人工确认。

## 与现有代码的集成点

- 渲染层：新增 `src/visual_renderer.py`，输出 SVG 或 PNG，复用当前
  `src/visual_evidence.py` 的问题高亮思路。
- 语义候选：视觉结果进入 `src/contracts.py` 的 `RecognizedDrawing` 或独立
  `VisualObservation` 契约。
- 冲突合并：在 `drawing_profile.audit_drawing()` 后增加 `VisualVerifier`，
  只允许创建新问题或提升置信度，不允许覆盖确定性几何。
- 主链路：视觉层默认关闭，仅在用户启用、存在低置信度实体或人工请求复核时运行。

## 评估维度

- 准确率：与人工标注/现有确定性结果比较，重点看编号归属和孔洞归属。
- 成本：只对局部裁剪图调用，避免整张大幅面图纸直接送模型。
- 延迟：异步任务，不阻塞 DXF 解析和排板确认。
- 可审计性：每次视觉调用记录模型、输入图像、提示词、输出契约和合并结果。
- 回退：模型不可用或超时时，系统仍能按确定性流程完成读图和审图。

## 推荐实施顺序

1. 先完成确定性闭环：继续强化 `audit_drawing()`、置信度和 `review_state.json`。
2. 实现 `src/visual_renderer.py`，为审图问题生成稳定、可复现的 SVG/PNG 检查图。
3. 定义 `VisualObservation` 契约和 `VisualVerifier` 合并规则。
4. 在 `_analysis` 中选一张真实 DXF 做小样本试点，仅对低置信度问题调用视觉模型。
5. 根据试点召回率/误报率决定是否纳入默认流程。

## 边界

- 模型输出只能成为审图问题或人工确认候选，不能直接修改 `Part` 几何。
- 模型不可用时，系统不进入阻塞状态，只记录 `visual_unavailable`。
- 视觉结果与确定性结果冲突时，默认以确定性几何为准，标记冲突供人工确认。
