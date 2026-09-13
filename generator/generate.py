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
MAX_DEPTH=int(CFG["limits"]["maxDepth"])
MAX_TAXA=int(CFG["limits"]["maxTaxa"])
MAX_CHILDREN=int(CFG["limits"]["maxChildrenPerTaxon"])
BOOSTS=CFG["weights"]["iconBoosts"]

INAT="https://api.inaturalist.org/v1"
INAT_DELAY=1.05
ORDER_EXPONENT=math.log10(2.0)  # 10x observations => 2x area
HEADERS={"User-Agent":"AtlasOfLifePrototype/0.7"}
PREFERRED_ROOT_RANK={"Animalia":"KINGDOM","Plantae":"KINGDOM","Fungi":"KINGDOM","Bacteria":"DOMAIN"}
_last_inat=0.0

def get_json(url,retries=4,throttle_inat=False):
    global _last_inat
    if throttle_inat:
        elapsed=time.monotonic()-_last_inat
        if elapsed<INAT_DELAY:
            time.sleep(INAT_DELAY-elapsed)
    for attempt in range(retries):
        try:
            req=urllib.request.Request(url,headers=HEADERS)
            with urllib.request.urlopen(req,timeout=45) as r:
                data=json.load(r)
            if throttle_inat:
                _last_inat=time.monotonic()
            return data
        except Exception:
            if attempt+1==retries: raise
            time.sleep(1.5*(attempt+1))

def normalize_results(payload):
    if isinstance(payload,list): return [x for x in payload if isinstance(x,dict)]
    if isinstance(payload,dict):
        if isinstance(payload.get("results"),list): return [x for x in payload["results"] if isinstance(x,dict)]
        if "key" in payload: return [payload]
    return []

def resolve_root(name):
    q=urllib.parse.urlencode({"q":name,"datasetKey":DATASET,"limit":100})
    rows=normalize_results(get_json(f"{GBIF}/species/search?{q}"))
    exact=[r for r in rows if (r.get("canonicalName") or r.get("scientificName") or "").casefold()==name.casefold()]
    cands=exact or rows
    pref=PREFERRED_ROOT_RANK.get(name)
    ranked=[r for r in cands if (r.get("rank") or "").upper()==pref]
    if ranked: cands=ranked
    if not cands: raise RuntimeError(f"Cannot resolve root {name}")
    cands.sort(key=lambda r:(r.get("datasetKey")==DATASET,(r.get("taxonomicStatus") or r.get("status") or "").upper()=="ACCEPTED"),reverse=True)
    return cands[0]

def children(key):
    out=[]; offset=0
    while True:
        page=get_json(f"{GBIF}/species/{key}/children?limit=1000&offset={offset}")
        batch=normalize_results(page); out.extend(batch)
        if not isinstance(page,dict) or page.get("endOfRecords",True) or not batch: break
        offset+=len(batch)
    return out

def clean_usage(u):
    return {
      "id":str(u.get("key")),"key":u.get("key"),
      "parentId":str(u.get("parentKey")) if u.get("parentKey") is not None else None,
      "scientificName":u.get("scientificName") or u.get("canonicalName") or "Unnamed",
      "canonicalName":u.get("canonicalName") or u.get("scientificName") or "Unnamed",
      "rank":(u.get("rank") or "UNRANKED").upper(),
      "status":u.get("taxonomicStatus") or u.get("status") or "",
      "vernacularName":u.get("vernacularName")
    }

def child_priority(u):
    scores={"DOMAIN":10,"KINGDOM":9,"PHYLUM":8,"CLASS":7,"ORDER":6,"FAMILY":5,"GENUS":4,"SPECIES":3}
    rank=scores.get((u.get("rank") or "").upper(),1)
    name=u.get("canonicalName") or u.get("scientificName") or ""
    accepted=(u.get("taxonomicStatus") or u.get("status") or "").upper()=="ACCEPTED"
    return (1 if accepted else 0,rank,float(BOOSTS.get(name,0)))

def inat_resolve_order(name):
    q=urllib.parse.urlencode({"q":name,"rank":"order","is_active":"true","per_page":30})
    payload=get_json(f"{INAT}/taxa?{q}",throttle_inat=True)
    rows=payload.get("results",[]) if isinstance(payload,dict) else []
    exact=[r for r in rows if (r.get("name") or "").casefold()==name.casefold() and (r.get("rank") or "").casefold()=="order"]
    cands=exact or [r for r in rows if (r.get("rank") or "").casefold()=="order"]
    if not cands: return None
    cands.sort(key=lambda r:(bool(r.get("is_active",True)),(r.get("name") or "").casefold()==name.casefold()),reverse=True)
    return cands[0]

def inat_count(taxon_id):
    q=urllib.parse.urlencode({"taxon_id":taxon_id,"per_page":1})
    payload=get_json(f"{INAT}/observations?{q}",throttle_inat=True)
    return int(payload.get("total_results",0)) if isinstance(payload,dict) else 0

def main():
    roots=[resolve_root(n) for n in CFG["taxonomy"]["roots"]]
    print("Resolved roots:",[(r.get("canonicalName"),r.get("rank"),r.get("key")) for r in roots])

    taxa=[]; seen=set(); q=deque((r,0,None) for r in roots)
    while q and len(taxa)<MAX_TAXA:
        u,depth,parent=q.popleft(); key=u.get("key")
        if key is None or key in seen: continue
        seen.add(key); n=clean_usage(u)
        if parent is not None: n["parentId"]=str(parent)
        n["depth"]=depth; taxa.append(n)
        if depth>=MAX_DEPTH: continue
        try: kids=children(key)
        except Exception as exc:
            print("WARN children",key,exc); continue
        kids=[k for k in kids if (k.get("taxonomicStatus") or k.get("status") or "").upper() not in {"SYNONYM","HETEROTYPIC_SYNONYM"}]
        kids.sort(key=child_priority,reverse=True)
        for child in kids[:MAX_CHILDREN]:
            q.append((child,depth+1,key))

    by_id={n["id"]:n for n in taxa}
    kids=defaultdict(list)
    for n in taxa:
        if n["parentId"] in by_id: kids[n["parentId"]].append(n["id"])

    memo={}
    def desc(nid):
        if nid in memo:return memo[nid]
        memo[nid]=sum(1+desc(cid) for cid in kids.get(nid,[]))
        return memo[nid]

    for n in taxa:
        proxy=desc(n["id"]); n["descendantProxy"]=proxy
        icon=float(BOOSTS.get(n["canonicalName"],1))
        n["popularityBoost"]=icon
        n["weight"]=max(float(CFG["weights"]["minWeight"]),float(CFG["weights"]["base"])+math.pow(max(1,proxy),float(CFG["weights"]["descendantExponent"]))+icon)

    orders=[n for n in taxa if n["rank"]=="ORDER"]
    print(f"Enriching {len(orders)} orders with iNaturalist counts")
    matched=0
    for i,n in enumerate(orders,1):
        name=n["canonicalName"]
        try:
            it=inat_resolve_order(name)
            if not it:
                n.update({"inatTaxonId":None,"inatObservationCount":0,"inatAreaWeight":1.0})
                print(f"[iNat {i}/{len(orders)}] no match {name}")
                continue
            count=inat_count(int(it["id"]))
            area=math.pow(max(1,count),ORDER_EXPONENT)
            n.update({"inatTaxonId":int(it["id"]),"inatObservationCount":count,"inatAreaWeight":round(area,6),"inatMatchedName":it.get("name")})
            matched+=1
            print(f"[iNat {i}/{len(orders)}] {name}: {count:,} -> {area:.3f}")
        except Exception as exc:
            n.update({"inatTaxonId":None,"inatObservationCount":0,"inatAreaWeight":1.0,"inatError":str(exc)})
            print(f"[iNat {i}/{len(orders)}] WARN {name}: {exc}")
    print(f"Matched {matched}/{len(orders)} orders")

    def map_weight(nid,inside_order=False):
        n=by_id[nid]; rank=n["rank"]; child_ids=kids.get(nid,[])
        if rank=="ORDER":
            n["mapWeight"]=float(n.get("inatAreaWeight",1.0))
            for cid in child_ids: map_weight(cid,True)
            return n["mapWeight"]
        if inside_order:
            if child_ids:
                total=sum(map_weight(cid,True) for cid in child_ids)
                n["mapWeight"]=max(float(n["weight"]),total)
            else:n["mapWeight"]=float(n["weight"])
            return n["mapWeight"]
        if child_ids:
            n["mapWeight"]=max(1.0,sum(map_weight(cid,False) for cid in child_ids))
        else:n["mapWeight"]=float(n["weight"])
        return n["mapWeight"]

    root_ids=[n["id"] for n in taxa if not n["parentId"] or n["parentId"] not in by_id]
    for rid in root_ids: map_weight(rid)

    payload={"meta":{
      "source":"Catalogue of Life eXtended Release via GBIF Species API",
      "areaSource":"iNaturalist observation counts at ORDER rank",
      "areaFormula":"max(1, observation_count) ** log10(2)",
      "areaInterpretation":"10x observations = 2x order-level area",
      "generatedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
      "taxa":len(taxa),
      "roots":[{"name":r.get("canonicalName") or r.get("scientificName"),"rank":r.get("rank"),"key":r.get("key")} for r in roots]
    },"taxa":taxa}
    target=ROOT/"data"/"taxa.json"
    target.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    print(f"Wrote {len(taxa)} taxa to {target}")

if __name__=="__main__":main()
