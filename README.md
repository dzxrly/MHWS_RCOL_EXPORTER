# RCOL Exporter

RCOL Exporter 是一个用于解析 RE Engine `*.rcol.xx` 文件的小工具。它可以把碰撞箱、RequestSet、RSZ 用户数据和动作值相关字段导出为 JSON。

当前支持两种 JSON：

- `readable`：人类可读，结构接近游戏导出的参考 JSON，适合查看和编辑碰撞/动作值。
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

## Web 快速导出

启动本地 Web 页面：

```powershell
python .\main.py web --port 8766
```

然后打开：

```text
http://127.0.0.1:8766/
```

页面上方只有两个来源按钮：`Select file` 用于选择单个 RCOL 文件，`Select folder` 用于选择一个 RCOL 目录。下方的输出目录、`rszmhws.json` 和 `il2cpp_dump.json` 仍保留选择器；点击导出后页面会显示进度条直到 export 完成。Web 和 CLI 共用同一个 `RCOLConverter`，输出结果一致。

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

`repack` 会额外包含：

```json
{
  "_format": "rcol_repack_v2",
  "_binary": {
    "encoding": "hex",
    "sha256": "...",
    "data": "..."
  }
}
```

需要人工查看时使用 `readable`。需要无损保存或后续重新封装时使用 `repack`。

## 项目结构

```text
main.py                  快速入口
demo.py                  兼容入口
rcol_exporter/api.py     对外 Python API
rcol_exporter/cli.py     CLI 入口
rcol_exporter/web/       本地 Web 快速导出
rcol_exporter/layout.py  RCOL 外层结构表和 shape 参数模板
rcol_exporter/rcol.py    RCOL 容器解析
rcol_exporter/rsz.py     RSZ 对象图解析
rcol_exporter/il2cpp.py  il2cpp_dump 按需元数据提取
RCOL解析报告.md          解析原理报告
```
