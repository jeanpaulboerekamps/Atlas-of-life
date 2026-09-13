#!/usr/bin/env python3
from __future__ import annotations
import json, math, time, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
CFG=json.loads((ROOT/"config.json").read_text(encoding="utf-8"))
GBIF=CFG["taxonomy"]["apiBase"].rstrip("/")
DATASET=CFG["taxonomy"]["datasetKey"]
INAT="https://api.inaturalist.org/v1"
EXP=math.log10(2.0)
HEADERS={"User-Agent":"AtlasOfLifePrototype/1.0"}
ROOT_NAMES=["Animalia","Plantae","Fungi","Bacteria"]
PREFERRED={"Animalia":"KINGDOM","Plantae":"KINGDOM","Fungi":"KINGDOM","Bacteria":"DOMAIN"}
_last_inat=0.0

def get_json(url,retries=4,inat=False):
    global _last_inat
    if inat:
        dt=time.monotonic()-_last_inat
        if dt<1.05:
            time.sleep(1.05-dt)
    for attempt in range(retries):
        try:
            req=urllib.request.Request(url,headers=HEADERS)
            with urllib.request.urlopen(req,timeout=45) as r:
                data=json.load(r)
            if inat:_last_inat=time.monotonic()
            return data
        except Exception:
            if attempt+1==retries: raise
            time.sleep(1.5*(attempt+1))

def results(x):
    if isinstance(x,list):
        return [r for r in x if isinstance(r,dict)]
    if isinstance(x,dict):
        if isinstance(x.get("results"),list):
            return [r for r in x["results"] if isinstance(r,dict)]
        if "key" in x:
            return [x]
    return []

def root_endpoint():
    return results(get_json(f"{GBIF}/species/root/{urllib.parse.quote(DATASET)}"))

def resolve_root(name):
    exact=[r for r in root_endpoint()
           if (r.get("canonicalName") or r.get("scientificName") or "").casefold()==name.casefold()]
    if exact:
        return exact[0]
    q=urllib.parse.urlencode({"q":name,"datasetKey":DATASET,"limit":100})
    rs=results(get_json(f"{GBIF}/species/search?{q}"))
    exact=[r for r in rs if (r.get("canonicalName") or r.get("scientificName") or "").casefold()==name.casefold()]
    ranked=[r for r in exact if (r.get("rank") or "").upper()==PREFERRED[name]]
    if ranked:
        return ranked[0]
    if exact:
        return exact[0]
    raise RuntimeError(f"Cannot resolve root {name}")

def search_orders(root_key):
    out=[];offset=0
    while True:
        params=urllib.parse.urlencode({
            "rank":"ORDER",
            "higherTaxonKey":root_key,
            "datasetKey":DATASET,
            "limit":1000,
            "offset":offset
        })
        page=get_json(f"{GBIF}/species/search?{params}")
        batch=results(page)
        out.extend(batch)
        print(f"GBIF root {root_key}: +{len(batch)} orders at offset {offset}",flush=True)
        if not isinstance(page,dict) or page.get("endOfRecords",True) or not batch:
            break
        offset+=len(batch)
    uniq={}
    for r in out:
        status=(r.get("taxonomicStatus") or r.get("status") or "").upper()
        if status in {"SYNONYM","HETEROTYPIC_SYNONYM"}:
            continue
        key=r.get("key")
        if key is not None:
            uniq[str(key)]=r
    return list(uniq.values())

def prior_inat_counts():
    p=ROOT/"data"/"taxa.json"
    if not p.exists():
        return {}
    try:
        old=json.loads(p.read_text(encoding="utf-8"))
        return {
            n["canonicalName"]:int(n["inatObservationCount"])
            for n in old.get("taxa",[])
            if n.get("rank")=="ORDER"
            and n.get("canonicalName")
            and n.get("inatObservationCount") is not None
        }
    except Exception:
        return {}

def inat_count_by_name(name):
    params=urllib.parse.urlencode({"taxon_name":name,"per_page":1})
    payload=get_json(f"{INAT}/observations?{params}",inat=True)
    return int(payload.get("total_results",0)) if isinstance(payload,dict) else 0

def add_node(nodes,node_id,parent_id,name,rank,key=None):
    if node_id in nodes:
        return
    nodes[node_id]={
        "id":node_id,
        "key":key,
        "parentId":parent_id,
        "scientificName":name,
        "canonicalName":name,
        "rank":rank,
        "status":"ACCEPTED"
    }

def main():
    roots=[resolve_root(n) for n in ROOT_NAMES]
    print("Resolved roots:",
          [(r.get("canonicalName"),r.get("rank"),r.get("key")) for r in roots],
          flush=True)

    previous=prior_inat_counts()
    print("Cached iNaturalist order counts:",len(previous),flush=True)

    nodes={}
    order_nodes=[]

    for root in roots:
        rname=root.get("canonicalName") or root.get("scientificName")
        rid=str(root.get("key"))
        add_node(nodes,rid,None,rname,(root.get("rank") or PREFERRED[rname]).upper(),root.get("key"))

        orders=search_orders(root.get("key"))
        print(rname,"direct orders:",len(orders),flush=True)

        for row in orders:
            order_name=row.get("canonicalName") or row.get("scientificName") or "Unnamed"
            order_key=row.get("key")
            if order_key is None:
                continue
            parent_id=rid

            phylum_key=row.get("phylumKey")
            phylum_name=row.get("phylum")
            if phylum_key and phylum_name:
                pid=str(phylum_key)
                add_node(nodes,pid,rid,phylum_name,"PHYLUM",phylum_key)
                parent_id=pid

            class_key=row.get("classKey")
            class_name=row.get("class")
            if class_key and class_name:
                cid=str(class_key)
                add_node(nodes,cid,parent_id,class_name,"CLASS",class_key)
                parent_id=cid

            oid=str(order_key)
            add_node(nodes,oid,parent_id,order_name,"ORDER",order_key)
            order_nodes.append(nodes[oid])

    seen=set();orders=[]
    for n in order_nodes:
        if n["id"] not in seen:
            seen.add(n["id"]);orders.append(n)

    print("Unique orders:",len(orders),flush=True)

    reused=queried=0
    for i,n in enumerate(orders,1):
        name=n["canonicalName"]
        if name in previous:
            count=previous[name]
            reused+=1
            source="cache"
        else:
            try:
                count=inat_count_by_name(name)
            except Exception as exc:
                print("WARN iNat",name,exc,flush=True)
                count=0
            queried+=1
            source="api"

        weight=math.pow(max(1,count),EXP) if count>0 else 0.0
        n["inatObservationCount"]=count
        n["inatAreaWeight"]=weight
        n["mapWeight"]=max(.01,weight)

        if source=="api" or i%25==0:
            print(f"[{i}/{len(orders)}] {name}: {count:,} -> {weight:.3f} ({source})",flush=True)

    print("iNat reused:",reused,"queried:",queried,flush=True)

    taxa=list(nodes.values())
    by={n["id"]:n for n in taxa}
    kids=defaultdict(list)
    for n in taxa:
        if n["parentId"] in by:
            kids[n["parentId"]].append(n["id"])

    memo={}
    def signal(nid):
        if nid in memo:
            return memo[nid]
        n=by[nid]
        if n["rank"]=="ORDER":
            v=float(n.get("mapWeight",0.01))
        else:
            v=sum(signal(c) for c in kids.get(nid,[]))
        memo[nid]=v
        return v

    for n in taxa:
        if n["rank"]!="ORDER":
            n["mapWeight"]=max(.01,signal(n["id"]))

    root_ids=[str(r.get("key")) for r in roots]
    summary=[]
    for rid in root_ids:
        r=by[rid]
        stack=[rid];orders_n=raw=0
        while stack:
            nid=stack.pop();node=by[nid]
            if node["rank"]=="ORDER":
                orders_n+=1
                raw+=int(node.get("inatObservationCount",0))
            stack.extend(kids.get(nid,[]))
        summary.append({
            "name":r["canonicalName"],
            "orders":orders_n,
            "rawObservations":raw,
            "mapWeight":r["mapWeight"]
        })

    print("ROOT SUMMARY",summary,flush=True)

    payload={"meta":{
        "source":"COL XR via GBIF direct ORDER search",
        "areaSource":"iNaturalist taxon_name observation counts at ORDER rank",
        "areaFormula":"N ** log10(2); 10x observations = 2x area",
        "generatedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "taxa":len(taxa),
        "orders":len(orders),
        "rootSummary":summary,
        "roots":[{"name":r.get("canonicalName"),"rank":r.get("rank"),"key":r.get("key")} for r in roots]
    },"taxa":taxa}

    (ROOT/"data"/"taxa.json").write_text(
        json.dumps(payload,ensure_ascii=False,separators=(",",":")),
        encoding="utf-8"
    )
    print("Wrote",len(taxa),"nodes",len(orders),"orders",flush=True)

if __name__=="__main__":
    main()
