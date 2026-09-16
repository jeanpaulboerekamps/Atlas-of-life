#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE=path.dirname(fileURLToPath(import.meta.url));
const ROOT=path.dirname(HERE);
const INPUT=path.resolve(ROOT,process.env.POINT_SHARDS_DIR||'point-artifacts');
const OUTPUT=path.join(ROOT,'data','points');
const STAGING=path.join(ROOT,'.points-merge');
const FAMILIES=path.join(STAGING,'families');

async function findIndexes(dir){
  const found=[];
  for(const entry of await fs.readdir(dir,{withFileTypes:true})){
    const full=path.join(dir,entry.name);
    if(entry.isDirectory())found.push(...await findIndexes(full));
    else if(entry.name==='family-index.json')found.push(full);
  }
  return found;
}
function add(target,name,ids){
  if(!target[name])target[name]=[];
  for(const id of ids||[])if(!target[name].includes(String(id)))target[name].push(String(id));
}

await fs.rm(STAGING,{recursive:true,force:true});
await fs.mkdir(FAMILIES,{recursive:true});
const indexes=(await findIndexes(INPUT)).sort();
if(!indexes.length)throw new Error(`No point shards found in ${INPUT}`);

const families={},genera={},copied=new Set();
let orders=0,familyCount=0,points=0;
for(const indexPath of indexes){
  const payload=JSON.parse(await fs.readFile(indexPath,'utf8'));
  for(const [name,ids] of Object.entries(payload.families||{}))add(families,name,ids);
  for(const [name,ids] of Object.entries(payload.genera||{}))add(genera,name,ids);
  orders+=Number(payload.totals?.orders||0);
  familyCount+=Number(payload.totals?.families||0);
  points+=Number(payload.totals?.points||0);
  const sourceFamilies=path.join(path.dirname(indexPath),'families');
  let entries=[];
  try{entries=await fs.readdir(sourceFamilies,{withFileTypes:true})}
  catch(err){if(err?.code!=='ENOENT')throw err}
  for(const entry of entries){
    if(!entry.isFile()||!entry.name.endsWith('.json'))continue;
    if(copied.has(entry.name))throw new Error(`Duplicate family file ${entry.name}`);
    copied.add(entry.name);
    await fs.copyFile(path.join(sourceFamilies,entry.name),path.join(FAMILIES,entry.name));
  }
}
for(const ids of Object.values(families))ids.sort();
for(const ids of Object.values(genera))ids.sort();
await fs.writeFile(path.join(STAGING,'family-index.json'),JSON.stringify({
  generatedAt:new Date().toISOString(),
  method:'Atlas v77.2 merged parallel coordinate export',
  sourceShards:indexes.length,
  families,genera,
  totals:{orders,families:familyCount,points}
}),'utf8');

await fs.rm(OUTPUT,{recursive:true,force:true});
await fs.mkdir(path.dirname(OUTPUT),{recursive:true});
await fs.rename(STAGING,OUTPUT);
console.log(`POINT MERGE OK · ${indexes.length} shards · ${orders} orders · ${points} species · ${copied.size} family files`);
