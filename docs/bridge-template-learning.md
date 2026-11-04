# 桥切板型学习

## 目标

把用户当次提供的人工排板 DXF 作为参考语料，归纳可复用的桥切结构方法，
用于当前任务。系统学习的是横带、互补配对、核心件与可选填充件、相对姿态
和重复条件，不保存或打包某个项目的完整板型、编号或坐标。

## 入口

```bash
python main.py input.dxf 3000 2000 20 \
  --bridge-template 人工排板结果.dxf \
  --bridge-template-min-fill 0.0 \
  --kerf 5 --sheet-oversize 30,30 \
  --no-confirm
```

当前限制：

- 只支持桥切工艺。
- 只有映射到同一个 `packing_group` 的材料才能共板。`material_group`
  保留原始材料号，用于标注、统计和回溯。
- 暂不支持 `--special-size`。
- 参考板与当前大板尺寸必须一致。
- 人工参考 DXF 只作为运行时输入，不作为仓库夹具、默认画像或发布包内容。

## 使用步骤

1. `learn_template_boards()` 识别参考 DXF 中尺寸等于大板尺寸的闭合矩形。
2. 用零件质心把板内轮廓分配给板框，形成 `TemplateBoard`。
3. `detect_template_structures()` 从零件组合中归纳通用结构：
   - `complementary_pair`：两件不重叠轮廓组合后接近完整矩形；
   - `horizontal_band` / `vertical_band`：至少三件同深度、同起点或同宽列；
   - `preserve_relative_pose`：匹配时必须保持模板中的相对姿态。
4. `infer_optional_fillers()` 把结构之外的小件标为可选填充槽。
5. `reuse_template_boards()` 先尝试完整板型；库存不足时，最多移除两个
   可选填充槽，核心结构仍须完整匹配。
6. 模板零件只有在以下条件全部满足时才接受：
   - 实际几何和模板姿态匹配；
   - 当前零件级 `allowed_rotations` 允许该姿态；
   - 所有零件位于板内；
   - 零件间没有面积重叠。
7. 重放成功的结构直接成为热启动板；其余零件继续进入束搜索、LNS 和回看。

整板匹配失败时只允许移除预先识别的可选填充槽；核心配对或横带缺件时整张
模板拒绝，不会悄悄改变零件方向。

## 板型匹配

模板不依赖原始编号。形状族由净面积、长宽和轮廓填充率分桶得到，再通过
bbox 归一化后的 Hausdorff 距离验证实际姿态。这样 CAD 中 0.1～0.2mm 的导出
误差不会破坏匹配，但镜像 L 型或换向 90° 的零件不会被误认。

## 人工板型之外仍保留的搜索

热启动只负责消化能直接复用的高价值板型，不能保证全局最优。剩余零件仍由：

- 多套贪心配置；
- 逐板束搜索；
- LNS 跨板重排；
- `k -> k-1` 压缩；
- 固定朝向局部回看；

共同竞争。最终结果按完整件数、板数、最弱板面积和制造评分比较。

## 输出与证据

模板学习统计进入 optimizer stats：

- `structure_counts`：参考图中识别到的结构类型；
- `structure_support`：按形状族、结构类型和相对关系统计的跨板支持次数；
- `boards_with_structures`：带可解释结构的板数；
- `optional_filler_count`：可选填充槽数量；
- `template_optional_variants_used`：使用部分模板的次数；
- `template_optional_slots_skipped`：实际省略的填充件数；
- `template_reused_structure_counts`：最终热启动实际使用的结构类型。

这些字段只描述方法使用情况，不携带参考图坐标或零件编号。

## 测试与项目数据边界

- 单元测试使用合成矩形、L 型和小填充件，验证配对、横带、缺填充回退、
  朝向白名单和整板平移。
- 不把任何客户人工排板 DXF 复制到 `tests/fixtures/` 或插件发布包。
- 项目结果只能作为一次性离线效果验证，不能成为持续回归的前提。
