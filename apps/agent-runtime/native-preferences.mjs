import {createHash} from 'node:crypto'
import {SettingsManager} from '@earendil-works/pi-coding-agent'

// Pi 0.99.1 exports SettingsManager at package level and its native file adapter in
// this version-pinned module. Use Pi's lock/merge/reload, never a second settings file.
const {FileSettingsStorage} = await import(new URL('./core/settings-manager.js', import.meta.resolve('@earendil-works/pi-coding-agent')).href)
const get = (value, key) => key.split('.').reduce((part, name) => part?.[name], value)
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const choices = values => values.map(([value, label]) => ({value, label}))
const field = (key, group, label, type, help, extra = {}) => ({key, group, label, type, help, ...extra})
const integer = (key, group, label, help, extra = {}) => field(key, group, label, 'integer', help, {min:0, ...extra})
export const preferenceFields = [
  field('steeringMode','interaction','补充指令的处理方式','select','在 Agent 工作时插入的指令，由 Pi 原生队列处理。',{options:choices([['one-at-a-time','逐条处理'],['all','一次处理全部']])}),
  field('followUpMode','interaction','后续消息的处理方式','select','当前任务结束后，怎样接续等待中的消息。',{options:choices([['one-at-a-time','逐条处理'],['all','一次处理全部']])}),
  field('defaultThinkingLevel','interaction','新会话默认思考深度','select','Pi 会根据所选模型支持的深度调整。当前会话不受影响。',{applies:'new_session',options:choices([['off','关闭'],['minimal','极简'],['low','轻量'],['medium','均衡'],['high','深入'],['xhigh','更深入'],['max','最深']])}),
  field('compaction.enabled','context','自动整理上下文','boolean','接近上下文上限时，由 Pi 总结较早的消息。'),
  integer('compaction.reserveTokens','context','预留上下文空间','剩余空间低于此值时触发自动整理。',{unit:'tokens'}),
  integer('compaction.keepRecentTokens','context','保留近期消息','整理后保留多少近期消息内容。原始会话历史仍在。',{unit:'tokens'}),
  integer('branchSummary.reserveTokens','context','分支总结预留空间','在会话树中切换分支时，为总结保留的空间。',{unit:'tokens'}),
  field('branchSummary.skipPrompt','context','分支切换时跳过总结询问','boolean','使用 Pi 的分支总结默认行为，不再每次询问。'),
  field('retry.enabled','retry','自动重试暂时性错误','boolean','由 Pi 处理限流、服务繁忙等可重试错误。'),
  integer('retry.maxRetries','retry','Agent 最大重试次数','不含第一次请求；0 表示不追加重试。',{unit:'次'}),
  integer('retry.baseDelayMs','retry','首次重试等待','后续等待由 Pi 的退避策略决定。',{unit:'ms'}),
  integer('retry.maxAgentDelayMs','retry','Agent 最长重试等待','超过这一等待时长时，Pi 会结束本次自动重试。',{unit:'ms'}),
  integer('retry.provider.maxRetries','retry','提供商请求重试次数','可选。留空采用 Pi 或提供商 SDK 的默认值。',{unit:'次',advanced:true}),
  integer('retry.provider.timeoutMs','retry','单次提供商请求超时','可选。由原生提供商传输层执行。',{unit:'ms',advanced:true}),
  integer('retry.provider.maxRetryDelayMs','retry','提供商最长重试等待','控制底层传输重试，与 Agent 轮次重试分开。',{unit:'ms',advanced:true}),
  field('transport','transport','模型连接方式','select','WebSocket 需要提供商支持；自动模式由 Pi 选择。',{options:choices([['sse','SSE 流式连接'],['auto','自动选择'],['websocket','WebSocket'],['websocket-cached','WebSocket 缓存连接']])}),
  integer('httpIdleTimeoutMs','transport','连接空闲超时','原生 HTTP 连接的空闲时限。0 表示不限制。',{unit:'ms'}),
  integer('websocketConnectTimeoutMs','transport','WebSocket 建连超时','可选。未设置时遵循 Pi 的原生默认值。',{unit:'ms',advanced:true}),
  field('cacheWarming','transport','提示缓存预热','select','预热可能产生模型服务费用。Pi 仅从用户设置读取此项。',{globalOnly:true,options:choices([['off','关闭'],['streaming','仅运行时预热'],['idle','空闲时也预热']])}),
  field('images.autoResize','images','自动调整图片尺寸','boolean','使用 Pi 的原生图片处理策略控制输入大小。'),
  field('images.blockImages','images','禁止向模型发送图片','boolean','图片内容会在 Pi 的模型输入边界替换为文字提示。'),
  field('defaultTools','tools','新会话启用的工具','list','每行一个工具名，也支持 +name / -name。空列表会关闭默认工具；恢复继承可回到 Pi 默认值。',{applies:'new_session'}),
  field('enableSkillCommands','tools','启用 Skill 命令','boolean','允许通过 /skill:名称 使用 Pi 发现的 Skills。'),
  field('codemode.mode','tools','Codemode 工具呈现','select','组合执行保留直接工具；仅脚本模式将工具交给 Codemode 调用。',{options:choices([['on','组合执行与直接工具'],['only','仅通过脚本调用']]),advanced:true}),
  integer('codemode.inlineBudget','tools','工具说明的上下文预算','可选。控制 Codemode 内联工具声明的估计 token 数。',{unit:'tokens',advanced:true}),
  field('shellPath','tools','终端程序路径','text','可选。使用 Pi 原生 Bash 工具的 shell 配置。',{advanced:true}),
  field('shellCommandPrefix','tools','终端命令前缀','text','可选。这个前缀会参与每次终端命令执行，仅填写你信任的内容。',{advanced:true}),
  field('skills','resources','额外 Skills 来源','list','每行一个目录或文件模式；由 Pi 原生资源加载器发现。'),
  field('extensions','resources','额外 Extensions 来源','list','扩展可以运行代码。每行一个你信任的路径或文件模式。'),
  field('prompts','resources','额外提示词来源','list','每行一个路径或模式，重载后作为原生命令使用。'),
  field('defaultProjectTrust','trust','新工作区的信任策略','select','保持询问最适合不熟悉的项目。信任工作区会允许其中的扩展代码。',{globalOnly:true,applies:'new_session',options:choices([['ask','每个工作区询问'],['never','默认不信任'],['always','默认信任']])}),
  ...['minimal','low','medium','high'].map((level,i) => integer('thinkingBudgets.'+level,'budgets',['极简','较低','中等','较高'][i]+'思考预算','可选。仅对支持显式思考预算的模型生效。',{unit:'tokens',advanced:true})),
]

function parse(value) {
  if (value === undefined) return {}
  try {
    const parsed = JSON.parse(value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error()
    return parsed
  } catch {throw new Error('Pi 设置文件格式不正确，原文件已保留。请先修复后重试。')}
}
function assign(target, key, value) {
  const names = key.split('.')
  const last = names.pop()
  let node = target
  for (const name of names) {
    if (!node[name] || typeof node[name] !== 'object' || Array.isArray(node[name])) node[name] = {}
    node = node[name]
  }
  if (value === null) delete node[last]
  else node[last] = value
}
function resolved(manager, item) {
  const getters = {steeringMode:'getSteeringMode',followUpMode:'getFollowUpMode',defaultThinkingLevel:'getDefaultThinkingLevel',
    transport:'getTransport',httpIdleTimeoutMs:'getHttpIdleTimeoutMs',websocketConnectTimeoutMs:'getWebSocketConnectTimeoutMs',cacheWarming:'getCacheWarmingMode',
    'images.autoResize':'getImageAutoResize','images.blockImages':'getBlockImages',enableSkillCommands:'getEnableSkillCommands',defaultProjectTrust:'getDefaultProjectTrust'}
  if (getters[item.key]) return manager[getters[item.key]]() ?? null
  if (item.key.startsWith('compaction.')) return get(manager.getCompactionSettings(),item.key.slice(11))
  if (item.key.startsWith('branchSummary.')) return get(manager.getBranchSummarySettings(),item.key.slice(14))
  if (item.key.startsWith('retry.provider.')) return get(manager.getProviderRetrySettings(),item.key.slice(15)) ?? null
  if (item.key.startsWith('retry.')) return get(manager.getRetrySettings(),item.key.slice(6))
  if (item.key === 'defaultTools') return manager.getDefaultTools() ?? ['read','bash','edit','write']
  if (item.key === 'codemode.mode') return manager.getSettings().codemode?.mode ?? 'on'
  return get(manager.getSettings(),item.key) ?? (item.type === 'list' ? [] : null)
}
export function createNativePreferences({cwd, agentDir, getSession, getTrust, applyRuntime}) {
  let applying = false
  const context = () => {
    const session = getSession()
    const directory = session?.sessionManager.getCwd() || cwd
    const trusted = session?.settingsManager.isProjectTrusted() ?? getTrust?.(directory) ?? false
    return {session, directory, trusted, storage:new FileSettingsStorage(directory,agentDir)}
  }
  function read() {
    const {session,directory,trusted,storage} = context()
    const raw = {}
    for (const scope of ['global','project']) storage.withLock(scope, text => {raw[scope] = parse(text)})
    const manager = SettingsManager.create(directory,agentDir,{projectTrusted:trusted})
    const globalManager = SettingsManager.inMemory(raw.global)
    const defaults = SettingsManager.inMemory({})
    const fields = preferenceFields.map(item => {
      const global = get(raw.global,item.key), project = get(raw.project,item.key)
      let value
      try {value = resolved(manager,item)} catch {throw new Error('Pi 设置值无效：'+item.label+'。请先修复设置文件。')}
      return {...item,value,defaultValue:resolved(defaults,item),scopeValues:{global:resolved(globalManager,item),project:value},stored:{global:global ?? null,project:project ?? null},
        source: !item.globalOnly && trusted && project !== undefined ? 'project' : global !== undefined ? 'global' : 'default'}
    })
    return {fields,revisions:{global:hash(raw.global),project:hash(raw.project)},projectTrusted:trusted,workspace:directory,
      runtimeReady:!!session,modelOverrides:manager.getSettings().compaction?.modelOverrides || {},
      diagnostics:manager.drainErrors().map(error=>({scope:error.scope,message:'原生设置读取异常，请检查对应的 Pi 设置文件。'}))}
  }
  async function update({scope,patch,revision,workspace}) {
    if (applying) throw new Error('Pi 设置正在保存，请稍后再试。')
    if (!['global','project'].includes(scope) || !patch || typeof patch !== 'object' || Array.isArray(patch)) throw new Error('Pi 设置更新格式无效。')
    const {session,directory,trusted,storage} = context()
    if (workspace !== directory) throw new Error('当前工作区已变化，请重新载入设置后再保存。')
    if (session && !session.isIdle) throw new Error('请先停止或等待当前任务结束，再修改 Pi 设置。')
    if (scope === 'project' && !trusted) throw new Error('这个工作区尚未通过 Pi 信任检查，请先完成原生工作区授权。')
    for (const [key,value] of Object.entries(patch)) {
      const item = preferenceFields.find(field=>field.key===key)
      if (!item || scope==='project' && item.globalOnly) throw new Error('这个设置不支持所选范围：'+key)
      if (value===null) continue
      const valid = item.type==='boolean' ? typeof value==='boolean' : item.type==='integer' ? Number.isSafeInteger(value) && value>=item.min
        : item.type==='select' ? item.options.some(option=>option.value===value)
        : item.type==='list' ? Array.isArray(value) && value.length<=200 && value.every(v=>typeof v==='string' && v.length>0 && v.length<=4096 && !/[\0\r\n]/.test(v))
        : typeof value==='string' && value.length<=4096 && !value.includes('\0')
      if (!valid) throw new Error('请填写有效的“'+item.label+'”。')
    }
    if (!Object.keys(patch).length) return {...read(),saved:false}
    applying = true
    try {
      await session?.settingsManager.flush()
      storage.withLock(scope,text=>{
        const current = parse(text)
        if (revision !== hash(current)) throw new Error('Pi 设置已在别处更新。请重新载入后再保存，你的输入已保留。')
        for (const [key,value] of Object.entries(patch)) assign(current,key,value)
        return JSON.stringify(current,null,2)+'\n'
      })
      let applied = true, message
      try {
        if (session) {
          await session.settingsManager.reload()
          await applyRuntime(session,Object.keys(patch))
        }
      } catch {applied=false;message='Pi 设置已保存，但当前运行时重载未完成。请重新载入原生能力后继续。'}
      return {...read(),saved:true,applied,message:message || (session ? '已保存到 Pi。标注为新会话生效的设置将在下次会话使用。' : '已保存到 Pi，下次启动 Agent 时生效。'),
        nextSession:Object.keys(patch).filter(key=>preferenceFields.find(field=>field.key===key)?.applies==='new_session'),workspace:directory}
    } finally {applying=false}
  }
  return {read,update,get applying(){return applying}}
}
