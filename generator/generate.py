#!/usr/bin/env python3
"""
Atlas of Life v6 taxonomy generator — ChecklistBank/COL XR edition.

Why this version exists:
- GBIF's legacy /v1/species search/children endpoints are not the right way to
  browse the current Catalogue of Life Extended Release (COL XR).
- COL XR uses Catalogue of Life identifiers and is directly browsable through
  the ChecklistBank tree API.
- This generator resolves configured roots in ChecklistBank and walks only
  direct children, bounded by maxDepth, maxTaxa and maxChildrenPerTaxon.
"""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# Keep the GBIF checklist UUID in output metadata because the rest of the app
# may use it for occurrence queries/maps. Taxonomy traversal itself uses CLB.
GBIF_CHECKLIST_KEY = CFG["taxonomy"]["datasetKey"]

# ChecklistBank alias for the latest monthly Catalogue of Life Extended Release.
# You may optionally add taxonomy.checklistBankDataset to config.json to pin
# a specific release/dataset later.
CLB_DATASET = CFG["taxonomy"].get("checklistBankDataset", "3LXR")
CLB_API = "https://api.checklistbank.org"

MAX_DEPTH = int(CFG["limits"]["maxDepth"])
MAX_TAXA = int(CFG["limits"]["maxTaxa"])
MAX_CHILDREN = int(CFG["limits"]["maxChildrenPerTaxon"])
BOOSTS = CFG["weights"]["iconBoosts"]

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "AtlasOfLifePrototype/0.6 (COL XR via ChecklistBank)",
}

ALLOWED_ROOT_RANKS = {
    "Animalia": {"KINGDOM"},
    "Plantae": {"KINGDOM"},
    "Fungi": {"KINGDOM"},
    # Checklist releases may express this high-level group differently.
    "Bacteria": {"DOMAIN", "SUPERKINGDOM", "KINGDOM"},
}


def get_json(url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            print(f"GET {url} (attempt {attempt + 1}/{retries})", flush=True)
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.load(response)
        except Exception as exc:
            print(f"WARN request failed: {exc}", flush=True)
            if attempt + 1 == retries:
                raise
            time.sleep(1.5 * (attempt + 1))


def as_rows(payload):
    """Normalize common ChecklistBank list/page response shapes."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in ("result", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

        # Some single-record endpoints return a record directly.
        if any(k in payload for k in ("id", "key", "name", "label")):
            return [payload]

    return []


def row_id(row):
    value = row.get("id")
    if value is None:
        value = row.get("key")
    return str(value) if value is not None else None


def row_name(row):
    # ChecklistBank tree nodes commonly expose label; usage records commonly
    # expose scientificName/name. Handle all of them defensively.
    value = (
        row.get("canonicalName")
        or row.get("scientificName")
        or row.get("label")
        or row.get("name")
    )
    if isinstance(value, dict):
        value = value.get("scientificName") or value.get("name") or value.get("label")
    return str(value) if value else "Unnamed"


def row_rank(row):
    value = row.get("rank") or row.get("taxonRank") or "UNRANKED"
    if isinstance(value, dict):
        value = value.get("name") or value.get("label") or "UNRANKED"
    return str(value).upper()


def resolve_root(name: str):
    params = urllib.parse.urlencode({
        "q": name,
        "type": "exact",
        "limit": 100,
    })
    url = f"{CLB_API}/dataset/{urllib.parse.quote(str(CLB_DATASET))}/nameusage/search?{params}"
    rows = as_rows(get_json(url))

    exact = [r for r in rows if row_name(r).casefold() == name.casefold()]
    allowed = ALLOWED_ROOT_RANKS.get(name, {"KINGDOM"})
    ranked = [r for r in exact if row_rank(r) in allowed]

    if not ranked:
        ranks = sorted({row_rank(r) for r in exact})
        raise RuntimeError(
            f"Could not resolve COL XR root {name!r} at rank(s) {sorted(allowed)}. "
            f"Exact-name ranks returned: {ranks or ['none']}."
        )

    # Prefer an accepted taxon if status is present. Tree traversal itself only
    # uses accepted taxa, so this mainly disambiguates search results.
    def score(row):
        status = str(row.get("status") or row.get("taxonomicStatus") or "").upper()
        accepted = status in {"ACCEPTED", ""}
        return (accepted, -len(row_name(row)))

    ranked.sort(key=score, reverse=True)
    root = ranked[0]

    if row_id(root) is None:
        raise RuntimeError(f"Resolved root {name!r} has no ChecklistBank taxon id.")

    return root


def children(taxon_id: str):
    # Tree API returns direct accepted children only. No pagination through
    # thousands of irrelevant rows is needed for this bounded prototype.
    url = (
        f"{CLB_API}/dataset/{urllib.parse.quote(str(CLB_DATASET))}"
        f"/tree/{urllib.parse.quote(str(taxon_id))}/children"
        "?insertPlaceholder=false"
    )
    rows = as_rows(get_json(url))
    return rows[:MAX_CHILDREN]


def clean_node(row, parent_id, depth):
    taxon_id = row_id(row)
    name = row_name(row)
    return {
        "id": taxon_id,
        "key": taxon_id,
        "parentId": str(parent_id) if parent_id is not None else None,
        "scientificName": name,
        "canonicalName": name,
        "rank": row_rank(row),
        "status": str(row.get("status") or "ACCEPTED").upper(),
        "vernacularName": row.get("vernacularName"),
        "depth": depth,
    }


def child_priority(row):
    rank_score = {
        "DOMAIN": 10,
        "SUPERKINGDOM": 10,
        "KINGDOM": 9,
        "PHYLUM": 8,
        "CLASS": 7,
        "ORDER": 6,
        "FAMILY": 5,
        "GENUS": 4,
        "SPECIES": 3,
    }.get(row_rank(row), 1)

    name = row_name(row)
    return (rank_score, float(BOOSTS.get(name, 0)))


def main():
    print(f"ChecklistBank dataset: {CLB_DATASET}", flush=True)

    selected_roots = []
    for name in CFG["taxonomy"]["roots"]:
        print(f"Resolving root: {name}", flush=True)
        root = resolve_root(name)
        print(
            f"Resolved {name}: {row_name(root)} "
            f"[{row_rank(root)}] id={row_id(root)}",
            flush=True,
        )
        selected_roots.append(root)

    taxa = []
    seen = set()
    queue = deque((root, 0, None) for root in selected_roots)

    while queue and len(taxa) < MAX_TAXA:
        row, depth, parent_id = queue.popleft()
        taxon_id = row_id(row)

        if taxon_id is None or taxon_id in seen:
            continue

        seen.add(taxon_id)
        taxa.append(clean_node(row, parent_id, depth))

        if len(taxa) % 100 == 0:
            print(
                f"Progress: {len(taxa)}/{MAX_TAXA} taxa; queue={len(queue)}",
                flush=True,
            )

        if depth >= MAX_DEPTH or len(taxa) >= MAX_TAXA:
            continue

        try:
            kids = children(taxon_id)
        except Exception as exc:
            print(f"WARN children {taxon_id}: {exc}", flush=True)
            continue

        kids.sort(key=child_priority, reverse=True)

        for child in kids[:MAX_CHILDREN]:
            queue.append((child, depth + 1, taxon_id))

    # Compute descendant counts entirely inside the sampled bounded tree.
    by_id = {node["id"]: node for node in taxa}
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
            "source": "Catalogue of Life Extended Release via ChecklistBank tree API",
            "datasetKey": GBIF_CHECKLIST_KEY,
            "checklistBankDataset": CLB_DATASET,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "taxa": len(taxa),
            "roots": [
                {
                    "name": row_name(root),
                    "rank": row_rank(root),
                    "key": row_id(root),
                }
                for root in selected_roots
            ],
            "note": (
                "Bounded multi-root COL XR prototype; taxonomy is traversed "
                "through ChecklistBank and weights use sampled descendants "
                "plus icon boosts."
            ),
        },
        "taxa": taxa,
    }

    target = ROOT / "data" / "taxa.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(f"Wrote {len(taxa)} taxa to {target}", flush=True)


if __name__ == "__main__":
    main()
