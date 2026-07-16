# RCOL Exporter

RCOL Exporter 是一个用于解析 RE Engine `*.rcol.xx` 文件的小工具。它可以把碰撞箱、RequestSet、RSZ 用户数据和动作值相关字段导出为 JSON。

当前支持三种 JSON 模式：

- `readable`：人类可读，结构接近游戏导出的参考 JSON，适合查看和编辑碰撞/动作值。
- `readable-debug`：在 `readable` 基础上附加布局探测和 metadata 兼容性诊断。
- `repack`：无损格式，内含完整原始二进制 hex，可用于后续重新封装工具开发。

## 环境准备

```powershell
python -m pip install -r .\requirements.txt
```

依赖中包含 [PyREUser3](https://github.com/dzxrly/PyREUser3)，代码会通过它读取 RE Engine RSZ schema，并复用其二进制字段解析能力。

运行时建议准备两个 metadata 文件：

- `rszmhws.json`：必需，用于把 RSZ class hash 映射到类和字段。
- `il2cpp_dump.json`：可选但强烈建议，用于提取 enum 名称、字段 hint，以及 shape 参数相关 native 类型信息。

如果这两个文件放在输入目录附近、当前目录或 `debug/` 下，工具会自动查找。

解析 RSZ 用户数据时，工具会把 RCOL 文件扩展版本作为提示，并结合当前 `rszmhws.json` 自动尝试不同数量的 native `v*` 头字段。候选结果会按 `RequestSetIndex` 一致性、非法引用和未解析 instance 数量打分，避免新版 schema 解析旧文件时出现字段整体错位。

RCOL 外层布局不再依赖需要人工维护的版本画像。每个文件都会根据 `RCOL`/`RSZ` 锚点、区段指针、Group 表边界、RequestSet 连续索引和 RSZ object table 分区等不变量自动探测计数、偏移、步长及核心字段位置。批量转换时会从少量文件生成一次仅存在于内存中的目录共识，用作候选搜索提示；每个文件仍会独立校验，既不会保存画像，也不会替你切换 RSZ/IL2CPP metadata。

`rszmhws.json` 和 `il2cpp_dump.json` 仍由调用方选择。`readable-debug` 和 `repack` 输出诊断中的 `schema_compatibility` 会标记为 `compatible`、`partial` 或 `incompatible`，并同时给出未知类、CRC 不一致、未解析实例和 class coverage，便于识别 metadata 选错或不完整的情况。默认 `readable` 不写入这些调试信息。

## 快速开始

使用 `main.py` 导出一个目录：

```powershell
python .\main.py export -i .\debug --format readable -o .\output
```

只试跑一个文件或一个目录中的第一个文件：

```powershell
python .\main.py export -i .\debug --format readable -o .\output --limit 1
```

显式指定 metadata：

```powershell
python .\main.py export `
  -i .\debug\natives\STM\GameDesign\Player\ActionData\Wp12\Collision\Collider\Wp12Attack.rcol.38 `
  -s .\debug\rszmhws.json `
  -p .\debug\il2cpp_dump.json `
  -o .\output `
  --format readable
```

`demo.py` 仍然保留为兼容入口，但推荐新用法统一走 `main.py` 或包入口。

## 包入口

也可以直接使用模块入口：

```powershell
python -m rcol_exporter export -i .\debug --format readable -o .\output
```

查看帮助：

```powershell
python .\main.py --help
python .\main.py export --help
```

## 桌面 UI

启动 Tkinter 桌面界面：

```powershell
python .\main.py gui
```

也可以使用模块入口或简写：

```powershell
python -m rcol_exporter gui
python .\main.py ui
```

桌面界面可以选择单个 RCOL 文件或整个目录，并显式设置输出目录、RSZ 模板、IL2CPP dump、JSON 格式和处理数量。RSZ 模板必须手动选择；IL2CPP 留空表示禁用，不会由 GUI 自动换用其他 metadata。导出任务在后台线程运行，窗口会显示进度状态、输出文件和错误信息。GUI 和 CLI 共用同一个 `RCOLConverter`，输出结果一致；程序不再启动 HTTP server，也不依赖浏览器前端。

## Python API

如果想在脚本中调用：

```python
from rcol_exporter import RCOLConverter

converter = RCOLConverter(
    schema_path="debug/rszmhws.json",
    il2cpp_dump_path="debug/il2cpp_dump.json",
)

tree = converter.rcol_to_json(
    "debug/natives/STM/GameDesign/Player/ActionData/Wp12/Collision/Collider/Wp12Attack.rcol.38",
    json_format="readable",
)

converter.export_path(
    "debug/natives/STM/GameDesign/Player/ActionData/Wp12/Collision/Collider",
    output_root="output",
    json_format="both",
    limit=10,
)
```

## 输出格式

`readable` 顶层结构：

```json
{
  "groupInfos": [],
  "requestSets": [],
  "ignoreTags": []
}
```

需要调查布局或 metadata 兼容性时使用 `readable-debug`：

```powershell
python .\main.py export -i .\debug --format readable-debug -o .\output
```

它会在相同可读结构上增加：

```json
{
  "_diagnostics": {
    "rcol_layout": {},
    "rsz": {
      "schema_compatibility": "compatible"
    }
  }
}
```

每个 RequestSet 的 `nativeShapeColliders` 是由当前 `nativeShapeColliderObjectIndex` 到下一个 RequestSet 的 `userDataObjectIndex` 推导出的完整对象区间，因此可以包含零个、一个或多个 collider，不再固定只导出第一个对象。

`repack` 会额外包含：

```json
{
  "_format": "rcol_repack_v3",
  "_binary": {
    "encoding": "hex",
    "sha256": "...",
    "data": "..."
  }
}
```

日常查看和编辑使用 `readable`；排查解析兼容性使用 `readable-debug`；需要无损保存或后续重新封装时使用 `repack`。

## 项目结构

```text
main.py                  快速入口
demo.py                  兼容入口
rcol_exporter/api.py     对外 Python API
rcol_exporter/cli.py     CLI 入口
rcol_exporter/gui.py     Tkinter 桌面导出界面
rcol_exporter/layout.py  RCOL 外层结构表和 shape 参数模板
rcol_exporter/detect.py  RCOL 外层布局自动探测与目录内临时共识
rcol_exporter/rcol.py    RCOL 容器解析
rcol_exporter/rsz.py     RSZ 对象图解析
rcol_exporter/il2cpp.py  il2cpp_dump 按需元数据提取
RCOL解析报告.md          解析原理报告
```
