import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import {createWorkspaceTools} from '../workspace-tools.mjs'

const setup = async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(),'morning-tools-test-'))
  const workspace = await createWorkspaceTools(root)
  return {root,workspace,tool:name=>workspace.tools.find(t=>t.name===name)}
}

test('Pi tools read, edit and write actual files; existing writes need prior reads',async()=>{
  const {root,workspace,tool}=await setup()
  await fs.writeFile(path.join(root,'a.txt'),'alpha\n')
  await assert.rejects(()=>tool('write').execute('w',{path:'a.txt',content:'bad'}),/Read the existing file/)
  await tool('read').execute('r',{path:'a.txt'})
  await tool('edit').execute('e',{path:'a.txt',edits:[{oldText:'alpha',newText:'beta'}]})
  assert.equal(await fs.readFile(path.join(root,'a.txt'),'utf8'),'beta\n')
  await tool('write').execute('w2',{path:'nested/new.txt',content:'created'})
  assert.equal(await fs.readFile(path.join(root,'nested/new.txt'),'utf8'),'created')
  assert.equal(workspace.backups.length,1)
})

test('Pi exact edits reject ambiguous text and preserve the file',async()=>{
  const {root,tool}=await setup()
  await fs.writeFile(path.join(root,'a.txt'),'x\nx\n')
  await assert.rejects(()=>tool('edit').execute('e',{path:'a.txt',edits:[{oldText:'x',newText:'y'}]}))
  assert.equal(await fs.readFile(path.join(root,'a.txt'),'utf8'),'x\nx\n')
})

test('Workspace and credential guards include symlink targets',async()=>{
  const {root,tool}=await setup()
  const external=await fs.mkdtemp(path.join(os.tmpdir(),'morning-private-test-'))
  await fs.writeFile(path.join(external,'private.txt'),'private fixture')
  await fs.symlink(path.join(external,'private.txt'),path.join(root,'escape.txt'))
  await assert.rejects(()=>tool('read').execute('r',{path:'escape.txt'}),/Symlink escapes/)
  await fs.mkdir(path.join(root,'.secrets'));await fs.writeFile(path.join(root,'.secrets/key'),'test credential')
  await fs.symlink(path.join(root,'.secrets/key'),path.join(root,'alias.txt'))
  await assert.rejects(()=>tool('read').execute('r',{path:'alias.txt'}),/protected/)
})

test('Isolated bash runs code and blocks access outside the workspace', {skip:process.platform!=='darwin'},async()=>{
  const {root,tool}=await setup()
  await fs.writeFile(path.join(root,'verify.py'),'from pathlib import Path\nassert Path("a.txt").read_text()=="beta"\nprint("verified beta")\n')
  await fs.writeFile(path.join(root,'a.txt'),'beta')
  const result=await tool('bash').execute('b',{command:'python3 verify.py',timeout:15})
  assert.equal(result.isError,undefined,JSON.stringify(result))
  assert.match(result.content[0].text,/verified beta/)
  const external=await fs.mkdtemp(path.join(os.tmpdir(),'morning-external-test-'))
  await fs.writeFile(path.join(external,'private.txt'),'private fixture')
  const denied=await tool('bash').execute('b2',{command:`cat ${JSON.stringify(path.join(external,'private.txt'))}`,timeout:5})
  assert.equal(denied.isError,true)
  assert.doesNotMatch(denied.content[0].text,/private fixture/)
  const secrets=await tool('bash').execute('b3',{command:'printf "%s" "$OPENAI_API_KEY"',timeout:5})
  assert.doesNotMatch(secrets.content[0].text,/sk-/)
  await fs.mkdir(path.join(root,'nested/.secrets'),{recursive:true})
  await fs.writeFile(path.join(root,'nested/.secrets/key'),'nested credential fixture')
  await fs.writeFile(path.join(root,'nested/.env.local'),'nested environment fixture')
  const nested=await tool('bash').execute('b4',{command:'cat nested/.secrets/key nested/.env.local',timeout:5})
  assert.equal(nested.isError,true)
  assert.doesNotMatch(nested.content[0].text,/nested (credential|environment) fixture/)
})

test('Isolated bash timeout also stops commands that ignore TERM', {skip:process.platform!=='darwin'},async()=>{
  const {tool}=await setup()
  const start=Date.now()
  const result=await tool('bash').execute('timeout',{command:"trap '' TERM; sleep 20",timeout:1})
  assert.equal(result.isError,true)
  assert.ok(Date.now()-start<6000,'Command ignored the execution deadline')
})
