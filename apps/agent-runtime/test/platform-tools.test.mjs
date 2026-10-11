import assert from 'node:assert/strict'
import test from 'node:test'
import {createMorningExtension} from '../morning-extension.mjs'

test('native web proposals retain their required nullable fields', async()=>{
  const tools=new Map(), calls=[]
  createMorningExtension({request:async(method,params)=>{calls.push(params);return {draft:{task_id:'watch-test'}}}}, {type:'object'}, true)({registerTool:tool=>tools.set(tool.name,tool)})
  const proposal={action_goal:null,semantic_condition:null,confidence:.95,value:null}
  await tools.get('morning_prepare_watch').execute('t',{inspection_id:'one',proposal})
  assert.deepEqual(calls[0].args.proposal,proposal)
  assert.ok(tools.has('morning_watches'))
})

test('computer parameters survive strict Gateway filling without unrelated action fields', async()=>{
  const tools=new Map(), calls=[]
  createMorningExtension({request:async(method,params)=>{calls.push(params);return {image:{mimeType:'image/png',data:'image-data'},id:'observed'}}}, null, true)({registerTool:tool=>tools.set(tool.name,tool)})
  const result=await tools.get('morning_computer').execute('t',{operation:'propose_action',device_id:'local',app:'com.apple.calculator',method:'click',observation_id:'obs',goal:'Click the observed element',display:null,region:null,arguments_json:'{"element_index":"21"}'})
  assert.deepEqual(calls[0].args.arguments,{element_index:'21'})
  assert.equal(calls[0].args.display,undefined)
  assert.equal(calls[0].args.arguments_json,undefined)
  assert.equal(result.content[1].type,'image')
  assert.ok(!result.content[0].text.includes('image-data'))
  assert.equal(result.details.image,undefined)
})
