# 人造石图案资产库

本目录保存当前机器的项目级人造石标准图案清单和图片副本。

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\slab_pattern_library.py `
  --library-dir slab_patterns register `
  --pattern-id artificial-a `
  --image "D:\图案\artificial-a.png" `
  --materials "ST/1,ST/2" `
  --usable-width-mm 3400 --usable-height-mm 1400 `
  --max-pixels-per-mm 0.5
```

`library.json` 可以直接作为 `--texture-patterns` 输入。客户图片默认不进入
Git，只保留下本目录中的实际文件。
