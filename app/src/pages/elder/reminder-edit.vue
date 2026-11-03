<route lang="json">
{
  "style": { "navigationBarTitleText": "编辑提醒" }
}
</route>

<script setup lang="ts">
import { onLoad } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'
import { createReminder, deviceTimezone, listReminders, updateReminder } from '@/api/elder'
import type { DeviceTimezone, MedicineItem, Reminder } from '@/api/elder'

const devicePk = ref(0)
const editId = ref(0)

const typeOptions = ['服药', '日程', '喝水', '运动', '自定义']
// ⚠️ 这个数组的**下标就是服务端 ScheduleKind 的值**（0=ONCE 1=DAILY 2=WEEKLY
// 3=MONTHLY 4=INTERVAL_DAYS，见 server/app/models.py 的 ScheduleKind）。
// pickKind() 直接把 picker 的下标写进 form.schedule_kind，所以**改这里的顺序
// 等于静默改掉所有规则的含义**，而不是改显示顺序。
//
// 2026-10-27 就是这么错的：原来写成 ['每天', '每周…', '每月…', '每隔 N 天',
// '指定日期一次']，整体与枚举错位一格 —— 用户在 APP 上选"每天"实际存成
// ONCE（0）、选"指定日期一次"实际存成 INTERVAL（4）。症状是"服务端显示
// 一次性、设备每天响"，而且存成 ONCE 的那些规则连日期都没机会填。
// 模板里的 v-if 分支（=== 0/2/3/4）本来就是按枚举写的、是对的，不要动。
const kindOptions = ['指定日期一次', '每天', '每周指定几天', '每月指定几号', '每隔 N 天']
const weekCn = ['一', '二', '三', '四', '五', '六', '日']

const form = ref<Partial<Reminder>>({
  title: '',
  speak_text: '',
  rtype: 0,
  schedule_kind: 1,
  times_of_day: ['08:00'],
  weekdays: [],
  month_days: [],
  interval_days: 2,
  once_date: '',
  start_date: '',
  end_date: '',
  dose: '',
  medicines: [],
  require_confirm: true,
  repeat_interval_min: 10,
  repeat_times: 3,
  escalate_after_min: 30,
  confirm_methods: ['voice', 'button'],
  enabled: true,
})

const timesText = ref('08:00')
const monthText = ref('')
const methods = ref<Record<string, boolean>>({ voice: true, button: true, app: false })

/**
 * 这台设备按哪个时区提醒（GET /api/devices/{pk}/timezone）。
 *
 * 为什么必须显示出来：提醒是**从设备上响出来**的，"早上 8 点吃药"指的是
 * 老人眼前那个钟的 8 点。家属手机在北美、设备在非洲时，按手机时区理解的
 * 8 点会整整错 7 小时 —— 这是服药提醒，错小时是安全问题，所以要在保存
 * **之前**就告诉他这句话会被哪个钟执行。
 *
 * `null` = 还没查到（不显示），`ok=false` = 判不出来（显示服务端给的兜底说明）。
 * **判不出来不是错误**：设备在 NAT 后面、刚出厂还没联过网都会这样。
 */
const devTz = ref<DeviceTimezone | null>(null)

/** 用户填的第一个钟点，换算成"相当于北京时间几点"。空串 = 算不出来。 */
const tzEquiv = computed(() => {
  const t = devTz.value
  if (!t || !t.ok) return ''
  const first = (timesText.value || '').split(/[,，\s]+/).filter(Boolean)[0] || ''
  const m = /^(\d{1,2}):(\d{2})$/.exec(first)
  if (!m) return ''
  // 规则时区钟点 = 北京钟点 + 偏差 ⇒ 北京钟点 = 规则钟点 - 偏差。
  // 拉各斯 08:00、偏差 -420 ⇒ 08:00 - (-420min) = 15:00。
  const raw = Number(m[1]) * 60 + Number(m[2]) - t.offset_from_beijing_min
  const norm = ((raw % 1440) + 1440) % 1440
  const hh = String(Math.floor(norm / 60)).padStart(2, '0')
  const mm = String(norm % 60).padStart(2, '0')
  return `${hh}:${mm}`
})

/** 服务端限制：一条提醒最多 4 种药。超了接口会 422，所以端侧先拦住。 */
const MAX_MEDICINES = 4

/** 药名/剂量上限，跟服务端字段长度一致，输入框 maxlength 也用这两个值 */
const NAME_MAX = 32
const DOSE_MAX = 64

/** 已经加满 4 种药 —— 用来把「+ 添加一种药」置灰 */
const medFull = computed(() => (form.value.medicines || []).length >= MAX_MEDICINES)

/**
 * 加一行空药。
 *
 * 为什么**允许删到 0 行**（而不是至少留 1 行）：
 * 提醒不只是吃药 —— 「喝水」「散步」「复诊」这些日程提醒本来就没有药。
 * 强制留一行空药，家属每次建日程都得手动删一次，删不掉就会存进一条
 * 药名空白的脏数据；服务端本身也允许空数组，所以这里就让它能空。
 */
function addMedicine() {
  if (medFull.value) {
    uni.showToast({ title: `最多 ${MAX_MEDICINES} 种药`, icon: 'none' })
    return
  }
  if (!form.value.medicines)
    form.value.medicines = []
  form.value.medicines.push({ name: '', dose: '' })
}

/** 删掉第 i 行。删到 0 行也允许（见 addMedicine 的注释）。 */
function removeMedicine(i: number) {
  form.value.medicines?.splice(i, 1)
}

function pickType(e: any) { form.value.rtype = Number(e.detail.value) }
function pickKind(e: any) { form.value.schedule_kind = Number(e.detail.value) }

function toggleWeek(i: number) {
  const list = new Set(form.value.weekdays || [])
  const v = i + 1
  if (list.has(v)) list.delete(v)
  else list.add(v)
  // ⚠️ 必须用 Array.from 而不是 [...list]：tsconfig 的 target 低于 es2015，
  // 展开 Set 会报 TS2802（需要 downlevelIteration）。**不要**为此去改
  // tsconfig —— 那是全工程编译目标，为一个循环不值得。
  form.value.weekdays = Array.from(list).sort((a, b) => a - b)
}

function toggleMethod(k: string) {
  methods.value[k] = !methods.value[k]
}

async function load() {
  if (!editId.value) return
  const all = await listReminders(devicePk.value)
  const hit = all.find(x => x.id === editId.value)
  if (!hit) return
  // ⚠️ 两个坑，都在这一句里解决：
  // 1) 老数据（2026-11 之前的行）没有 medicines 字段，接口返回 undefined，
  //    模板里 v-for="(m, i) in form.medicines" 会直接抛错白屏 —— 兜成 []。
  // 2) { ...hit } 是浅拷贝，medicines 数组仍是 listReminders 缓存里的同一个
  //    实例；用户在编辑页改药名会顺手改掉缓存，取消返回后列表显示的是改过的值。
  //    所以每一项也重新建对象。
  form.value = {
    ...hit,
    medicines: (hit.medicines || []).map((m: MedicineItem) => ({
      name: m?.name || '',
      dose: m?.dose || '',
    })),
  }
  timesText.value = (hit.times_of_day || []).join(', ')
  monthText.value = (hit.month_days || []).join(',')
  methods.value = {
    voice: (hit.confirm_methods || []).includes('voice'),
    button: (hit.confirm_methods || []).includes('button'),
    app: (hit.confirm_methods || []).includes('app'),
  }
}

async function save() {
  const f = form.value
  if (!f.title) {
    uni.showToast({ title: '请填写标题', icon: 'none' })
    return
  }
  const times = timesText.value.split(/[,，\s]+/).filter(Boolean)
  if (times.length === 0) {
    uni.showToast({ title: '至少填一个触发时刻', icon: 'none' })
    return
  }
  for (const t of times) {
    if (!/^\d{1,2}:\d{2}$/.test(t)) {
      uni.showToast({ title: `时刻格式不对：${t}`, icon: 'none' })
      return
    }
  }

  // ---- 药物列表清洗 ----
  // 规则（三条都是为了「不静默丢数据」）：
  //   · 药名和剂量都空  → 直接丢掉。这是「加了行但没填」的残留，不该存进库。
  //   · 有药名          → 保留（剂量可以为空，只吃一种药不写剂量很常见）。
  //   · **只有剂量没药名** → 拦下来报错，不提交。直接丢掉等于把用户填的
  //     "1片" 悄悄吞了，家属会以为保存成功；直接存下去又会在列表里显示成
  //     一条没有名字的药，看着像 bug。让用户补个药名最省事。
  const medicines: MedicineItem[] = []
  const rows = f.medicines || []
  for (let i = 0; i < rows.length; i++) {
    const name = (rows[i]?.name || '').trim()
    const dose = (rows[i]?.dose || '').trim()
    if (!name && !dose) continue
    if (!name) {
      uni.showToast({ title: `第 ${i + 1} 种药只填了剂量，请补药名`, icon: 'none' })
      return
    }
    medicines.push({ name: name.slice(0, NAME_MAX), dose: dose.slice(0, DOSE_MAX) })
  }
  if (medicines.length > MAX_MEDICINES) {
    uni.showToast({ title: `最多 ${MAX_MEDICINES} 种药`, icon: 'none' })
    return
  }

  const payload: Partial<Reminder> = {
    ...f,
    // 覆盖掉 ...f 里那份未清洗的 medicines
    medicines,
    times_of_day: times,
    month_days: monthText.value.split(/[,，\s]+/).filter(Boolean).map(Number),
    confirm_methods: Object.keys(methods.value).filter(k => methods.value[k]),
    once_date: f.once_date || null,
    start_date: f.start_date || null,
    end_date: f.end_date || null,
  }

  try {
    if (editId.value) await updateReminder(editId.value, payload)
    else await createReminder(devicePk.value, payload)
    uni.showToast({ title: '已保存', icon: 'success' })
    setTimeout(() => uni.navigateBack(), 600)
  }
  catch { /* 已提示 */ }
}

/**
 * 拉「这台设备按哪个时区」。
 *
 * 失败**只影响提示条**，绝不影响保存：查不到时区不显示这一块，用户照常能
 * 建提醒（服务端照样会按设备时区落库）。把一次只读的辅助请求做成保存的
 * 前置条件，等于让"提示查不到"变成"提醒建不了"。
 */
async function loadDevTz() {
  if (!devicePk.value) return
  try {
    devTz.value = await deviceTimezone(devicePk.value)
  }
  catch {
    devTz.value = null
  }
}

onLoad((options: any) => {
  devicePk.value = Number(options?.devicePk || 0)
  editId.value = Number(options?.id || 0)
  if (editId.value) load()
  loadDevTz()
})
</script>

<template>
  <view class="page">
    <!-- 同步时延告知（2026-10-29 用户要求）。
         为什么是 5 分钟：提醒由**设备本地**响，服务端新加的提醒要等设备下次
         心跳才拿到，心跳周期 sync_interval_sec 默认 300 秒（server/app/schemas.py）。
         刻意写成**静态文案**、不按当前时间动态变红：times_of_day 是设备所在
         时区的钟点，而手机在用户所在时区，两边可能不同，动态判断会误报。 -->
    <view class="card notice">
      <text class="notice-t">保存后最多需要 5 分钟才会同步到设备</text>
      <text class="notice-s">提醒时间如果距离现在不足 5 分钟，这一次设备会漏报 —— 请把时间设在 5 分钟以后。</text>
    </view>

    <!-- 设备时区告知（2026-11-04 用户要求）。
         场景：手机在北美、设备在非洲、服务器在北京。家属填的"上午 8 点"
         是按**设备**那个钟执行的，不说清楚他会以为是北美的 8 点。
         文案 hint 由服务端拼（单一真源），这里只渲染 + 补一句换算。
         devTz 为 null（还没查到 / 查失败）时整块不显示 —— 不能因此拦住保存。 -->
    <view v-if="devTz" class="card notice">
      <text class="notice-t">提醒时间按设备所在时区生效</text>
      <text class="notice-s">{{ devTz.hint }}</text>
      <text v-if="tzEquiv && devTz.offset_from_beijing_min !== 0" class="notice-s">
        您填的 {{ timesText }} 相当于北京时间 {{ tzEquiv }}。
      </text>
    </view>

    <view class="card">
      <text class="label">标题</text>
      <input v-model="form.title" class="input" placeholder="如：降压药" />

      <text class="label">播报文案（留空则读标题）</text>
      <input v-model="form.speak_text" class="input" placeholder="该吃降压药了，一片" />

      <text class="label">药物（可加多种，每种带自己的剂量）</text>
      <!-- key 用下标：这是**可增删的编辑行**，用下标做 key 在删中间一行时会复用
           组件，但 v-model 绑定的是 m.name / m.dose 本身，重新渲染时 value 会被
           重新 patch 成新一行的值，不会出现"框里留着上一行文字"。 -->
      <view v-for="(m, i) in (form.medicines || [])" :key="i" class="med-row">
        <input v-model="m.name" class="input med-name" :maxlength="NAME_MAX" placeholder="药名，如：降压药" />
        <input v-model="m.dose" class="input med-dose" :maxlength="DOSE_MAX" placeholder="剂量，如：1 片" />
        <text class="med-del" @click="removeMedicine(i)">删除</text>
      </view>
      <view class="med-add-wrap">
        <text class="med-add" :class="medFull ? 'off' : ''" @click="addMedicine">+ 添加一种药</text>
        <text v-if="medFull" class="tip med-full">最多 4 种药</text>
      </view>

      <!-- 老字段保留：2026-11 之前的提醒把药名塞在「标题」、剂量塞在 dose，
           老数据还在库里跑，不能把输入框摘掉 —— 摘了家属就改不了老提醒的剂量。
           标签明确写成「备注 / 老剂量」，避免有人以为这里是新药列表的剂量。 -->
      <text class="label">备注 / 老剂量</text>
      <input v-model="form.dose" class="input" placeholder="1 片，饭后" />
      <view class="tip">新数据请用上面的药物列表；这里留给老提醒，也当整条提醒的备注用。</view>

      <text class="label">类型</text>
      <picker mode="selector" :range="typeOptions" :value="form.rtype" @change="pickType">
        <view class="picker">{{ typeOptions[form.rtype || 0] }}</view>
      </picker>

      <text class="label">重复方式</text>
      <picker mode="selector" :range="kindOptions" :value="form.schedule_kind" @change="pickKind">
        <view class="picker">{{ kindOptions[form.schedule_kind || 0] }}</view>
      </picker>
    </view>

    <view class="card">
      <text class="label">触发时刻（24 小时制，逗号分隔）</text>
      <input v-model="timesText" class="input" placeholder="08:00, 12:00, 20:00" />

      <template v-if="form.schedule_kind === 2">
        <text class="label">星期（可多选）</text>
        <view class="chips">
          <text
            v-for="(w, i) in weekCn" :key="i"
            :class="['chip', (form.weekdays || []).includes(i + 1) ? 'on' : '']"
            @click="toggleWeek(i)">{{ w }}</text>
        </view>
      </template>

      <template v-if="form.schedule_kind === 3">
        <text class="label">每月几号（逗号分隔）</text>
        <input v-model="monthText" class="input" placeholder="1,15" />
      </template>

      <template v-if="form.schedule_kind === 4">
        <text class="label">每隔天数</text>
        <input v-model="form.interval_days" class="input" type="number" placeholder="2" />
      </template>

      <template v-if="form.schedule_kind === 0">
        <text class="label">日期</text>
        <input v-model="form.once_date" class="input" placeholder="2026-10-15" />
      </template>

      <text class="label">生效开始（可选）</text>
      <input v-model="form.start_date" class="input" placeholder="2026-01-01" />
      <text class="label">生效结束（可选）</text>
      <input v-model="form.end_date" class="input" placeholder="2027-12-31" />
    </view>

    <view class="card">
      <text class="label">确认与超时策略</text>
      <view class="line">
        <text class="l">需要确认</text>
        <switch :checked="form.require_confirm" color="#2563eb" @change="form.require_confirm = $event.detail.value" />
      </view>
      <view class="line">
        <text class="l">未确认重复间隔（分钟）</text>
        <input v-model="form.repeat_interval_min" class="inline" type="number" />
      </view>
      <view class="line">
        <text class="l">超时升级（分钟）</text>
        <input v-model="form.escalate_after_min" class="inline" type="number" />
      </view>
      <text class="label">允许的确认方式</text>
      <view class="chips">
        <text :class="['chip', methods.voice ? 'on' : '']" @click="toggleMethod('voice')">语音</text>
        <text :class="['chip', methods.button ? 'on' : '']" @click="toggleMethod('button')">按键</text>
        <text :class="['chip', methods.app ? 'on' : '']" @click="toggleMethod('app')">手机端</text>
      </view>
      <view class="tip">按键方式：长按 3 秒听到提示后松开，30 秒内再短按一次即视为完成</view>
    </view>

    <button class="btn save" @click="save">保 存</button>
  </view>
</template>

<style scoped>
.page { padding: 24rpx; background: #f5f7fa; min-height: 100vh; }
.card { background: #fff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; }
.label { font-size: 24rpx; color: #6b7280; display: block; margin: 12rpx 0 8rpx; }
.input { border: 1rpx solid #e3e8ef; border-radius: 10rpx; padding: 16rpx; font-size: 28rpx; background: #fff; }
.inline { width: 160rpx; border: 1rpx solid #e3e8ef; border-radius: 8rpx; padding: 8rpx; font-size: 26rpx; text-align: right; }
.picker { border: 1rpx solid #e3e8ef; border-radius: 10rpx; padding: 16rpx; font-size: 28rpx; }
.line { display: flex; justify-content: space-between; align-items: center; padding: 12rpx 0; }
.l { font-size: 26rpx; color: #374151; }
.chips { display: flex; flex-wrap: wrap; }
.chip { padding: 10rpx 24rpx; margin: 8rpx 12rpx 0 0; border-radius: 28rpx; background: #f1f3f6; color: #6b7280; font-size: 26rpx; }
.chip.on { background: #2563eb; color: #fff; }
.tip { font-size: 22rpx; color: #8a8f99; margin-top: 12rpx; }
/* 药物行：药名弹性占满，剂量固定宽度（flex:none 防止被药名挤扁），删除按钮跟在后面。
   min-width:0 是给 flex 子项兜底的 —— uni-app 的 input 默认 width:100%，
   不加这一条时内容一长会把剂量框挤出屏幕。 */
.med-row { display: flex; flex-direction: row; align-items: center; margin-bottom: 12rpx; }
.med-name { flex: 1; min-width: 0; margin-right: 12rpx; }
.med-dose { width: 200rpx; flex: none; margin-right: 12rpx; }
.med-del { font-size: 24rpx; color: #d93025; padding: 12rpx 8rpx; flex: none; }
.med-add-wrap { display: flex; flex-direction: row; align-items: center; margin-top: 4rpx; }
.med-add { font-size: 26rpx; color: #2563eb; padding: 12rpx 0; }
.med-add.off { color: #c0c4cc; }
.med-full { margin-top: 0; margin-left: 16rpx; }
.notice { background: #fff7e6; border: 1rpx solid #ffd591; }
.notice-t { display: block; font-size: 26rpx; font-weight: 600; color: #874d00; }
.notice-s { display: block; font-size: 22rpx; color: #ad6800; margin-top: 8rpx; line-height: 1.6; }
.btn { background: #2563eb; color: #fff; border-radius: 12rpx; font-size: 30rpx; padding: 22rpx 0; }
.save { margin-top: 12rpx; }
</style>
