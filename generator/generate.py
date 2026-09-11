#!/usr/bin/env python3
"""
Atlas of Life v6 taxonomy generator — Catalogue of Life via ChecklistBank.

This version follows the current ChecklistBank API shapes used by the official
CatalogueOfLife/rcol client:

- Resolve names with:
    /dataset/3LXR/match/nameusage
- Browse direct children with:
    /dataset/3LXR/tree/{id}/children

The alias 3LXR always refers to the latest monthly Catalogue of Life Extended
Release. The traversal remains bounded by maxDepth, maxTaxa and
maxChildrenPerTaxon from config.json.
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

CLB_API = "https://api.checklistbank.org"
CLB_DATASET = CFG["taxonomy"].get("checklistBankDataset", "3LXR")
GBIF_DATASET = CFG["taxonomy"]["datasetKey"]

MAX_DEPTH = int(CFG["limits"]["maxDepth"])
MAX_TAXA = int(CFG["limits"]["maxTaxa"])
MAX_CHILDREN = int(CFG["limits"]["maxChildrenPerTaxon"])
BOOSTS = CFG["weights"]["iconBoosts"]

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "AtlasOfLifePrototype/0.6 (Catalogue of Life via ChecklistBank)",
}

# Rank candidates are tried in order. This avoids accepting a low-rank homonym
# such as a genus named "Bacteria".
ROOT_RANKS = {
    "Animalia": ("kingdom",),
    "Plantae": ("kingdom",),
    "Fungi": ("kingdom",),
    "Bacteria": ("domain", "superkingdom", "kingdom"),
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


def records(payload):
    """Normalize ChecklistBank paged and plain-list responses."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        value = payload.get("result")
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

        # Some endpoints may use results/items.
        for key in ("results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

    return []


def node_id(row):
    value = row.get("id")
    return str(value) if value is not None else None


def node_name(row):
    value = row.get("name") or row.get("label") or "Unnamed"
    if isinstance(value, dict):
        value = value.get("scientificName") or value.get("name") or value.get("label")
    return str(value) if value else "Unnamed"


def node_rank(row):
    return str(row.get("rank") or "UNRANKED").upper()


def node_status(row):
    return str(row.get("status") or "").upper()


def resolve_root(name: str):
    """
    Resolve a configured root with ChecklistBank's dedicated name matcher.

    The matcher returns:
      {"usage": {"id", "name", "rank", "status", ...}, ...}
    """
    for rank in ROOT_RANKS.get(name, ("kingdom",)):
        params = urllib.parse.urlencode({
            "q": name,
            "rank": rank,
            "verbose": "false",
        })
        url = (
            f"{CLB_API}/dataset/{urllib.parse.quote(str(CLB_DATASET), safe='')}"
            f"/match/nameusage?{params}"
        )
        payload = get_json(url)
        usage = payload.get("usage") if isinstance(payload, dict) else None

        if not isinstance(usage, dict):
            continue

        resolved_name = node_name(usage)
        resolved_rank = node_rank(usage)
        resolved_status = node_status(usage)

        if (
            resolved_name.casefold() == name.casefold()
            and resolved_rank == rank.upper()
            and resolved_status in {"ACCEPTED", ""}
            and node_id(usage) is not None
        ):
            return usage

        print(
            f"Matcher candidate for {name}: "
            f"{resolved_name} [{resolved_rank}] status={resolved_status or 'n/a'}",
            flush=True,
        )

    raise RuntimeError(
        f"Could not resolve configured root {name!r} at allowed rank(s) "
        f"{ROOT_RANKS.get(name)} in ChecklistBank dataset {CLB_DATASET}."
    )


def children(parent_key: str):
    """
    Fetch only the first bounded page of direct accepted children.

    ChecklistBank's tree endpoint is paginated. We intentionally request no more
    than MAX_CHILDREN because the prototype cannot use additional children.
    """
    limit = max(1, MAX_CHILDREN)
    params = urllib.parse.urlencode({
        "limit": limit,
        "offset": 0,
        "extinct": "true",
        "insertPlaceholder": "false",
    })
    url = (
        f"{CLB_API}/dataset/{urllib.parse.quote(str(CLB_DATASET), safe='')}"
        f"/tree/{urllib.parse.quote(str(parent_key), safe='')}/children?{params}"
    )
    return records(get_json(url))[:MAX_CHILDREN]


def clean_node(row, parent_override, depth):
    key = node_id(row)
    name = node_name(row)

    return {
        "id": str(key),
        "key": key,
        "parentId": str(parent_override) if parent_override is not None else None,
        "scientificName": name,
        "canonicalName": name,
        "rank": node_rank(row),
        "status": node_status(row) or "ACCEPTED",
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
    }.get(node_rank(row), 1)

    name = node_name(row)
    return (rank_score, float(BOOSTS.get(name, 0)))


def main():
    print(f"ChecklistBank dataset: {CLB_DATASET}", flush=True)

    selected_roots = []
    for name in CFG["taxonomy"]["roots"]:
        print(f"Resolving root: {name}", flush=True)
        root = resolve_root(name)
        print(
            f"Resolved {name}: {node_name(root)} "
            f"[{node_rank(root)}] id={node_id(root)}",
            flush=True,
        )
        selected_roots.append(root)

    taxa = []
    seen = set()
    queue = deque((root, 0, None) for root in selected_roots)

    while queue and len(taxa) < MAX_TAXA:
        row, depth, parent_override = queue.popleft()
        key = node_id(row)

        if key is None or key in seen:
            continue

        seen.add(key)
        taxa.append(clean_node(row, parent_override, depth))

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

        kids.sort(key=child_priority, reverse=True)

        for child in kids[:MAX_CHILDREN]:
            queue.append((child, depth + 1, key))

    # Descendant counts are calculated entirely inside the sampled tree.
    by_id = {node["id"]: node for node in taxa}
    kids_by_parent = defaultdict(list)

    for node in taxa:
        if node["parentId"] in by_id:
            kids_by_parent[node["parentId"]].append(node["id"])

    descendant_count = {}

    def count_desc(node_id_):
        if node_id_ in descendant_count:
            return descendant_count[node_id_]

        total = 0
        for child_id in kids_by_parent.get(node_id_, []):
            total += 1 + count_desc(child_id)

        descendant_count[node_id_] = total
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
            "source": "Catalogue of Life Extended Release via ChecklistBank API",
            "datasetKey": GBIF_DATASET,
            "checklistBankDataset": CLB_DATASET,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "taxa": len(taxa),
            "roots": [
                {
                    "name": node_name(root),
                    "rank": node_rank(root),
                    "key": node_id(root),
                }
                for root in selected_roots
            ],
            "note": (
                "Bounded multi-root COL XR prototype; direct children are "
                "traversed with ChecklistBank and weights use sampled "
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
