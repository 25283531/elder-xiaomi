<route lang="json">
{
  "style": { "navigationBarTitleText": "选择形象" }
}
</route>

<script setup lang="ts">
import { onLoad, onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import { applyAvatar, listAvatars, updateDevice } from '@/api/elder'
import type { Avatar } from '@/api/elder'

const devicePk = ref(0)
const deviceName = ref('')
const avatars = ref<Avatar[]>([])
const currentId = ref<number | null>(null)

async function refresh() {
  try {
    avatars.value = await listAvatars()
  }
  catch { /* 已提示 */ }
}

const browseOnly = ref(false)

async function onPick(a: Avatar) {
  // 从「我的 → 系统形象库」进来时没有具体设备，只做浏览
  if (browseOnly.value) {
    uni.showToast({ title: '请到「设备」页选择设备后再绑定形象', icon: 'none' })
    return
  }
  try {
    await applyAvatar(a.id, devicePk.value)
    currentId.value = a.id
    uni.showToast({ title: `已切换为「${a.name}」`, icon: 'success' })
  }
  catch { /* 已提示 */ }
}

async function onClear() {
  if (browseOnly.value) return
  try {
    await updateDevice(devicePk.value, { avatar_id: 0 })
    currentId.value = null
    uni.showToast({ title: '已清除形象', icon: 'success' })
  }
  catch { /* 已提示 */ }
}

// 灯效摘要：只取"待机"和"提醒"两个用户最能感知的状态。
// 2026-10-31 起灯效**已由固件消费**（服务端 device.sync 下发 lights，
// 固件 sync.c 的 apply_lights → rgb.c 的 elder_rgb_apply_config）。
// 所以这里可以如实说"设备会这样亮"，但仍要写明生效要等心跳 ——
// 设备没有常连控制通道，服务端推不动它。
function lightSummary(a: Avatar) {
  const L: Record<string, any> = a.emotion_lights || {}
  const pick = (k: string) => {
    const it = L[k]
    if (!it) return '—'
    const eff = String(it.effect || '')
    // ⚠️ 这张表是**只读展示**用的兜底：本页不提供灯效编辑，拿不到
    // /api/avatars/light-presets 的 label 时才走这里。真源仍在服务端
    // avatar_style.py 的 LIGHT_EFFECTS，加新效果必须两边一起加，
    // 否则 APP 里会显示成英文 effect 名（如 "flow"）。
    const map: Record<string, string> = {
      off: '关闭', solid: '常亮', breath: '呼吸', starlight: '星光', flow: '流水',
    }
    const name = map[eff] || eff
    // #000000 是"颜色随机"的哨兵（服务端约定，见 avatar_style.RANDOM_RGB）。
    // 直接显示 "#000000" 会让人以为设成了黑色。
    const rgb = String(it.rgb || '')
    const color = rgb.toUpperCase() === '#000000' ? '随机' : rgb
    return name + ' ' + color
  }
  return '待机 ' + pick('neutral') + ' · 提醒 ' + pick('alert')
}

onLoad((options: any) => {
  devicePk.value = Number(options?.devicePk || 0)
  browseOnly.value = devicePk.value <= 0
  deviceName.value = decodeURIComponent(options?.name || '')
  const cur = Number(options?.current || 0)
  currentId.value = cur || null
})
onShow(() => { refresh() })
</script>

<template>
  <view class="page">
    <view class="head">
      <text class="dname">{{ browseOnly ? '系统形象库' : deviceName }}</text>
      <text class="sub">{{ browseOnly ? '在「设备」页选择设备后即可绑定形象' : '切换后最长约 5 分钟自动获取生效，唤醒设备进入对话可立即生效' }}</text>
    </view>

    <view v-for="a in avatars" :key="a.id" class="card" @click="onPick(a)">
      <view class="row between">
        <text class="title">{{ a.name }}</text>
        <text v-if="currentId === a.id" class="tag ok">使用中</text>
      </view>
      <text class="sub">{{ a.description || '暂无描述' }}</text>
      <text class="sub">{{ lightSummary(a) }}</text>
      <!-- 资源包 / 分辨率这两项固件当前**都不消费**，
           所以只在下面用小字如实说明，不放在主信息行里冒充"生效中"。 -->
      <text class="sub dim">
        资源包 {{ a.eaf_pack_name || '—' }} · {{ a.screen_width }}×{{ a.screen_height }}
      </text>
      <text class="sub dim">以上两项当前固件尚未消费，暂不影响设备显示</text>
    </view>

    <view v-if="avatars.length === 0" class="tip">还没有形象，请先到 WEB 管理后台添加</view>
    <view v-if="!browseOnly" class="tip">
      音色属于人格，请在「我的 → 角色」里设置
    </view>
    <button v-if="!browseOnly" class="btn ghost" @click="onClear">清除形象</button>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; }
.head { margin-bottom: 16rpx; }
.dname { font-size: 32rpx; font-weight: 600; }
.sub { font-size: 24rpx; color: #8a8f99; margin-top: 6rpx; display: block; }
.sub.dim { color: #b0b5bd; font-size: 22rpx; }
.card { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; }
.title { font-size: 30rpx; font-weight: 600; }
.row { display: flex; flex-direction: row; align-items: center; }
.between { justify-content: space-between; }
.tag { font-size: 22rpx; border-radius: 20rpx; padding: 4rpx 16rpx; }
.tag.ok { color: #16a34a; background: #e8f7ee; }
.btn { border-radius: 12rpx; font-size: 30rpx; padding: 22rpx 0; margin-top: 12rpx; }
.btn.ghost { background: #fff; color: #dc2626; border: 1rpx solid #f3c7c7; }
.tip { font-size: 24rpx; color: #8a8f99; text-align: center; padding: 40rpx 0; }
</style>
