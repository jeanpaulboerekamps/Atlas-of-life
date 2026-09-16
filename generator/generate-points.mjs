#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const HERE=path.dirname(fileURLToPath(import.meta.url));
const ROOT=path.dirname(HERE);
const BASE_URL=process.env.ATLAS_EXPORT_URL||'http://127.0.0.1:8765/';
const WORKERS=Math.max(1,Number(process.env.POINT_EXPORT_WORKERS||4));
const BATCH_SIZE=Math.max(1,Number(process.env.POINT_EXPORT_BATCH||6));
const taxaPayload=JSON.parse(await fs.readFile(path.join(ROOT,'data','taxa.json'),'utf8'));
const orders=taxaPayload.taxa.filter(t=>String(t.rank||'').toUpperCase()==='ORDER'&&t.detailAvailable!==false);
const staging=path.join(ROOT,'.point-staging');
const familiesDir=path.join(staging,'families');
await fs.rm(staging,{recursive:true,force:true});
await fs.mkdir(familiesDir,{recursive:true});

const browser=await chromium.launch({headless:true});
const familyIndex={};
const genusIndex={};
let next=0,completed=0,totalPoints=0,totalFamilies=0;

function safeId(value){return String(value).replace(/[^A-Za-z0-9._-]/g,'_')}
function addFamilyName(name,id){
  if(!name)return;
  if(!familyIndex[name])familyIndex[name]=[];
  if(!familyIndex[name].includes(id))familyIndex[name].push(id);
}
function addGenusName(name,id){
  if(!name)return;
  if(!genusIndex[name])genusIndex[name]=[];
  if(!genusIndex[name].includes(id))genusIndex[name].push(id);
}

async function exportOrder(workerId){
  const context=await browser.newContext();
  let page=null,used=0;
  try{
    while(next<orders.length){
      const order=orders[next++];
      if(!page||used>=BATCH_SIZE){
        if(page)await page.close();
        page=await context.newPage();
        page.setDefaultTimeout(0);
        await page.goto(BASE_URL,{waitUntil:'networkidle',timeout:0});
        await page.waitForFunction(()=>typeof window.atlasExportOrderPoints==='function',{timeout:0});
        used=0;
      }
      used++;
      const result=await page.evaluate(id=>window.atlasExportOrderPoints(id),String(order.id));
      for(const family of result.families||[]){
        const id=String(family.id);
        addFamilyName(String(family.name||''),id);
        for(const point of family.points||[])
          addGenusName(String(point[0]||'').split(/\s+/)[0],id);
        await fs.writeFile(
          path.join(familiesDir,`${safeId(id)}.json`),
          JSON.stringify({familyId:id,familyName:family.name,points:family.points}),
          'utf8'
        );
        totalFamilies++;
        totalPoints+=(family.points||[]).length;
      }
      completed++;
      console.log(`[${completed}/${orders.length}] worker ${workerId} · ${result.orderName} · ${(result.families||[]).length} families`);
    }
  }finally{
    if(page)await page.close();
    await context.close();
  }
}

try{
  await Promise.all(Array.from({length:WORKERS},(_,i)=>exportOrder(i+1)));
}finally{
  await browser.close();
}

for(const ids of Object.values(familyIndex))ids.sort();
for(const ids of Object.values(genusIndex))ids.sort();
await fs.writeFile(path.join(staging,'family-index.json'),JSON.stringify({
  generatedAt:new Date().toISOString(),
  method:'Atlas v77.1 batched browser layout export',
  families:familyIndex,
  genera:genusIndex,
  totals:{orders:orders.length,families:totalFamilies,points:totalPoints}
}),'utf8');

const destination=path.join(ROOT,'data','points');
await fs.rm(destination,{recursive:true,force:true});
await fs.rename(staging,destination);
console.log(`POINT EXPORT OK · ${totalPoints} species · ${totalFamilies} family shards`);
