#!/usr/bin/env python3
from __future__ import annotations
import json, math, time, urllib.parse, urllib.request
from pathlib import Path
from collections import deque

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
API = CFG["taxonomy"]["apiBase"].rstrip("/")
DATASET = CFG["taxonomy"]["datasetKey"]
MAX_DEPTH = CFG["limits"]["maxDepth"]
MAX_TAXA = CFG["limits"]["maxTaxa"]
MAX_CHILDREN = CFG["limits"]["maxChildrenPerTaxon"]
BOOSTS = CFG["weights"]["iconBoosts"]
HEADERS = {"User-Agent": "AtlasOfLifePrototype/0.6"}

def get_json(url, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except Exception:
            if attempt + 1 == retries: raise
            time.sleep(1.5 * (attempt + 1))

def normalize_usages(payload):
    found = []
    def walk(value):
        if isinstance(value, list):
            for item in value: walk(item)
        elif isinstance(value, dict):
            if "key" in value and ("scientificName" in value or "canonicalName" in value):
                found.append(value); return
            if "results" in value:
                walk(value["results"])
            else:
                for child in value.values():
                    if isinstance(child, (list, dict)): walk(child)
    walk(payload)
    out, seen = [], set()
    for u in found:
        marker = u.get("key")
        if marker is None:
            marker = (u.get("canonicalName"), u.get("scientificName"), u.get("rank"))
        if marker not in seen:
            seen.add(marker); out.append(u)
    return out

def roots():
    return normalize_usages(get_json(f"{API}/species/root/{urllib.parse.quote(DATASET)}"))

def children(key):
    out, offset = [], 0
    while True:
        page = get_json(f"{API}/species/{key}/children?limit=1000&offset={offset}")
        batch = normalize_usages(page)
        out.extend(batch)
        if not isinstance(page, dict) or page.get("endOfRecords", True) or not batch: break
        offset += len(batch)
    return out

def children_all(key):
    try: return normalize_usages(get_json(f"{API}/species/{key}/childrenAll"))
    except Exception: return []

def clean_usage(u):
    return {
        "id": str(u.get("key")), "key": u.get("key"),
        "parentId": str(u.get("parentKey")) if u.get("parentKey") is not None else None,
        "scientificName": u.get("scientificName") or u.get("canonicalName") or "Unnamed",
        "canonicalName": u.get("canonicalName") or u.get("scientificName") or "Unnamed",
        "rank": (u.get("rank") or "UNRANKED").upper(),
        "status": u.get("taxonomicStatus") or u.get("status") or "",
        "vernacularName": u.get("vernacularName")
    }

def priority(u):
    rs={"KINGDOM":8,"PHYLUM":7,"CLASS":6,"ORDER":5,"FAMILY":4,"GENUS":3,"SPECIES":2}
    rank=rs.get((u.get("rank") or "").upper(),1)
    name=u.get("canonicalName") or u.get("scientificName") or ""
    return rank*1000 + BOOSTS.get(name,0)*100

def main():
    root_usages=roots()
    print(f"Root endpoint yielded {len(root_usages)} taxon usages")
    wanted=set(CFG["taxonomy"]["roots"])
    selected=[u for u in root_usages if (u.get("canonicalName") or u.get("scientificName")) in wanted]
    if not selected:
        names=sorted((u.get("canonicalName") or u.get("scientificName") or "Unnamed") for u in root_usages)
        raise RuntimeError(f"Could not resolve configured roots. Available: {names[:40]}")
    print("Selected roots:", ", ".join(u.get("canonicalName") or u.get("scientificName") for u in selected))
    taxa=[]; seen=set(); queue=deque((u,0,None) for u in selected)
    while queue and len(taxa)<MAX_TAXA:
        u,depth,parent=queue.popleft(); key=u.get("key")
        if key is None or key in seen: continue
        seen.add(key); n=clean_usage(u)
        if parent is not None: n["parentId"]=str(parent)
        n["depth"]=depth
        proxy=len(children_all(key)) if depth<=4 else 0
        n["descendantProxy"]=proxy
        icon=float(BOOSTS.get(n["canonicalName"],1))
        n["popularityBoost"]=icon
        n["weight"]=max(CFG["weights"]["minWeight"],
            CFG["weights"]["base"]+math.pow(max(1,proxy),CFG["weights"]["descendantExponent"])+icon)
        taxa.append(n)
        if depth>=MAX_DEPTH: continue
        try: kids=children(key)
        except Exception as exc:
            print(f"WARN children {key}: {exc}"); continue
        kids=[k for k in kids if (k.get("taxonomicStatus") or k.get("status") or "").upper()
              not in {"SYNONYM","HETEROTYPIC_SYNONYM"}]
        kids.sort(key=priority,reverse=True)
        for child in kids[:MAX_CHILDREN]: queue.append((child,depth+1,key))
    payload={"meta":{"source":"Catalogue of Life eXtended Release via GBIF Species API",
        "datasetKey":DATASET,"generatedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "taxa":len(taxa),"note":"Bounded prototype subset."},"taxa":taxa}
    out=ROOT/"data"/"taxa.json"
    out.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    print(f"Wrote {len(taxa)} taxa to {out}")

if __name__=="__main__": main()
