#!/usr/bin/env python3
from __future__ import annotations
import json, math, time, urllib.parse, urllib.request
from collections import deque, defaultdict
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
CFG=json.loads((ROOT/"config.json").read_text(encoding="utf-8"))
GBIF=CFG["taxonomy"]["apiBase"].rstrip("/")
DATASET=CFG["taxonomy"]["datasetKey"]
MAX_TAXA=int(CFG["limits"]["maxTaxa"])
MAX_DEPTH=int(CFG["limits"]["maxDepth"])
MAX_CHILDREN=int(CFG["limits"]["maxChildrenPerTaxon"])
INAT="https://api.inaturalist.org/v1"
INAT_DELAY=1.05
EXP=math.log10(2.0)
HEADERS={"User-Agent":"AtlasOfLifePrototype/0.8"}
ROOT_NAMES=["Animalia","Plantae","Fungi","Bacteria"]
PREFERRED={"Animalia":"KINGDOM","Plantae":"KINGDOM","Fungi":"KINGDOM","Bacteria":"DOMAIN"}
_last=0.0

def get_json(url,retries=4,inat=False):
    global _last
    if inat:
        dt=time.monotonic()-_last
        if dt<INAT_DELAY: time.sleep(INAT_DELAY-dt)
    for i in range(retries):
        try:
            req=urllib.request.Request(url,headers=HEADERS)
            with urllib.request.urlopen(req,timeout=45) as r: data=json.load(r)
            if inat:_last=time.monotonic()
            return data
        except Exception:
            if i+1==retries: raise
            time.sleep(1.5*(i+1))

def rows(x):
    if isinstance(x,list): return [r for r in x if isinstance(r,dict)]
    if isinstance(x,dict):
        if isinstance(x.get("results"),list): return [r for r in x["results"] if isinstance(r,dict)]
        if "key" in x:return [x]
    return []

def root_endpoint():
    return rows(get_json(f"{GBIF}/species/root/{urllib.parse.quote(DATASET)}"))

def resolve_root(name):
    exact=[r for r in root_endpoint() if (r.get("canonicalName") or r.get("scientificName") or "").casefold()==name.casefold()]
    if exact:return exact[0]
    q=urllib.parse.urlencode({"q":name,"datasetKey":DATASET,"limit":100})
    rs=rows(get_json(f"{GBIF}/species/search?{q}"))
    exact=[r for r in rs if (r.get("canonicalName") or r.get("scientificName") or "").casefold()==name.casefold()]
    ranked=[r for r in exact if (r.get("rank") or "").upper()==PREFERRED[name]]
    if ranked:return ranked[0]
    if exact:return exact[0]
    raise RuntimeError(f"Cannot resolve root {name}")

def children(key):
    out=[];offset=0
    while True:
        page=get_json(f"{GBIF}/species/{key}/children?limit=1000&offset={offset}")
        batch=rows(page);out.extend(batch)
        if not isinstance(page,dict) or page.get("endOfRecords",True) or not batch:break
        offset+=len(batch)
    return [k for k in out if (k.get("taxonomicStatus") or k.get("status") or "").upper() not in {"SYNONYM","HETEROTYPIC_SYNONYM"}]

def clean(u,parent=None,depth=0,rootname=None):
    return {"id":str(u.get("key")),"key":u.get("key"),
      "parentId":str(parent) if parent is not None else (str(u.get("parentKey")) if u.get("parentKey") is not None else None),
      "scientificName":u.get("scientificName") or u.get("canonicalName") or "Unnamed",
      "canonicalName":u.get("canonicalName") or u.get("scientificName") or "Unnamed",
      "rank":(u.get("rank") or "UNRANKED").upper(),"status":u.get("taxonomicStatus") or u.get("status") or "",
      "depth":depth,"rootName":rootname}

def build_root(root,quota):
    taxa=[];seen=set();q=deque([(root,0,None)])
    rootname=root.get("canonicalName") or root.get("scientificName")
    while q and len(taxa)<quota:
        u,depth,parent=q.popleft();key=u.get("key")
        if key is None or key in seen:continue
        seen.add(key);n=clean(u,parent,depth,rootname);taxa.append(n)
        if depth>=MAX_DEPTH:continue
        try:kids=children(key)
        except Exception as e:
            print("WARN children",key,e,flush=True);continue
        if n["rank"] in {"ORDER","FAMILY","GENUS","SPECIES"}:
            kids=kids[:MAX_CHILDREN]
        for c in kids:q.append((c,depth+1,key))
    return taxa

def inat_order(name):
    q=urllib.parse.urlencode({"q":name,"rank":"order","is_active":"true","per_page":30})
    rs=get_json(f"{INAT}/taxa?{q}",inat=True).get("results",[])
    exact=[r for r in rs if (r.get("name") or "").casefold()==name.casefold() and (r.get("rank") or "").casefold()=="order"]
    return exact[0] if exact else None

def obs_count(tid):
    q=urllib.parse.urlencode({"taxon_id":tid,"per_page":1})
    return int(get_json(f"{INAT}/observations?{q}",inat=True).get("total_results",0))

def main():
    roots=[resolve_root(n) for n in ROOT_NAMES]
    print("Resolved:",[(r.get("canonicalName"),r.get("rank"),r.get("key")) for r in roots],flush=True)
    quota=max(100,MAX_TAXA//len(roots))
    taxa=[]
    for r in roots:
        part=build_root(r,quota)
        print(r.get("canonicalName"),"sampled",len(part),"taxa",flush=True)
        taxa.extend(part)
    taxa=taxa[:MAX_TAXA]

    by={n["id"]:n for n in taxa};kids=defaultdict(list)
    for n in taxa:
        if n["parentId"] in by:kids[n["parentId"]].append(n["id"])

    orders=[n for n in taxa if n["rank"]=="ORDER"]
    print("Orders:",len(orders),flush=True)
    for i,n in enumerate(orders,1):
        try:
            it=inat_order(n["canonicalName"])
            if not it:
                n.update(inatTaxonId=None,inatObservationCount=0,inatAreaWeight=0.0)
                print(f"[{i}/{len(orders)}] no exact iNat match {n['canonicalName']}",flush=True)
                continue
            c=obs_count(int(it["id"]));w=math.pow(max(1,c),EXP)
            n.update(inatTaxonId=int(it["id"]),inatObservationCount=c,inatAreaWeight=w)
            print(f"[{i}/{len(orders)}] {n['canonicalName']}: {c:,} -> {w:.3f}",flush=True)
        except Exception as e:
            n.update(inatTaxonId=None,inatObservationCount=0,inatAreaWeight=0.0)
            print("WARN iNat",n["canonicalName"],e,flush=True)

    memo={}
    def propagated(nid):
        if nid in memo:return memo[nid]
        n=by[nid]
        if n["rank"]=="ORDER":
            v=float(n.get("inatAreaWeight",0.0))
        else:
            v=sum(propagated(c) for c in kids.get(nid,[]))
        memo[nid]=v
        return v

    def internal(nid):
        n=by[nid];cs=kids.get(nid,[])
        if not cs:n["mapWeight"]=1.0;return 1.0
        total=sum(internal(c) for c in cs)
        n["mapWeight"]=max(1.0,total);return n["mapWeight"]

    for n in orders:
        for c in kids.get(n["id"],[]):internal(c)
        n["mapWeight"]=max(.01,float(n.get("inatAreaWeight",0.0)))

    root_ids=[n["id"] for n in taxa if not n["parentId"] or n["parentId"] not in by]
    for rid in root_ids:
        stack=[rid]
        while stack:
            nid=stack.pop();node=by[nid]
            if node["rank"]!="ORDER":node["mapWeight"]=max(.01,propagated(nid))
            stack.extend(kids.get(nid,[]))

    payload={"meta":{
      "source":"COL XR via GBIF","areaSource":"iNaturalist ORDER observation counts",
      "areaFormula":"N ** log10(2); 10x observations = 2x area",
      "sampling":"balanced per root; no high-rank child truncation",
      "generatedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"taxa":len(taxa),
      "roots":[{"name":r.get("canonicalName"),"rank":r.get("rank"),"key":r.get("key")} for r in roots]},
      "taxa":taxa}
    (ROOT/"data"/"taxa.json").write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    print("Root weights:",[(by[rid]["canonicalName"],by[rid]["mapWeight"]) for rid in root_ids],flush=True)

if __name__=="__main__":main()
