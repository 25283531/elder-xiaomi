"""形象管理：EAF 序列帧资源 + 片段映射 + 情绪灯效 + 资源包。

⚠️ 形象已**退化为纯形象**（架构 §4.2）：人格与角色级音色搬到了 AIProvider，
这里不再暴露 system_prompt。设备侧「换外观」与「换人格」从此互不影响。

⚠️ 2026-10-31 职责重划（用户定调）：
    形象主管 = 灯效 / EAF 资源 / clips（动画片段映射）/ 资源包
    人格预设主管 = 声音（音色）/ 定义（system_prompt + traits）
两处**不许出现同一个可编辑项** —— 否则用户改了不生效的那一处，会以为改错了。

``tts_voice`` 列**保留但不读不写**：本项目没有 Alembic，schema_patch 只能
ADD COLUMN、删不掉列，所以列还留在库里。但它已经**不再参与任何下发**
（device_api.sync 的音色只取角色的），形象表单也不带它 ——
让 PATCH 走 exclude_unset 根本不碰这一列，是"列在、语义已死"的最省事写法。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..avatar_style import (  # noqa: F401  (DEFAULT_CLIPS 等被 main.py 复用)
    BRIGHTNESS_MAX, BRIGHTNESS_MIN, CLIP_STATES, DEFAULT_CLIPS, DEFAULT_LIGHTS,
    INTERVAL_MAX, INTERVAL_MIN, LIGHT_EFFECTS, LIGHT_PRESETS, LIGHT_STATES,
    normalize_clips, normalize_lights,
)
from ..db import get_db
from ..deps import get_current_user, require_avatar
from ..schemas import AvatarIn, AvatarOut

router = APIRouter(prefix="/api/avatars", tags=["avatars"])


@router.post("", response_model=AvatarOut, status_code=status.HTTP_201_CREATED)
def create_avatar(payload: AvatarIn, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """新建一个**用户形象**（归属当前用户）。

    record_type 默认 'role' + owner_id = 当前用户：新建物体一律属于创建者。
    只有管理员通过直接改库（或未来的管理端接口）才能造 preset 形象。
    """
    av = models.Avatar(**payload.model_dump())
    if not av.clips:
        av.clips = dict(DEFAULT_CLIPS)
    if not av.emotion_lights:
        av.emotion_lights = dict(DEFAULT_LIGHTS)
    av.clips = normalize_clips(av.clips)
    av.emotion_lights = normalize_lights(av.emotion_lights)
    av.record_type = models.RECORD_TYPE_ROLE
    av.owner_id = user.id
    db.add(av)
    db.commit()
    db.refresh(av)
    return av


@router.get("", response_model=list[AvatarOut])
def list_avatars(db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """形象列表：过滤已软删的；非管理员只看 preset + 自己的。

    ⚠️ 顺手补上了鉴权。现状这个接口连 ``get_current_user`` 都没有 ——
    与 providers 的 P0-2 是同一类问题，一并收掉。
    """
    q = select(models.Avatar).where(models.Avatar.deleted_at.is_(None))
    if not user.is_admin:
        q = q.where(
            (models.Avatar.record_type == models.RECORD_TYPE_PRESET)
            | (models.Avatar.owner_id == user.id)
        )
    return list(db.scalars(q.order_by(models.Avatar.id.desc())).all())


# ⚠️ 这个路由**必须**注册在 ``/{avatar_pk}`` 之前。
# FastAPI 按注册顺序匹配：放后面的话 ``GET /api/avatars/light-presets``
# 会先命中 ``/{avatar_pk}``，``"light-presets"`` 转 int 失败 → 422，
# 前端表现为"灯效下拉永远是空的"，而日志里只有一条 validation error，
# 跟"路由写错"看起来毫无关系。
@router.get("/light-presets")
def light_presets(user: models.User = Depends(get_current_user)):
    """灯效编辑器要的元数据：状态清单 + 预置灯效 + 动效清单 + 取值范围。

    **由后端下发而不是前端硬编码**：加一种灯效只改这里的 LIGHT_PRESETS，
    管理端与用户端两个前端自动都有；硬编码则必然出现"两端清单不一致"。

    仍然要登录：内容本身不敏感（纯常量），但本仓的约定是"业务接口一律先认人" ——
    留一个匿名端点，将来谁往里塞一句"当前生效的设备列表"就没人拦得住了。
    """
    _ = user  # 只为鉴权，不参与逻辑
    return {
        "states": [{"key": k, "label": lab, "desc": desc} for k, lab, desc in LIGHT_STATES],
        "presets": [dict(p) for p in LIGHT_PRESETS],
        "effects": [{"effect": e, "label": lab, "interval": iv}
                    for e, lab, iv in LIGHT_EFFECTS],
        "defaults": {k: dict(v) for k, v in DEFAULT_LIGHTS.items()},
        "clip_states": [{"key": k, "label": lab} for k, lab in CLIP_STATES],
        "brightness": {"min": BRIGHTNESS_MIN, "max": BRIGHTNESS_MAX},
        "interval_ms": {"min": INTERVAL_MIN, "max": INTERVAL_MAX},
    }


@router.get("/{avatar_pk}", response_model=AvatarOut)
def get_avatar(avatar_pk: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    return require_avatar(db, avatar_pk, user)


@router.patch("/{avatar_pk}", response_model=AvatarOut)
def update_avatar(avatar_pk: int, payload: AvatarIn, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """改形象（P0-3 修复：加归属 / is_admin 守卫）。

    修复前任何登录用户都能改平台预设形象 —— 改掉 clips 或灯效映射，
    等于让全平台所有设备的屏幕表现被人为改变。
    """
    av = require_avatar(db, avatar_pk, user, write=True)
    # exclude_unset：只改请求里**真的带了**的字段。
    # 用全量 model_dump 的后果是"前端没渲染某个字段 ⇒ 该字段被重置成默认值"，
    # 而形象表单里 `enabled` 这类开关并不总在弹窗里出现 —— 那样每保存一次外观，
    # 就会把"是否启用"悄悄重置成 true。
    data = payload.model_dump(exclude_unset=True)
    # clips / emotion_lights 走归一而不是原样落库：这两个字段在界面上是
    # "每状态一行"的固定表单，前端漏传一个状态就会让该状态在库里消失，
    # 而设备按 key 查表取不到 → 该状态下灯不亮、表情不动，且没有任何报错。
    if "clips" in data:
        data["clips"] = normalize_clips(data["clips"])
    if "emotion_lights" in data:
        data["emotion_lights"] = normalize_lights(data["emotion_lights"])
    for k, v in data.items():
        setattr(av, k, v)
    # 归属信息不接受请求体覆写：改了就等于"把平台形象变成我的"
    # 或"把我的形象公开"，两者都必须走显式的归属变更路径。
    av.record_type = models.RECORD_TYPE_PRESET if av.owner_id is None else models.RECORD_TYPE_ROLE
    db.commit()
    db.refresh(av)
    return av


@router.delete("/{avatar_pk}", status_code=status.HTTP_204_NO_CONTENT)
def delete_avatar(avatar_pk: int, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """删除形象（P0-3 修复 + 改为软删）。

    为什么改软删：``devices.avatar_id`` 是指向本表的外键，真删会让已绑定
    该形象的设备留下悬空引用，心跳下发时行为不可预期。软删后读路径统一按
    "未绑定" 处理（device_api.sync 里 avatar 取不到就下发 None）。
    行的其余信息保留，审计与回溯不断。
    """
    av = require_avatar(db, avatar_pk, user, write=True)
    av.deleted_at = models.utcnow()
    db.commit()


@router.post("/{avatar_pk}/apply/{device_pk}", status_code=status.HTTP_200_OK)
def apply_avatar(avatar_pk: int, device_pk: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """把形象应用到指定设备（设备下次心跳即生效）。

    返回里带 ``device_name`` / ``avatar_name`` 是给前端做成功提示用的：
    应用形象是个"看不见"的操作（设备要等下一次心跳才换脸），页面不给人话
    反馈的话，用户只会看到下拉框跳回原位 —— 2026-10-29 用户报「选择后无法
    应用，会自动恢复到选择设备」就是这个：接口其实成功了，界面一句话都没有。
    """
    from ..deps import require_device_owner

    dev = require_device_owner(device_pk, user, db)
    av = require_avatar(db, avatar_pk, user)
    if av.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "形象不存在")
    dev.avatar_id = av.id
    db.commit()
    return {"ok": True, "device_id": dev.device_id, "device_pk": dev.id,
            "device_name": dev.name, "avatar_id": av.id, "avatar_name": av.name}