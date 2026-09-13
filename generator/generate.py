#!/usr/bin/env python3
from __future__ import annotations
import json, math, time, urllib.parse, urllib.request
from collections import deque, defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
CFG=json.loads((ROOT/"config.json").read_text(encoding="utf-8"))
GBIF=CFG["taxonomy"]["apiBase"].rstrip("/")
DATASET=CFG["taxonomy"]["datasetKey"]
MAX_TAXA=int(CFG["limits"]["maxTaxa"])
MAX_CHILDREN=int(CFG["limits"]["maxChildrenPerTaxon"])
INAT="https://api.inaturalist.org/v1"
EXP=math.log10(2.0)
HEADERS={"User-Agent":"AtlasOfLifePrototype/0.9"}
ROOT_NAMES=["Animalia","Plantae","Fungi","Bacteria"]
PREFERRED={"Animalia":"KINGDOM","Plantae":"KINGDOM","Fungi":"KINGDOM","Bacteria":"DOMAIN"}
_last=0.0

def get_json(url,retries=4,inat=False):
    global _last
    if inat:
        dt=time.monotonic()-_last
        if dt<1.05: time.sleep(1.05-dt)
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

def build_to_order(root):
    rootname=root.get("canonicalName") or root.get("scientificName")
    taxa=[];seen=set();q=deque([(root,0,None)])
    while q:
        u,depth,parent=q.popleft(); key=u.get("key")
        if key is None or key in seen: continue
        seen.add(key); n=clean(u,parent,depth,rootname); taxa.append(n)
        if n["rank"]=="ORDER": continue
        if depth>10: continue
        try:kids=children(key)
        except Exception as e:
            print("WARN children",key,e,flush=True);continue
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

    taxa=[]
    for r in roots:
        part=build_to_order(r)
        print(r.get("canonicalName"),"skeleton",len(part),"orders",sum(n["rank"]=="ORDER" for n in part),flush=True)
        taxa.extend(part)

    unique=[];seen=set()
    for n in taxa:
        if n["id"] not in seen:
            seen.add(n["id"]);unique.append(n)
    taxa=unique
    orders=[n for n in taxa if n["rank"]=="ORDER"]

    for i,n in enumerate(orders,1):
        try:
            it=inat_order(n["canonicalName"])
            if not it:
                n.update(inatTaxonId=None,inatObservationCount=0,inatAreaWeight=0.0)
                continue
            c=obs_count(int(it["id"])); w=math.pow(max(1,c),EXP)
            n.update(inatTaxonId=int(it["id"]),inatObservationCount=c,inatAreaWeight=w)
            print(f"[{i}/{len(orders)}] {n['canonicalName']}: {c:,} -> {w:.3f}",flush=True)
        except Exception as e:
            n.update(inatTaxonId=None,inatObservationCount=0,inatAreaWeight=0.0)
            print("WARN iNat",n["canonicalName"],e,flush=True)

    by={n["id"]:n for n in taxa}
    kids=defaultdict(list)
    for n in taxa:
        if n["parentId"] in by:kids[n["parentId"]].append(n["id"])

    memo={}
    def signal(nid):
        if nid in memo:return memo[nid]
        n=by[nid]
        v=float(n.get("inatAreaWeight",0.0)) if n["rank"]=="ORDER" else sum(signal(c) for c in kids.get(nid,[]))
        memo[nid]=v; return v

    for n in taxa:
        if n["rank"]=="ORDER":n["mapWeight"]=max(.01,float(n.get("inatAreaWeight",0.0)))
        else:n["mapWeight"]=max(.01,signal(n["id"]))

    root_ids=[n["id"] for n in taxa if not n["parentId"] or n["parentId"] not in by]
    summary=[]
    for rid in root_ids:
        r=by[rid]; stack=[rid];num=match=raw=0
        while stack:
            nid=stack.pop(); n=by[nid]
            if n["rank"]=="ORDER":
                num+=1;raw+=int(n.get("inatObservationCount",0));match+=1 if n.get("inatTaxonId") else 0
            stack.extend(kids.get(nid,[]))
        summary.append({"name":r["canonicalName"],"orders":num,"matchedOrders":match,"rawObservations":raw,"mapWeight":r["mapWeight"]})
    print("ROOT SUMMARY",summary,flush=True)

    payload={"meta":{"source":"COL XR via GBIF","areaSource":"iNaturalist at ORDER rank",
      "areaFormula":"N ** log10(2)","sampling":"complete skeleton to ORDER before any deeper detail",
      "generatedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"taxa":len(taxa),"rootSummary":summary,
      "roots":[{"name":r.get("canonicalName"),"rank":r.get("rank"),"key":r.get("key")} for r in roots]},
      "taxa":taxa}
    (ROOT/"data"/"taxa.json").write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    print("Wrote",len(taxa),"taxa",flush=True)

if __name__=="__main__":main()
