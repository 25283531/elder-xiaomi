# 第三方素材：Microsoft Fluent Emoji（3D）

本目录下的 21 张 PNG 是 Microsoft Fluent Emoji 3D 素材的**派生作品**：
裁掉透明外框、等比缩放、居中对齐到 128x128 透明画布，并重命名为本固件
使用的语义名。原图未做任何调色或重绘。

## 来源

- 仓库：https://github.com/microsoft/fluentui-emoji
- 分支：`main`
- 上游 commit：`1ffb34c752ecf5d402f04cfb4b392c77f57c54bc`（取自 git ls-remote）
- 路径规则：`assets/<官方表情名>/3D/<官方名小写下划线>_3d.png`
- 原图规格：256x256 PNG / 8 bit RGBA

## 我们做的修改

1. 按 alpha>0 的包围盒裁出内容区（原图画布上图形只占 87.5%，其余是投影留白）；
2. 等比缩放到 120x120 内容区（四边各留 4px 透明边），预乘 alpha 后重采样以避免透明区黑边渗色；
3. 居中贴到 128x128 透明画布；
4. 文件重命名为固件语义名（`neutral.png` 等），与 `emoji_collection` 词表一一对应。

生成工具：`tools/fetch_fluent_emoji.py`（Pillow 12.2.0）。该脚本只在开发机跑一次，
产物入库，CI 不执行。

## 文件对照

| 本目录文件 | 上游官方名 | 上游文件名 |
|---|---|---|
| `neutral.png` | Neutral face | `neutral_face_3d.png` |
| `happy.png` | Slightly smiling face | `slightly_smiling_face_3d.png` |
| `laughing.png` | Grinning squinting face | `grinning_squinting_face_3d.png` |
| `funny.png` | Face with tears of joy | `face_with_tears_of_joy_3d.png` |
| `sad.png` | Pensive face | `pensive_face_3d.png` |
| `angry.png` | Angry face | `angry_face_3d.png` |
| `crying.png` | Crying face | `crying_face_3d.png` |
| `loving.png` | Smiling face with heart-eyes | `smiling_face_with_heart-eyes_3d.png` |
| `embarrassed.png` | Flushed face | `flushed_face_3d.png` |
| `surprised.png` | Hushed face | `hushed_face_3d.png` |
| `shocked.png` | Face screaming in fear | `face_screaming_in_fear_3d.png` |
| `thinking.png` | Thinking face | `thinking_face_3d.png` |
| `winking.png` | Winking face | `winking_face_3d.png` |
| `cool.png` | Smiling face with sunglasses | `smiling_face_with_sunglasses_3d.png` |
| `relaxed.png` | Relieved face | `relieved_face_3d.png` |
| `delicious.png` | Face savoring food | `face_savoring_food_3d.png` |
| `kissy.png` | Face blowing a kiss | `face_blowing_a_kiss_3d.png` |
| `confident.png` | Smirking face | `smirking_face_3d.png` |
| `sleepy.png` | Sleeping face | `sleeping_face_3d.png` |
| `silly.png` | Winking face with tongue | `winking_face_with_tongue_3d.png` |
| `confused.png` | Face with rolling eyes | `face_with_rolling_eyes_3d.png` |

## 许可证（上游 LICENSE 全文）

```
MIT License

Copyright (c) Microsoft Corporation.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

MIT 允许商用、修改、闭源再分发；唯一义务是**保留上述版权声明和许可声明**
（本文件即履行该义务，随固件源码一起分发）。
