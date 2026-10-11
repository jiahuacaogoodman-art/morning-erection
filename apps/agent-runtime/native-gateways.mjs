import {streamSimple as streamResponses} from '@earendil-works/pi-ai/api/openai-responses'

const modelFields = ['input','inputLimits','reasoning','cost','contextWindow','maxTokens','thinkingLevelMap','promptCache','compat','samplingParams']

// Some Codex-compatible Gateways require leading system instructions in the Responses
// `instructions` field. This is an HTTP representation change; Pi's transcript stays intact.
export function instructionsPayload(payload) {
  if (!payload || !Array.isArray(payload.input)) return payload
  const input = payload.input.slice()
  const instructions = []
  while (input.length && ['system','developer'].includes(input[0].role)) {
    const message = input.shift()
    instructions.push(typeof message.content === 'string' ? message.content
      : (message.content || []).map(part => part.text || '').join(''))
  }
  if (!instructions.length) return payload
  return {...payload, input, instructions:[payload.instructions, ...instructions].filter(Boolean).join('\n\n')}
}

// registerProvider requires the complete ProviderChatModelConfig (unlike models.json).
// For uncatalogued models use Pi 0.99.1 modelFromJson's documented defaults, then apply
// catalog data and the user's overrides. Contract tests compare these with Pi itself.
export function registerGateways(modelRuntime, profiles) {
  const isMorningGateway = id => /^gateway-[a-f0-9]{32}$/.test(id)
  const registered = new Set(modelRuntime.getRegisteredProviderIds().filter(isMorningGateway))
  const nativeModels = modelRuntime.getAllModels().filter(model => !isMorningGateway(model.provider))
  for (const profile of profiles || []) {
    const known = nativeModels.find(model => model.id === profile.model && (!model.type || model.type === 'chat'))
    const definition = {id:profile.model, name:profile.model, api:profile.api,
      reasoning:false, input:['text'], cost:{input:0,output:0,cacheRead:0,cacheWrite:0}, contextWindow:128000, maxTokens:16384}
    for (const field of modelFields) if (known?.[field] !== undefined) definition[field] = known[field]
    Object.assign(definition, profile.model_options || {})
    const provider = {name:profile.name, baseUrl:profile.base_url, api:profile.api, models:[definition]}
    if (profile.api === 'openai-responses' && profile.request_format === 'instructions') {
      provider.streamSimple = (model, context, options) => streamResponses(model, context, {...options,
        onPayload:async (payload, currentModel) => instructionsPayload(await options?.onPayload?.(payload, currentModel) ?? payload)})
    }
    modelRuntime.registerProvider(profile.provider_id, provider)
    registered.delete(profile.provider_id)
  }
  for (const id of registered) modelRuntime.unregisterProvider(id)
}

export function connectionError(error, redact = String) {
  const message = redact(error?.message || error || '')
  const lower = message.toLowerCase()
  const category = /401|403|api.?key|unauthori[sz]ed|authentication|credential/.test(lower) ? 'authentication'
    : /model.{0,40}(not found|not exist|invalid)|unknown model/.test(lower) ? 'model'
    : /429|rate.?limit|quota|insufficient/.test(lower) ? 'rate_limit'
    : /certificate|ssl|tls/.test(lower) ? 'tls'
    : /timeout|timed out|abort/.test(lower) ? 'timeout'
    : /404|405|protocol|unsupported|invalid.response|parse|json/.test(lower) ? 'protocol'
    : /50[0-9]|overloaded|unavailable/.test(lower) ? 'upstream'
    : 'connection'
  const messages = {authentication:'Gateway 拒绝了认证，请检查或更新凭据。', model:'Gateway 没有找到所选模型，请核对模型名称。',
    rate_limit:'Gateway 的额度或请求频率受限，请稍后重试或检查账户额度。', tls:'无法验证 Gateway 的安全连接，请核对地址和证书。',
    timeout:'Gateway 响应超时，请稍后重试。', protocol:'Gateway 地址或接口协议不匹配，请检查 API 根地址和协议。',
    upstream:'Gateway 上游暂时不可用，请稍后重试。', connection:'暂时无法完成 Gateway 请求，请检查网络和配置。'}
  return {category, message:messages[category]}
}

export async function testGateway(modelRuntime, profile, redact, options = {}) {
  const model = modelRuntime.getModel(profile.provider_id, profile.model)
  if (!model) return {ok:false, category:'model', message:'找不到所选模型的配置。'}
  const started = Date.now()
  const timeout = AbortSignal.timeout(options.timeoutMs ?? 45000)
  const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout
  try {
    const result = await modelRuntime.completeSimple(model,
      {systemPrompt:'You are a helpful assistant. Reply briefly to confirm this API connection.',
        messages:[{role:'user', content:'Reply with exactly: MORNING_CONNECTED', timestamp:Date.now()}]},
      {maxTokens:96, signal})
    signal.throwIfAborted()
    if (result.stopReason === 'error' || result.stopReason === 'aborted') throw new Error(result.errorMessage || 'Gateway response error')
    const text = result.content.filter(item => item.type === 'text').map(item => item.text).join('')
    if (!text.trim()) throw new Error('Empty Gateway response')
    return {ok:true, category:'connected', message:'认证和模型调用成功。', model:model.id,
      latency_ms:Date.now()-started, tested_at:new Date().toISOString(), usage:result.usage}
  } catch (error) {
    if (options.signal?.aborted) return {ok:false, category:'cancelled', message:'连接测试已取消。', latency_ms:Date.now()-started, tested_at:new Date().toISOString()}
    return {ok:false, ...connectionError(signal.aborted ? new Error('Request timed out') : error, redact), latency_ms:Date.now()-started, tested_at:new Date().toISOString()}
  }
}

export function publicModel(model, profile) {
  return {id:model.id, name:model.name, provider:model.provider, api:model.api, reasoning:model.reasoning,
    input:model.input, contextWindow:model.contextWindow, maxTokens:model.maxTokens,
    metadata_source:profile ? (profile.model_options && Object.keys(profile.model_options).length ? 'configured' : 'pi_defaults_or_catalog') : 'pi_catalog'}
}
