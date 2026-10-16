# 金标准样本（抢救版）

## 为什么在这里

`scripts/check_output_cases.py` 的 6 个回归 case 原本指向
`output/<时间戳>_<项目>/xxx_numbered_原位.dxf`。该目录被 `.gitignore`
的 `output/*_*/` 规则忽略，**不在版本控制内**。

2026-09-23 检查时发现 `output/` 下这些历史运行目录已不存在（只剩
`.cache/` 与 `api_jobs/`），6 个 case 的输入**全部失效**，CI 与本地都无法
再复现任何历史结论。本目录是从仍然存活的副本里抢救出来的样本。

## 当前内容

见 `manifest.json`（含 sha256、字节数、来源路径、关联画像、画像期望计数）。

| 文件 | 来源 | 可支撑的验证 |
|---|---|---|
| `CGR45final.dxf` | `output/api_jobs/.../uploads/inputs/` | 读图/编号/孔洞 + 桥切排板 |
| `countertop2.dxf` | 同上 | 无孔洞台面、层级构建关闭场景 |
| `mercury_numbered_原位.dxf` | `_archive/output_loose/` | 原位编号 DXF 直接进排板 |
| `加拿大3L.dxf` | 用户图纸目录（2026-10-16 回填入库） | 孔洞层级 96/144/96 + 案例库门禁 |
| `福州万象城1.dxf` | 用户图纸目录（2026-10-16 回填入库） | 区域/重复算量图层过滤场景（2026-10-16 回归已修：133/133，孔洞 35 见 `known_deltas`） |
| `加拿大IFC.dxf` | 用户图纸目录（2026-10-16 重新登记入库） | 编号层 767 文本 / 766 面板各匹配 1 个编号 + 案例库门禁 |
| `雪花白洗手台（小利）1.8.dxf` | 用户图纸目录（2026-10-16 经用户逐项核对后入库） | 双线绘制 + 按颜色排除磨边示意线：6 件 / 4 孔 / 6 编号 + 案例库门禁 |

## 仍然缺失（需要重新提供）

`check_output_cases.py` 里下列 case 的输入本地已无副本，需要从服务器
（`/opt/stonebot/`）、原始 CAD 或上游交付目录重新取得：

- `blockd_bridge`
- `outlets1_waterjet`
- `outlets2_waterjet`

## 体积现状（2026-10-16 更新）

原先"保持 untracked、待拍板"的约定已经作废：`CGR45final.dxf`（6.9MB）、
`countertop2.dxf`（2.0MB）、`mercury_numbered_原位.dxf`（1.0MB）**都已在
版本控制内**，2026-10-16 又按同一做法补入 `加拿大3L.dxf`（2.8MB）、
`福州万象城1.dxf`（1.7MB）、`加拿大IFC.dxf`（6.3MB）与
`雪花白洗手台（小利）1.8.dxf`（0.8MB）——四份都是案例库里已有 sha256 的
原始图纸，并且都已接入 `tests/test_validated_cases.py` 与
`benchmark_cases.json`。仍是"源图不进仓库、门禁侧 skip"的只有
`citizen.dxf`（45MB）与水星原始图纸（11.5MB）
读图门禁。当前夹具合计约 14.5MB。

如果夹具继续增长（尤其 citizen 那种 45MB 量级），下面几条仍然可选：

1. **Git LFS** 追踪（需确认仓库/远端是否支持 LFS 配额）；
2. **不做版本控制**，改为 `scripts/fetch_fixtures.py` + 哈希清单，从私有
   bucket/服务器拉取，校验 sha256；
3. **脱敏入库**：删除图层名中的客户/项目信息后再入库。

大体积样本（如 citizen 45MB）建议**不入库**：用
`scripts/backfill_validated_cases.py --sources <目录>` 在本地跑，CI 侧以
`pytest.skip` 显式跳过并打印原因。

## 使用方式

期望值集中在 `tests/fixtures/benchmark_cases.json`（不在代码里写死），由
`scripts/benchmark.py` 执行：

```bash
# 快车道（合成 + 可用的真实样本），CI 跑这个
python -X utf8 scripts/benchmark.py --json benchmark_report.json

# 本地完整回归（含 slow 的排板用例；缺样本即失败）
python -X utf8 scripts/benchmark.py --include-slow --strict

# 只跑指定用例
python -X utf8 scripts/benchmark.py --names cgr45_read,synthetic_layer_rename
```

用例覆盖：CGR45 读图金标准（203/306/203）、countertop2、mercury 原位 DXF、
以及不依赖外部文件的合成用例（嵌套多段线孔洞、图层扰动）。每个读取用例都会
跑两遍并比对结果摘要 digest，锁定确定性。
