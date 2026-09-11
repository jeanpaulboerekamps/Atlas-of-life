#!/usr/bin/env python3
"""
Atlas of Life v6 taxonomy generator.

Fast bounded traversal:
- Resolves configured roots from the configured COL/GBIF dataset.
- Rejects misleading low-rank exact-name matches (e.g. a genus named Bacteria).
- Fetches only enough child pages to fill the configured per-taxon bound.
- Computes descendant counts locally, with no childrenAll calls.
- Emits unbuffered progress logs suitable for GitHub Actions.
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

API = CFG["taxonomy"]["apiBase"].rstrip("/")
DATASET = CFG["taxonomy"]["datasetKey"]
MAX_DEPTH = int(CFG["limits"]["maxDepth"])
MAX_TAXA = int(CFG["limits"]["maxTaxa"])
MAX_CHILDREN = int(CFG["limits"]["maxChildrenPerTaxon"])
BOOSTS = CFG["weights"]["iconBoosts"]

HEADERS = {
    "User-Agent": "AtlasOfLifePrototype/0.6 (dataset-driven cartographic prototype)"
}

# Bacteria is represented differently by different checklist releases.
# DOMAIN is preferred, but KINGDOM is also acceptable; never accept a low-rank
# homonym such as a genus called "Bacteria".
ALLOWED_ROOT_RANKS = {
    "Animalia": ("KINGDOM",),
    "Plantae": ("KINGDOM",),
    "Fungi": ("KINGDOM",),
    "Bacteria": ("DOMAIN", "KINGDOM"),
}

PAGE_SIZE = min(1000, max(1, MAX_CHILDREN))


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


def normalize_results(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return [x for x in payload["results"] if isinstance(x, dict)]
        if "key" in payload:
            return [payload]
    return []


def accepted(row):
    return (row.get("taxonomicStatus") or row.get("status") or "").upper() in {
        "ACCEPTED", ""
    }


def resolve_root(name: str):
    allowed_ranks = ALLOWED_ROOT_RANKS.get(name, ("KINGDOM",))

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
    ranked = [
        row for row in exact
        if (row.get("rank") or "").upper() in allowed_ranks
    ]
    ranked_accepted = [row for row in ranked if accepted(row)]

    candidates = ranked_accepted or ranked
    if not candidates:
        ranks = sorted({
            (row.get("rank") or "UNRANKED").upper()
            for row in exact
        })
        raise RuntimeError(
            f"Could not resolve root {name!r} at rank(s) {allowed_ranks}. "
            f"Exact-name ranks returned: {ranks or ['none']}."
        )

    # Prefer the requested dataset, then the earlier allowed rank.
    rank_order = {rank: i for i, rank in enumerate(allowed_ranks)}
    candidates.sort(
        key=lambda row: (
            row.get("datasetKey") == DATASET,
            accepted(row),
            -rank_order.get((row.get("rank") or "").upper(), 999),
        ),
        reverse=True,
    )
    return candidates[0]


def children(key):
    """Fetch only enough direct children to satisfy the configured bound."""
    out = []
    offset = 0

    while len(out) < MAX_CHILDREN:
        remaining = MAX_CHILDREN - len(out)
        limit = min(PAGE_SIZE, remaining)
        url = f"{API}/species/{key}/children?limit={limit}&offset={offset}"
        page = get_json(url)
        batch = normalize_results(page)

        if not batch:
            break

        out.extend(batch)

        if not isinstance(page, dict) or page.get("endOfRecords", True):
            break

        offset += len(batch)

    return out[:MAX_CHILDREN]


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
    rank_score = {
        "DOMAIN": 10, "KINGDOM": 9, "PHYLUM": 8, "CLASS": 7, "ORDER": 6,
        "FAMILY": 5, "GENUS": 4, "SPECIES": 3,
    }.get((u.get("rank") or "").upper(), 1)
    name = u.get("canonicalName") or u.get("scientificName") or ""
    is_accepted = accepted(u)
    return (1 if is_accepted else 0, rank_score, float(BOOSTS.get(name, 0)))


def main():
    selected_roots = []

    for name in CFG["taxonomy"]["roots"]:
        print(f"Resolving root: {name}", flush=True)
        root = resolve_root(name)
        print(
            f"Resolved {name}: "
            f"{root.get('canonicalName') or root.get('scientificName')} "
            f"[{root.get('rank')}] key={root.get('key')}",
            flush=True,
        )
        selected_roots.append(root)

    taxa = []
    seen = set()
    queue = deque((row, 0, None) for row in selected_roots)

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
            k for k in kids
            if (k.get("taxonomicStatus") or k.get("status") or "").upper()
            not in {"SYNONYM", "HETEROTYPIC_SYNONYM"}
        ]
        kids.sort(key=child_priority, reverse=True)

        for child in kids[:MAX_CHILDREN]:
            queue.append((child, depth + 1, key))

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
    print(f"Wrote {len(taxa)} taxa to {target}", flush=True)


if __name__ == "__main__":
    main()
