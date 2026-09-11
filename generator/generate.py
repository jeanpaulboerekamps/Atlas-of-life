#!/usr/bin/env python3
"""
Atlas of Life v6 taxonomy generator — GBIF Species API v2 / COL XR.

This version uses GBIF's v2 taxonomy service, which is backed by ChecklistBank.
Taxon identifiers are checklist-scoped strings, and the configured GBIF dataset
UUID selects the Catalogue of Life Extended Release (COL XR).

Key properties:
- resolves each configured root at an explicitly allowed high rank;
- traverses only direct accepted children;
- fetches at most maxChildrenPerTaxon children for each node;
- computes descendant counts locally (no expensive descendant API calls);
- emits immediate progress logs for GitHub Actions.
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

DATASET = CFG["taxonomy"]["datasetKey"]
API = "https://api.gbif.org/v2"

MAX_DEPTH = int(CFG["limits"]["maxDepth"])
MAX_TAXA = int(CFG["limits"]["maxTaxa"])
MAX_CHILDREN = int(CFG["limits"]["maxChildrenPerTaxon"])
BOOSTS = CFG["weights"]["iconBoosts"]

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "AtlasOfLifePrototype/0.6 (GBIF Species API v2 / COL XR)",
}

# Try ranks in this order.  This prevents a low-rank homonym such as a genus
# called "Bacteria" from ever being selected as a configured root.
ROOT_RANKS = {
    "Animalia": ("KINGDOM",),
    "Plantae": ("KINGDOM",),
    "Fungi": ("KINGDOM",),
    "Bacteria": ("DOMAIN", "SUPERKINGDOM", "KINGDOM"),
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


def page_results(payload):
    if isinstance(payload, dict):
        for key in ("results", "result", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def taxon_id(row):
    value = row.get("taxonID")
    if value is None:
        value = row.get("key")
    if value is None:
        value = row.get("id")
    return str(value) if value is not None else None


def taxon_name(row):
    value = row.get("scientificName") or row.get("canonicalName") or row.get("label")
    return str(value) if value else "Unnamed"


def taxon_rank(row):
    value = row.get("taxonRank") or row.get("rank") or "UNRANKED"
    return str(value).upper()


def taxon_status(row):
    value = row.get("taxonomicStatus") or row.get("status") or ""
    return str(value).upper()


def parent_id(row):
    value = row.get("parentNameUsageID")
    if value is None:
        value = row.get("parentId")
    return str(value) if value is not None else None


def resolve_root(name: str):
    ranks = ROOT_RANKS.get(name, ("KINGDOM",))

    for rank in ranks:
        params = urllib.parse.urlencode(
            {
                "q": name,
                "taxonRank": rank,
                "limit": 100,
            }
        )
        url = (
            f"{API}/taxon/search/{urllib.parse.quote(str(DATASET), safe='')}"
            f"?{params}"
        )
        rows = page_results(get_json(url))

        exact = [
            row
            for row in rows
            if taxon_name(row).casefold() == name.casefold()
            and taxon_rank(row) == rank
        ]

        # Prefer accepted records.  If status is absent, the v2 search record
        # is still usable, so an empty status is accepted as a fallback.
        accepted = [
            row
            for row in exact
            if taxon_status(row) in {"ACCEPTED", ""}
        ]
        candidates = accepted or exact

        if candidates:
            root = candidates[0]
            if taxon_id(root) is None:
                raise RuntimeError(
                    f"Resolved {name!r} at rank {rank}, but response has no taxonID."
                )
            return root

    raise RuntimeError(
        f"Could not resolve configured root {name!r} "
        f"at any allowed rank {ranks} in dataset {DATASET}."
    )


def children(key: str):
    # GBIF v2 tree endpoint returns direct accepted children and is paginated.
    # We only request as many as the bounded prototype can use.
    limit = max(1, MAX_CHILDREN)
    params = urllib.parse.urlencode({"limit": limit, "offset": 0})
    url = (
        f"{API}/taxon/tree/{urllib.parse.quote(str(DATASET), safe='')}/"
        f"{urllib.parse.quote(str(key), safe='')}/children?{params}"
    )
    rows = page_results(get_json(url))
    return rows[:MAX_CHILDREN]


def clean_usage(row, parent_override=None, depth=0):
    key = taxon_id(row)
    parent = str(parent_override) if parent_override is not None else parent_id(row)
    name = taxon_name(row)

    return {
        "id": str(key),
        "key": key,
        "parentId": parent,
        "scientificName": name,
        "canonicalName": name,
        "rank": taxon_rank(row),
        "status": taxon_status(row) or "ACCEPTED",
        "vernacularName": None,
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
    }.get(taxon_rank(row), 1)

    name = taxon_name(row)
    accepted = taxon_status(row) in {"ACCEPTED", ""}
    return (1 if accepted else 0, rank_score, float(BOOSTS.get(name, 0)))


def main():
    print(f"GBIF Species API v2 dataset: {DATASET}", flush=True)

    selected_roots = []
    for name in CFG["taxonomy"]["roots"]:
        print(f"Resolving root: {name}", flush=True)
        root = resolve_root(name)
        print(
            f"Resolved {name}: {taxon_name(root)} "
            f"[{taxon_rank(root)}] id={taxon_id(root)}",
            flush=True,
        )
        selected_roots.append(root)

    taxa = []
    seen = set()
    queue = deque((root, 0, None) for root in selected_roots)

    while queue and len(taxa) < MAX_TAXA:
        row, depth, parent_override = queue.popleft()
        key = taxon_id(row)

        if key is None or key in seen:
            continue

        seen.add(key)
        taxa.append(clean_usage(row, parent_override, depth))

        if len(taxa) % 100 == 0:
            print(
                f"Progress: {len(taxa)}/{MAX_TAXA} taxa; queue={len(queue)}",
                flush=True,
            )

        if depth >= MAX_DEPTH or len(taxa) >= MAX_TAXA:
            continue

        try:
            kids = children(key)
        except Exception as exc:
            print(f"WARN children {key}: {exc}", flush=True)
            continue

        kids = [
            child
            for child in kids
            if taxon_status(child)
            not in {"SYNONYM", "HETEROTYPIC_SYNONYM", "HOMOTYPIC_SYNONYM"}
        ]
        kids.sort(key=child_priority, reverse=True)

        for child in kids[:MAX_CHILDREN]:
            queue.append((child, depth + 1, key))

    # Count descendants only within the bounded sampled tree.
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
            + math.pow(
                max(1, proxy),
                float(CFG["weights"]["descendantExponent"]),
            )
            + icon,
        )

    payload = {
        "meta": {
            "source": "Catalogue of Life Extended Release via GBIF Species API v2",
            "datasetKey": DATASET,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "taxa": len(taxa),
            "roots": [
                {
                    "name": taxon_name(root),
                    "rank": taxon_rank(root),
                    "key": taxon_id(root),
                }
                for root in selected_roots
            ],
            "note": (
                "Bounded multi-root COL XR prototype; taxonomy is traversed "
                "through GBIF Species API v2 and weights use sampled "
                "descendants plus icon boosts."
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
