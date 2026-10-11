import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import {ModelRuntime} from '@earendil-works/pi-coding-agent'
import {instructionsPayload, registerGateways} from '../native-gateways.mjs'
import {secretRedactor, VaultCredentialStore} from '../native-credentials.mjs'

test('uncatalogued Gateway models use the same capabilities as native Pi models.json', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'morning-model-contract-'))
  try {
    const model = 'morning-uncatalogued-contract-model'
    const baseUrl = 'http://127.0.0.1:1/v1'
    const modelsPath = path.join(dir, 'models.json')
    await fs.writeFile(modelsPath, JSON.stringify({providers:{fixture:{api:'openai-responses',baseUrl,models:[{id:model}]}}}))
    const credentials = new VaultCredentialStore({}, async () => true)
    const native = await ModelRuntime.create({credentials,modelsPath,refreshOnCreate:false})
    const adapted = await ModelRuntime.create({credentials,modelsPath:path.join(dir,'none.json'),refreshOnCreate:false})
    const id = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    registerGateways(adapted,[{id,provider_id:'gateway-'+id,name:'Contract',api:'openai-responses',base_url:baseUrl,model}])
    const expected = native.getModel('fixture', model), actual = adapted.getModel('gateway-'+id,model)
    for (const key of ['input','reasoning','contextWindow','maxTokens','cost']) assert.deepEqual(actual[key],expected[key],key)
    registerGateways(adapted,[{id,provider_id:'gateway-'+id,name:'Contract',api:'openai-responses',base_url:baseUrl,model,model_options:{input:['text','image'],contextWindow:200000,maxTokens:32000,reasoning:true}}])
    assert.equal(adapted.getModel('gateway-'+id,model).maxTokens,32000)
    assert.deepEqual(adapted.getModel('gateway-'+id,model).input,['text','image'])
  } finally {await fs.rm(dir,{recursive:true,force:true})}
})

test('Gateway format compatibility preserves tools, input order and native hook additions', () => {
  const original = {model:'native-model', input:[{role:'developer', content:[{type:'input_text', text:'system prompt'}]},
    {role:'user', content:'hello'}, {type:'function_call_output', call_id:'call', output:'result'}], tools:[{type:'function', name:'read'}], stream:true}
  const adapted = instructionsPayload(original)
  assert.equal(adapted.instructions, 'system prompt')
  assert.deepEqual(adapted.input, original.input.slice(1))
  assert.deepEqual(adapted.tools, original.tools)
  assert.equal(original.input.length, 3)
  assert.equal(instructionsPayload({input:[]}).instructions, undefined)
})

test('native credential updates are serialized and failures do not overwrite working credentials', async () => {
  const writes = []
  const store = new VaultCredentialStore({provider:{type:'api_key',key:'old'}}, async (method, params) => {
    writes.push([method, params.credential?.key])
    if (params.credential?.key === 'bad') throw new Error('vault unavailable')
  })
  await Promise.all([store.modify('provider', () => ({type:'api_key',key:'one'})), store.modify('provider', credential => ({...credential,key:credential.key+'-two'}))])
  assert.equal((await store.read('provider')).key, 'one-two')
  await assert.rejects(store.modify('provider', () => ({type:'api_key',key:'bad'})))
  assert.equal((await store.read('provider')).key, 'one-two')
  await store.delete('provider')
  assert.equal(await store.read('provider'), undefined)
  assert.equal(writes.length, 4)
})

test('credential filtering preserves structured tool values without corrupting JSON', () => {
  const redact = secretRedactor({provider:{type:'api_key',key:'secret"with\\escapes'}})
  const value = {content:[{type:'text',text:'secret"with\\escapes'}], structuredContent:{total:2}, details:{ok:true}}
  const result = redact.value(value)
  assert.equal(result.content[0].text, '[凭据已隐藏]')
  assert.deepEqual(result.structuredContent, {total:2})
  assert.equal(value.content[0].text, 'secret"with\\escapes')
})
