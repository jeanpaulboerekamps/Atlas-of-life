#!/usr/bin/env python3
"""
Atlas of Life taxonomy generator.

Fetches a bounded real taxonomic tree from the GBIF Species API, using the
Catalogue of Life eXtended Release (COL XR) checklist UUID configured in
config.json.

No third-party packages are required.
"""
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

HEADERS = {"User-Agent": "AtlasOfLifePrototype/0.5 (GitHub Pages research prototype)"}

def get_json(url, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1.5 * (attempt + 1))

def roots():
    # Official GBIF species API endpoint for checklist root usages.
    return get_json(f"{API}/species/root/{urllib.parse.quote(DATASET)}")

def children(key):
    out, offset = [], 0
    while True:
        url = f"{API}/species/{key}/children?limit=1000&offset={offset}"
        page = get_json(url)
        batch = page.get("results", [])
        out.extend(batch)
        if page.get("endOfRecords", True) or not batch:
            break
        offset += len(batch)
    return out

def children_all(key):
    try:
        return get_json(f"{API}/species/{key}/childrenAll")
    except Exception:
        return []

def clean_usage(u):
    return {
        "id": str(u.get("key")),
        "key": u.get("key"),
        "parentId": str(u.get("parentKey")) if u.get("parentKey") is not None else None,
        "scientificName": u.get("scientificName") or u.get("canonicalName") or "Unnamed",
        "canonicalName": u.get("canonicalName") or u.get("scientificName") or "Unnamed",
        "rank": (u.get("rank") or "UNRANKED").upper(),
        "status": u.get("taxonomicStatus") or u.get("status") or "",
        "vernacularName": u.get("vernacularName"),
    }

def descendant_proxy(key):
    """
    childrenAll returns brief counts/child usages; we use its size/count fields only
    as a layout proxy, not as a statement of total species richness.
    """
    rows = children_all(key)
    total = 0
    for r in rows:
        for fld in ("count", "numDescendants", "species", "usages"):
            v = r.get(fld)
            if isinstance(v, (int, float)):
                total += max(0, v)
                break
        else:
            total += 1
    return total

def priority(u):
    rank = (u.get("rank") or "").upper()
    rank_score = {
        "KINGDOM": 8, "PHYLUM": 7, "CLASS": 6, "ORDER": 5,
        "FAMILY": 4, "GENUS": 3, "SPECIES": 2
    }.get(rank, 1)
    name = u.get("canonicalName") or u.get("scientificName") or ""
    return rank_score * 1000 + BOOSTS.get(name, 0) * 100

def main():
    root_usages = roots()
    wanted = set(CFG["taxonomy"]["roots"])
    selected_roots = []
    for u in root_usages:
        name = u.get("canonicalName") or u.get("scientificName")
        if name in wanted:
            selected_roots.append(u)

    if not selected_roots:
        raise RuntimeError("Could not resolve configured root taxa in COL XR.")

    taxa = []
    seen = set()
    queue = deque((u, 0, None) for u in selected_roots)

    while queue and len(taxa) < MAX_TAXA:
        u, depth, parent_override = queue.popleft()
        key = u.get("key")
        if key is None or key in seen:
            continue
        seen.add(key)
        n = clean_usage(u)
        if parent_override is not None:
            n["parentId"] = str(parent_override)
        n["depth"] = depth
        proxy = descendant_proxy(key) if depth <= 4 else 0
        n["descendantProxy"] = proxy
        name = n["canonicalName"]
        icon = float(BOOSTS.get(name, 1))
        n["popularityBoost"] = icon
        n["weight"] = max(
            CFG["weights"]["minWeight"],
            CFG["weights"]["base"] + math.pow(max(1, proxy), CFG["weights"]["descendantExponent"]) + icon
        )
        taxa.append(n)

        if depth >= MAX_DEPTH or len(taxa) >= MAX_TAXA:
            continue
        try:
            kids = children(key)
        except Exception as exc:
            print(f"WARN children {key}: {exc}")
            continue

        # Keep the prototype bounded while preserving broad high ranks.
        kids = [k for k in kids if (k.get("taxonomicStatus") or "").upper() not in {"SYNONYM", "HETEROTYPIC_SYNONYM"}]
        kids.sort(key=priority, reverse=True)
        for child in kids[:MAX_CHILDREN]:
            queue.append((child, depth + 1, key))

    payload = {
        "meta": {
            "source": "Catalogue of Life eXtended Release via GBIF Species API",
            "datasetKey": DATASET,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "taxa": len(taxa),
            "note": "Bounded prototype subset; max children/depth are configured in config.json."
        },
        "taxa": taxa
    }
    out = ROOT / "data" / "taxa.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(taxa)} taxa to {out}")

if __name__ == "__main__":
    main()
