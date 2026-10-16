# 读图件数回归的二分复算工具（2026-10-16）

用来回答"这张图的件数是被哪次提交改掉的"。原理：把历史提交的代码树**只读导出**
到仓库外，用同一张源图复算件数，二分定位改动点。导出而不是 `git worktree`，
因为 worktree 要写 `.git`，在受限沙箱里做不了。

```bash
git -C <仓库> archive -o /tmp/tree.tar <sha> src drawing_profiles
mkdir -p /tmp/tree && tar -xf /tmp/tree.tar -C /tmp/tree
python count_drawing.py /tmp/tree mercury="<源图.dxf>" fuzhou_wanxiangcheng1="<源图.dxf>"
```

| 脚本 | 用途 |
|---|---|
| `count_drawing.py` | 在指定代码树里复算 panels/holes/numbers |
| `dump_panels.py` | 导出零件清单（面积/外包络/图层/编号/孔数），兼容新旧契约 |
| `lost_rows.py` | 把"A 有 B 没有"的零件写成 JSON |
| `diff_panels.py` | 直接打印两份清单的差集 |
| `contains_check.py` | 判断消失的件是不是房间轮廓（里面是否装着别的零件） |
| `gap_probe.py` | 统计首尾缝隙落在容差带里的多段线，并按外包络反查实体 |
| `vertex_probe.py` | 打印某实体的全部顶点、首尾距离、末点到各边距离 |
| `repro_closure.py` | 闭合容差回归的最小复现（合成 DXF，跨代码树对比） |

2026-10-16 的结论与证据见 `_analysis/worklog_2026-10-16.md` 第六节与
`AGENTS.md`「案例库立刻暴露的三处"声明值已过期"」。
