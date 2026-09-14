#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import unicodedata
import urllib.request
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
API = CFG["taxonomy"]["apiBase"].rstrip("/")
DATASET = CFG["taxonomy"]["datasetKey"]

HEADERS = {
    "User-Agent": "AtlasOfLife/0.42 taxonomic detail generator"
}

ACCEPTED_STATUSES = {
    "", "ACCEPTED", "PROVISIONALLY_ACCEPTED", "DOUBTFUL"
}

DETAIL_RANKS = {"FAMILY", "GENUS", "SPECIES"}


def get_json(url: str, retries: int = 5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(2 * (attempt + 1))


def normalize_usages(payload):
    found = []

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            if "key" in value and ("scientificName" in value or "canonicalName" in value):
                found.append(value)
                return
            if "results" in value:
                walk(value["results"])
            else:
                for child in value.values():
                    if isinstance(child, (list, dict)):
                        walk(child)

    walk(payload)

    out = []
    seen = set()
    for u in found:
        marker = str(u.get("key"))
        if marker not in seen:
            seen.add(marker)
            out.append(u)
    return out


def safe_id(value) -> str:
    s = str(value)
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)


def norm_name(value: str | None) -> str:
    s = unicodedata.normalize("NFD", str(value or "")).lower()
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip()


def search_prefix(value: str | None) -> str:
    s = "".join(c for c in norm_name(value) if c.isalnum())
    return (s[:2] + "__")[:2]


def clean_usage(u):
    return {
        "id": str(u.get("key")),
        "key": u.get("key"),
        "parentId": str(u.get("parentKey")) if u.get("parentKey") is not None else None,
        "scientificName": u.get("scientificName") or u.get("canonicalName") or "Unnamed",
        "canonicalName": u.get("canonicalName") or u.get("scientificName") or "Unnamed",
        "rank": (u.get("rank") or "UNRANKED").upper(),
        "status": (u.get("taxonomicStatus") or u.get("status") or "").upper(),
        "vernacularName": u.get("vernacularName"),
    }


def descendants(order_key):
    url = f"{API}/species/{order_key}/childrenAll"
    return normalize_usages(get_json(url))


def nearest_ancestor(raw_by_id, rec, allowed, order_id):
    current = rec
    seen = set()

    while current:
        pid = current.get("parentId")
        if not pid or pid == order_id:
            return order_id

        if pid in seen:
            return order_id
        seen.add(pid)

        parent = raw_by_id.get(pid)
        if parent is None:
            return order_id

        if parent.get("rank") in allowed:
            return pid

        current = parent

    return order_id


def main():
    taxa_path = ROOT / "data" / "taxa.json"
    payload = json.loads(taxa_path.read_text(encoding="utf-8"))
    base_taxa = payload.get("taxa", [])

    orders = [
        t for t in base_taxa
        if str(t.get("rank", "")).upper() == "ORDER"
    ]

    orders_dir = ROOT / "data" / "orders"
    families_dir = ROOT / "data" / "families"
    search_dir = ROOT / "data" / "search"

    for d in (orders_dir, families_dir, search_dir):
        if d.exists():
            for p in d.glob("*.json"):
                p.unlink()
        else:
            d.mkdir(parents=True, exist_ok=True)

    search_shards = defaultdict(list)
    order_meta = {}

    print(f"Generating lower-rank detail for {len(orders)} orders")

    for idx, order in enumerate(orders, start=1):
        order_id = str(order["id"])
        order_name = order.get("canonicalName") or order.get("scientificName") or order_id

        try:
            raw = descendants(order.get("key", order_id))
        except Exception as exc:
            print(f"WARN {idx}/{len(orders)} {order_name}: {exc}")
            continue

        cleaned = []
        for u in raw:
            r = clean_usage(u)
            if r["status"] not in ACCEPTED_STATUSES:
                continue
            cleaned.append(r)

        raw_by_id = {r["id"]: r for r in cleaned}

        families = {}
        genera = {}
        species = {}

        for r in cleaned:
            rank = r["rank"]
            if rank == "FAMILY":
                r["parentId"] = order_id
                families[r["id"]] = r

        # Real genera first.
        for r in cleaned:
            if r["rank"] != "GENUS":
                continue
            family_id = nearest_ancestor(raw_by_id, r, {"FAMILY"}, order_id)

            if family_id == order_id:
                family_id = f"__other_family_{safe_id(order_id)}"
                families.setdefault(family_id, {
                    "id": family_id,
                    "parentId": order_id,
                    "canonicalName": "Overige families",
                    "scientificName": "Overige families",
                    "rank": "FAMILY",
                    "status": "ACCEPTED",
                    "synthetic": True,
                })

            r["parentId"] = family_id
            genera[r["id"]] = r

        # Species; if a real genus is missing, create a synthetic bucket
        # inside the nearest family so every species still receives a place.
        for r in cleaned:
            if r["rank"] != "SPECIES":
                continue

            family_id = nearest_ancestor(raw_by_id, r, {"FAMILY"}, order_id)
            if family_id == order_id:
                family_id = f"__other_family_{safe_id(order_id)}"
                families.setdefault(family_id, {
                    "id": family_id,
                    "parentId": order_id,
                    "canonicalName": "Overige families",
                    "scientificName": "Overige families",
                    "rank": "FAMILY",
                    "status": "ACCEPTED",
                    "synthetic": True,
                })

            genus_id = nearest_ancestor(raw_by_id, r, {"GENUS"}, order_id)
            if genus_id == order_id or genus_id not in genera:
                genus_id = f"__other_genus_{safe_id(family_id)}"
                genera.setdefault(genus_id, {
                    "id": genus_id,
                    "parentId": family_id,
                    "canonicalName": "Overige soorten",
                    "scientificName": "Overige soorten",
                    "rank": "GENUS",
                    "status": "ACCEPTED",
                    "synthetic": True,
                })

            # Make sure real genus also points into the same family.
            genera[genus_id]["parentId"] = family_id

            r["parentId"] = genus_id
            r["speciesCount"] = 1
            r["weight"] = 1
            r["mapWeight"] = 1
            species[r["id"]] = r

        # Count species per genus and family.
        genus_counts = defaultdict(int)
        family_counts = defaultdict(int)

        for s in species.values():
            gid = s["parentId"]
            genus_counts[gid] += 1
            family_id = genera[gid]["parentId"]
            family_counts[family_id] += 1

        for gid, g in genera.items():
            g["speciesCount"] = max(1, genus_counts.get(gid, 0))
            g["weight"] = g["speciesCount"]
            g["mapWeight"] = g["speciesCount"]

        for fid, f in families.items():
            f["speciesCount"] = max(1, family_counts.get(fid, 0))
            f["weight"] = f["speciesCount"]
            f["mapWeight"] = f["speciesCount"]

        # ORDER file contains only FAMILY + GENUS: small enough for fast lazy load.
        order_taxa = list(families.values()) + list(genera.values())
        order_payload = {
            "meta": {
                "orderId": order_id,
                "orderName": order_name,
                "families": len(families),
                "genera": len(genera),
                "species": len(species),
                "weightRule": "family/genus = descendant species count",
            },
            "taxa": order_taxa,
        }

        (orders_dir / f"{safe_id(order_id)}.json").write_text(
            json.dumps(order_payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        # FAMILY files contain species only.  This means species are downloaded
        # only when a family/genus becomes relevant in the browser.
        by_family_species = defaultdict(list)
        for s in species.values():
            gid = s["parentId"]
            fid = genera[gid]["parentId"]
            by_family_species[fid].append(s)

        for fid, sp in by_family_species.items():
            family_payload = {
                "meta": {
                    "familyId": fid,
                    "orderId": order_id,
                    "species": len(sp),
                    "weightRule": "every species = fixed weight 1",
                },
                "taxa": sp,
            }
            (families_dir / f"{safe_id(fid)}.json").write_text(
                json.dumps(family_payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

        # Prefix-sharded search index for FAMILY / GENUS / SPECIES.
        for t in order_taxa:
            if t.get("synthetic"):
                continue
            n = norm_name(t.get("canonicalName"))
            if not n:
                continue
            search_shards[search_prefix(n)].append({
                "n": n,
                "id": str(t["id"]),
                "o": order_id,
                "f": str(t["parentId"]) if t["rank"] == "GENUS" else str(t["id"]),
                "r": t["rank"],
            })

        for s in species.values():
            n = norm_name(s.get("canonicalName"))
            if not n:
                continue
            gid = str(s["parentId"])
            fid = str(genera[gid]["parentId"])
            search_shards[search_prefix(n)].append({
                "n": n,
                "id": str(s["id"]),
                "o": order_id,
                "f": fid,
                "g": gid,
                "r": "SPECIES",
            })

        order["speciesCount"] = len(species)
        order["familyCount"] = len(families)
        order["genusCount"] = len(genera)
        order["detailAvailable"] = True

        order_meta[order_id] = {
            "families": len(families),
            "genera": len(genera),
            "species": len(species),
        }

        print(
            f"[{idx}/{len(orders)}] {order_name}: "
            f"{len(families)} families, {len(genera)} genera, {len(species)} species"
        )

    for prefix, rows in search_shards.items():
        rows.sort(key=lambda x: x["n"])
        (search_dir / f"{prefix}.json").write_text(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    payload.setdefault("meta", {})
    payload["meta"]["detailGeneratedAt"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    payload["meta"]["detailOrders"] = len(order_meta)
    payload["meta"]["detailWeightRules"] = {
        "order": "existing iNaturalist-derived map weight",
        "family": "descendant species count",
        "genus": "descendant species count",
        "species": "fixed weight 1",
    }

    taxa_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    manifest = {
        "generatedAt": payload["meta"]["detailGeneratedAt"],
        "orders": order_meta,
        "searchShardCount": len(search_shards),
    }
    (ROOT / "data" / "detail_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(
        f"Done: {len(order_meta)} order detail files, "
        f"{len(list(families_dir.glob('*.json')))} family species files, "
        f"{len(search_shards)} search shards"
    )


if __name__ == "__main__":
    main()
