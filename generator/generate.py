#!/usr/bin/env python3
"""
Atlas of Life v6 taxonomy generator.

Changes from v5:
- Resolves Animalia, Plantae, Fungi and Bacteria explicitly from COL XR.
- Avoids the expensive childrenAll request for every taxon.
- Computes sampled descendant counts after building the bounded tree.
- Keeps observations separate from taxonomy.
"""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from collections import deque, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

API = CFG["taxonomy"]["apiBase"].rstrip("/")
DATASET = CFG["taxonomy"]["datasetKey"]
MAX_DEPTH = int(CFG["limits"]["maxDepth"])
MAX_TAXA = int(CFG["limits"]["maxTaxa"])
MAX_CHILDREN = int(CFG["limits"]["maxChildrenPerTaxon"])
BOOSTS = CFG["weights"]["iconBoosts"]

HEADERS = {
    "User-Agent": "AtlasOfLifePrototype/0.6 (dataset-driven cartographic prototype)"
}

PREFERRED_ROOT_RANK = {
    "Animalia": "KINGDOM",
    "Plantae": "KINGDOM",
    "Fungi": "KINGDOM",
    "Bacteria": "DOMAIN",
}

def get_json(url: str, retries: int = 4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.load(response)
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1.2 * (attempt + 1))

def normalize_results(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return [x for x in payload["results"] if isinstance(x, dict)]
        if "key" in payload:
            return [payload]
    return []

def resolve_root(name: str):
    """
    Search the configured checklist directly. COL XR's true top-level roots
    include broad domains, so Animalia/Plantae/Fungi are not necessarily
    returned by /species/root/{datasetKey}.
    """
    query = urllib.parse.urlencode({
        "q": name,
        "datasetKey": DATASET,
        "limit": 100,
    })
    rows = normalize_results(get_json(f"{API}/species/search?{query}"))
    exact = [
        row for row in rows
        if (row.get("canonicalName") or row.get("scientificName") or "").casefold()
        == name.casefold()
    ]
    accepted = [
        row for row in exact
        if (row.get("taxonomicStatus") or row.get("status") or "").upper()
        in {"ACCEPTED", ""}
    ]
    candidates = accepted or exact or rows

    preferred = PREFERRED_ROOT_RANK.get(name)
    ranked = [
        row for row in candidates
        if (row.get("rank") or "").upper() == preferred
    ]
    if ranked:
        candidates = ranked

    if not candidates:
        raise RuntimeError(f"Could not resolve configured root taxon {name!r}.")

    # Prefer records actually belonging to the requested checklist.
    candidates.sort(
        key=lambda row: (
            row.get("datasetKey") == DATASET,
            (row.get("taxonomicStatus") or row.get("status") or "").upper() == "ACCEPTED",
        ),
        reverse=True,
    )
    return candidates[0]

def children(key):
    out = []
    offset = 0
    while True:
        url = f"{API}/species/{key}/children?limit=1000&offset={offset}"
        page = get_json(url)
        batch = normalize_results(page)
        out.extend(batch)
        if not isinstance(page, dict) or page.get("endOfRecords", True) or not batch:
            break
        offset += len(batch)
    return out

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

def child_priority(u):
    # Prefer accepted, named, conventional ranks in the bounded prototype.
    rank_score = {
        "DOMAIN": 10, "KINGDOM": 9, "PHYLUM": 8, "CLASS": 7, "ORDER": 6,
        "FAMILY": 5, "GENUS": 4, "SPECIES": 3,
    }.get((u.get("rank") or "").upper(), 1)
    name = u.get("canonicalName") or u.get("scientificName") or ""
    accepted = (u.get("taxonomicStatus") or u.get("status") or "").upper() == "ACCEPTED"
    return (1 if accepted else 0, rank_score, float(BOOSTS.get(name, 0)))

def main():
    root_names = CFG["taxonomy"]["roots"]
    selected_roots = [resolve_root(name) for name in root_names]

    print("Resolved roots:")
    for row in selected_roots:
        print(
            " -",
            row.get("canonicalName") or row.get("scientificName"),
            row.get("rank"),
            row.get("key"),
        )

    taxa = []
    seen = set()
    queue = deque((row, 0, None) for row in selected_roots)

    # Breadth-first traversal naturally gives all configured roots a chance
    # to expand before one large branch consumes the bounded prototype.
    while queue and len(taxa) < MAX_TAXA:
        u, depth, parent_override = queue.popleft()
        key = u.get("key")
        if key is None or key in seen:
            continue
        seen.add(key)

        node = clean_usage(u)
        if parent_override is not None:
            node["parentId"] = str(parent_override)
        node["depth"] = depth
        taxa.append(node)

        if depth >= MAX_DEPTH or len(taxa) >= MAX_TAXA:
            continue

        try:
            kids = children(key)
        except Exception as exc:
            print(f"WARN children {key}: {exc}")
            continue

        kids = [
            k for k in kids
            if (k.get("taxonomicStatus") or k.get("status") or "").upper()
            not in {"SYNONYM", "HETEROTYPIC_SYNONYM"}
        ]
        kids.sort(key=child_priority, reverse=True)

        for child in kids[:MAX_CHILDREN]:
            queue.append((child, depth + 1, key))

    # Compute descendant counts inside the sampled tree, with zero extra API calls.
    by_id = {n["id"]: n for n in taxa}
    kids_by_parent = defaultdict(list)
    for node in taxa:
        if node["parentId"] in by_id:
            kids_by_parent[node["parentId"]].append(node["id"])

    descendant_count = {}
    def count_desc(node_id):
        if node_id in descendant_count:
            return descendant_count[node_id]
        total = 0
        for child_id in kids_by_parent.get(node_id, []):
            total += 1 + count_desc(child_id)
        descendant_count[node_id] = total
        return total

    for node in taxa:
        proxy = count_desc(node["id"])
        node["descendantProxy"] = proxy
        icon = float(BOOSTS.get(node["canonicalName"], 1))
        node["popularityBoost"] = icon
        node["weight"] = max(
            float(CFG["weights"]["minWeight"]),
            float(CFG["weights"]["base"])
            + math.pow(max(1, proxy), float(CFG["weights"]["descendantExponent"]))
            + icon,
        )

    payload = {
        "meta": {
            "source": "Catalogue of Life eXtended Release via GBIF Species API",
            "datasetKey": DATASET,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "taxa": len(taxa),
            "roots": [
                {
                    "name": r.get("canonicalName") or r.get("scientificName"),
                    "rank": r.get("rank"),
                    "key": r.get("key"),
                }
                for r in selected_roots
            ],
            "note": "Bounded multi-root prototype; weights use sampled descendants plus icon boosts.",
        },
        "taxa": taxa,
    }

    target = ROOT / "data" / "taxa.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {len(taxa)} taxa to {target}")

if __name__ == "__main__":
    main()
