# RCOL 文件解析报告

## 目标

本项目中的 `*.rcol.28` 和 `*.rcol.38` 是 RE Engine 游戏资源的一类碰撞配置文件。它们通常和角色、怪物、机关、武器、弹体的碰撞体、攻击判定、动作值、受击参数等数据有关。

当前实现已经整理为 `rcol_exporter` 库，`demo.py` 只保留为兼容入口。工具会把 RCOL 二进制文件导出为 JSON，并提供两种用途不同的格式。当前实现已经按 `debug/Wp12Attack.rcol.28.json` 和 `debug/Wp12Attack.rcol.38.json` 的结构校准 readable 输出：

- `readable`：面向人工阅读，顶层为 `groupInfos`、`requestSets`、`ignoreTags`，展开 RSZ 对象引用，方便查看动作值和碰撞配置。
- `repack`：面向无损重新封装，保留完整原始二进制内容，并附带解析出的结构索引，方便后续工具按字节级别恢复或继续实现 packer。

## 使用方法

```powershell
python -m rcol_exporter export -i .\debug
```

默认输出 `readable` 格式，生成在源文件同目录下，文件名为：

```text
原文件名.json
```

常用参数：

```powershell
# 导出人工可读 JSON
python -m rcol_exporter export -i .\debug --format readable

# 导出无损 repack JSON
python -m rcol_exporter export -i .\debug --format repack

# 同时导出两种格式
python -m rcol_exporter export -i .\debug --format both

# 指定输出目录
python -m rcol_exporter export -i .\debug --output-dir .\out

# 显式指定 RSZ schema
python -m rcol_exporter export -i .\debug --schema .\debug\rszmhws.json
```

`demo.py` 仍然兼容旧命令：

```powershell
python .\demo.py .\debug --format readable
```

工具会自动在输入目录附近查找 `rszmhws.json` 和 `il2cpp_dump.json`。`rszmhws.json` 用于识别 RSZ 类和字段，`il2cpp_dump.json` 用于把枚举数字格式化成 `[值]名称`，例如 `[0]NORMAL`，也用于读取 `via.physics.ShapeType` 和 `via.Sphere` / `via.Capsule` 等 native 结构的字段偏移。

如果使用 `--format both`，输出文件会带格式后缀：

```text
xxx.rcol.38.readable.json
xxx.rcol.38.repack.json
```

## 库与 Web 入口

可以在 Python 代码中直接使用 `RCOLConverter`：

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
```

本地 Web 快速导出入口：

```powershell
python -m rcol_exporter web --port 8766
```

打开 `http://127.0.0.1:8766/` 后页面上方只有两个来源按钮：`Select file` 用于选择单个 RCOL 文件，`Select folder` 用于选择一个 RCOL 目录。下方的输出目录、schema 和 dump 仍保留选择器；点击导出后页面会显示进度条直到 export 完成。Web 页面和 CLI 调用的是同一个 `RCOLConverter`，因此输出结构一致。

当前代码结构：

| 路径 | 作用 |
| --- | --- |
| `rcol_exporter/api.py` | 对外库 API，提供单文件和目录导出 |
| `rcol_exporter/cli.py` | 命令行入口，支持 `export` 和 `web` |
| `rcol_exporter/web/server.py` | 标准库 HTTP server 快速导出页面 |
| `rcol_exporter/layout.py` | RCOL 外层 header/group/shape/request-set 的结构表和 shape 参数模板 |
| `rcol_exporter/rcol.py` | RCOL 外层解析与 RSZ 块挂接 |
| `rcol_exporter/rsz.py` | 基于 `pyreuser3` 字段解析能力的 RSZ 对象图解析 |
| `rcol_exporter/il2cpp.py` | mmap 按需抽取 `il2cpp_dump.json` 的类和枚举元数据 |

## 文件总体结构

RCOL 文件可以粗略看成两层：

```text
RCOL 外层容器
  Header
  Group 表
  Collider 记录区
  RSZ 数据块
  RequestSet / IgnoreTag 等附加段
  UTF-16 字符串池
```

其中最关键的部分是内嵌的 `RSZ` 数据块。`RSZ` 是 RE Engine 资源序列化系统常用的对象数据格式，`.user.3` 也使用类似结构。因此 `demo.py` 复用了 `pyreuser3` 中的 `BinaryReader`、`TypeDB` 和字段解析逻辑，避免重新实现一套 RSZ 字段读取器。

## RCOL Header

RCOL 文件开头 4 字节是魔数：

```text
52 43 4F 4C = "RCOL"
```

目前样本中 header 长度按 `0x70` 处理。这些字段不再散落在解析函数中，而是集中登记在 `rcol_exporter/layout.py` 的 `HEADER_COUNTS`、`HEADER_UNKNOWNS`、`HEADER_OFFSETS` 表里。重要偏移包括：

| 偏移 | 含义 |
| --- | --- |
| `0x00` | magic，固定为 `RCOL` |
| `0x04` | group 数量 |
| `0x30` | group 表偏移，通常是 `0x70` |
| `0x38` | RSZ 数据块偏移 |
| `0x40` | request set 段偏移 |
| `0x48` | ignore tag 段偏移 |
| `0x50` | auto generate joint desc 或字符串池附近偏移 |

Header 中还有一些计数字段和未知字段。脚本会保留这些原始值，避免把尚未确认的字段误命名。

## Group 表

Group 表从 header 中的 `groups` offset 开始，每条记录按 `rcol_exporter/layout.py` 中的 `GROUP_LAYOUT` 读取，当前样本大小为 `0x50` 字节。已确认或较可靠的字段包括：

| 相对偏移 | 含义 |
| --- | --- |
| `0x00` | GUID |
| `0x10` | UTF-16 名称字符串偏移 |
| `0x18` | 名称或类型 hash |
| `0x1C` | shape 数量 |
| `0x28` | collider 记录偏移 |
| `0x38` | mask GUID 数组偏移 |
| `0x40` | 第二个 GUID 或关联 GUID |

部分 group 名称为空，这是样本中的正常现象。RE Engine 资源经常同时使用 GUID、hash 和字符串池，因此不能只依赖字符串判断含义。

## Shape/Collider 记录区

Shape/Collider 记录区位于 group 表之后、RSZ 块之前。样本中每条 shape 记录按 `rcol_exporter/layout.py` 中的 `SHAPE_LAYOUT` 解析，当前大小为 `0xA0` 字节。工具目前会提取：

- GUID
- `shapeNameMMHash`
- `shapeType`
- `shapeParam`
- `primaryJointName`
- `secondaryJointName`
- `ignoreTagBits`

`shapeType` 的名称优先从 `il2cpp_dump.json` 的 `via.physics.ShapeType` 枚举读取，而不是只靠脚本内固定表。没有提供 dump 时才使用少量 fallback。已按参考样本确认的 shape type 包括：

| 数字 | readable 名称 | 参数解释 |
| --- | --- | --- |
| `1` | `Sphere` | `x/y/z/radius`，字段偏移优先来自 `via.Sphere` |
| `3` | `Capsule` | `start/end/radius`，字段偏移优先来自 `via.Capsule` |
| `4` | `ContinuousCapsule` | `start/end/radius`，字段偏移优先来自 `via.Capsule` |

在 `readable` 格式中，原始数组会被省略，只保留更容易查看的字段。在 `repack` 格式中，完整原始文件字节和 `_raw` 分段会被保留，因此不会丢失未知字段。

## RequestSet 段

RequestSet 记录从 header 的 `request_sets` offset 开始，字段集中在 `REQUEST_SET_LAYOUT` 中。已按 `Wp12Attack` 样本确认每条记录为 `0x30` 字节：

| 相对偏移 | 含义 |
| --- | --- |
| `0x00` | `requestSetID` |
| `0x04` | `groupIndex` |
| `0x08` | userData 在 RSZ object table 中的下标 |
| `0x0C` | nativeShapeCollider 在 RSZ object table 中的下标 |
| `0x10` | `status` |
| `0x14` | `requestSetIndex` |
| `0x18` | name 字符串偏移 |
| `0x20` | keyName 字符串偏移 |
| `0x28` | `keyHash` |
| `0x2C` | `KeyNameMMHash` |

读取 request set 后，脚本会根据 object table 下标找到对应的 RSZ root instance，再生成：

```json
{
  "nativeShapeColliders": [
    {
      "via.physics.RequestSetColliderUserData": {}
    }
  ],
  "userData": {
    "app.col_user_data.AttackParamPl": {}
  }
}
```

## RSZ 数据块

RSZ 块从 RCOL header 的 `rsz` offset 开始，魔数是：

```text
52 53 5A 00 = "RSZ\0"
```

RSZ header 的核心字段包括：

| 字段 | 含义 |
| --- | --- |
| `version` | RSZ 版本，样本中常见为 `16` |
| `object_count` | object table 条目数 |
| `instance_count` | instance info 条目数 |
| `userdata_count` | 外部 userdata 引用数量 |
| `instance_offset` | instance info 表偏移，相对 RSZ 块起点 |
| `data_offset` | instance 数据区偏移，相对 RSZ 块起点 |
| `userdata_offset` | userdata 表偏移，相对 RSZ 块起点 |

RSZ 的解析流程：

1. 读取 object table，得到根对象 instance id。
2. 读取 instance info 表。每条包含 `class_hash` 和 `crc`。
3. 用 `rszmhws.json` 把 `class_hash` 映射到类名和字段定义。
4. 用 `il2cpp_dump.json` 补充 enum 标签、字段 enum hint、generic 容器关系和 native shape 参数偏移。
5. 根据 RCOL 版本处理 native 字段差异，再按字段定义读取 instance 数据区。
6. 对 `Object`、`UserData` 字段保留或展开 `ref_instance_id` 引用。

例如攻击碰撞文件中常见：

```text
app.col_user_data.AttackParamEm
```

该类中可以解析出 `_Attack`、`_FixAttack`、`_StunDamage`、`_DamageTypeFixed`、`_HitEffectTypeFixed` 等字段，这些就是动作值和攻击参数相关数据。

## 为什么能复用 pyreuser3

`.user.3` 和 RCOL 都嵌入了 RE Engine 的 RSZ 序列化块。不同点在于：

- `.user.3` 外层是 USR 容器。
- `.rcol.xx` 外层是 RCOL 容器。
- 内部 RSZ 的 object table、instance info、data 区布局高度相似。

因此脚本自己解析 RCOL 外层，然后把 RSZ 块交给与 `pyreuser3` 相同的字段解析思路处理。字段大小、对齐、数组、对象引用、GUID、字符串等规则都沿用 schema 驱动解析。

### `.28` 和 `.38` 的 native 字段差异

`rszmhws.json` 中部分类的前三个字段是 native 占位字段：

```text
v0: String
v1: Data
v2: Data
```

在 `*.rcol.38` 中，`v2` 对应 `RequestSetIndex`；在 `*.rcol.28` 中这一字段实际不存在。如果按 `.38` schema 直接解析 `.28`，后续 `_Attack`、`_StunDamage` 等字段会整体错位。当前实现按文件扩展版本判断：

- `.rcol.28`：跳过 native `v2`。
- `.rcol.38`：读取 native `v2` 并输出为 `RequestSetIndex`。

## readable JSON

`readable` 格式适合人工查看，顶层结构与参考文件一致：

```json
{
  "groupInfos": [],
  "requestSets": [],
  "ignoreTags": []
}
```

其中 `requestSets[*].userData` 会展开成类型名包裹的对象，例如：

```json
{
  "app.col_user_data.AttackParamPl": {
    "_Attack": 30.0,
    "_GuardType": "[0]NORMAL"
  }
}
```

## repack JSON

`repack` 格式适合保真和后续工具链，顶层结构大致为：

```json
{
  "_format": "rcol_repack_v2",
  "_version": 2,
  "_source": {},
  "_binary": {
    "encoding": "hex",
    "sha256": "...",
    "data": "..."
  },
  "header": {},
  "groupInfos": [],
  "requestSets": [],
  "ignoreTags": [],
  "rsz": {},
  "_raw": {}
}
```

`_binary.data` 是完整原始 RCOL 文件的十六进制内容。只要这个字段存在，就可以字节级恢复原文件，因此当前 `repack` JSON 是无损的。

解析出的 `rcol` 和 `rsz` 结构用于辅助理解、定位和后续编辑器开发。未来如果要实现真正的“修改 JSON 后重新打包”，推荐按这个顺序推进：

1. 先支持只修改 RSZ instance 字段，并复写 RSZ data 区。
2. 再支持字符串池长度变化和 offset 重排。
3. 最后支持 group/collider/request set 等 RCOL 外层结构重建。

## 当前限制

- RCOL 外层仍有部分字段没有最终命名，脚本在 `repack` 的 `_raw` 中保留。
- Collider 记录、Group 记录、RequestSet 记录已经集中在 layout 表中，但这些仍然是 RCOL native 外层结构，不属于 RSZ schema；后续如果遇到不同游戏版本，需要增加版本化 layout。
- `repack` 格式已经无损携带原始 bytes，但尚未实现“修改字段后重新生成二进制”的 pack 命令。
- `il2cpp_dump.json` 很大，脚本使用 mmap 按需抽取类和 enum，并有进程内缓存；首次解析某类资源时会慢一些。
- `readable` 格式会省略 `_raw` 和 object table 内部索引，适合阅读但不是无损格式；需要无损保存时应使用 `--format repack`。

## 验证结果

本轮实现后使用 `Wp12Attack.rcol.28` 和 `Wp12Attack.rcol.38` 进行了验证：

- `readable` 顶层 key 与参考 JSON 一致：`groupInfos`、`requestSets`、`ignoreTags`。
- `.28` 与 `.38` 的 group/request/tag 数量均与参考一致。
- 首个 shape 的 GUID、类型、参数、主/副关节名均与参考一致。
- 首个 request set 的 `userData` 展开结果与参考一致，包括 enum 符号位和 `ace.Bitset` 空值处理。
- `repack` JSON 中 `_binary.data` 还原出的字节与源文件完全一致，SHA-256 为 `749e9e1639e43a7c514b5215b08ef44332030b0a6aab4ccb75fe14acc3e26f38`。

## 结论

RCOL 文件的关键是“RCOL 外层索引 + 内嵌 RSZ 对象图”。外层负责组织碰撞 group、collider、request/tag 和字符串池；RSZ 块负责保存类型化的用户数据，例如攻击参数、伤害类型、效果类型等。

当前实现已经能稳定导出两种 JSON：

- `readable` 用于查看碰撞和动作值。
- `repack` 用于无损保存和后续重新封装工具开发。
