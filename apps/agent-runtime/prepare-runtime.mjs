// Pi's published CLI shrinkwrap currently overrides npm's root override on disk.
// Point that one transitive dependency at the explicitly pinned, patched root package.
import fs from 'node:fs/promises'
import path from 'node:path'
import {fileURLToPath} from 'node:url'

const root=path.dirname(fileURLToPath(import.meta.url))
const patched=path.join(root,'node_modules/brace-expansion')
const metadata=JSON.parse(await fs.readFile(path.join(patched,'package.json'),'utf8'))
if(metadata.version!=='5.0.12')throw new Error('Expected the pinned patched brace-expansion 5.0.12')
const nested=path.join(root,'node_modules/@earendil-works/pi-coding-agent/node_modules/brace-expansion')
const lockPath=path.join(root,'package-lock.json')
const lock=JSON.parse(await fs.readFile(lockPath,'utf8'))
const patchedEntry=lock.packages['node_modules/brace-expansion']
if(!patchedEntry || patchedEntry.version!=='5.0.12')throw new Error('Patched dependency is not pinned in the root lockfile')
for(const[key,value] of Object.entries(lock.packages)){
  if(key.endsWith('/brace-expansion'))Object.assign(value,{version:patchedEntry.version,resolved:patchedEntry.resolved,integrity:patchedEntry.integrity})
}
await fs.writeFile(lockPath,JSON.stringify(lock,null,2)+'\n')
try{
  const current=JSON.parse(await fs.readFile(path.join(nested,'package.json'),'utf8'))
  if(current.version==='5.0.12')process.exit(0)
}catch{}
await fs.rm(nested,{recursive:true,force:true})
await fs.symlink(path.relative(path.dirname(nested),patched),nested,'dir')
process.stdout.write('Pi runtime dependency patch applied\n')
