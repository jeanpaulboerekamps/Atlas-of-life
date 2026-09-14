#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import unicodedata
import urllib.parse
import urllib.request
import shutil
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
API = CFG["taxonomy"]["apiBase"].rstrip("/")
DATASET = CFG["taxonomy"]["datasetKey"]

HEADERS = {
    "User-Agent": "AtlasOfLife/0.42.1 taxonomic detail generator"
}

REJECTED = {
    "SYNONYM", "HETEROTYPIC_SYNONYM", "HOMOTYPIC_SYNONYM",
    "PROPARTE_SYNONYM", "MISAPPLIED", "DOUBTFUL"
}

PAGE_SIZE = 1000
DEEP_PAGE_LIMIT = 95000


def get_json(url: str, retries: int = 5):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as exc:
            last = exc
            if attempt + 1 == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise last


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


def rank_of(x):
    return str(x.get("rank") or x.get("taxonRank") or "").upper()


def status_of(x):
    return str(x.get("taxonomicStatus") or x.get("status") or "").upper()


def accepted(x):
    return status_of(x) not in REJECTED


def canonical(x):
    return (
        x.get("canonicalName")
        or x.get("scientificName")
        or x.get("name")
        or "Unnamed"
    )


def key_of(x):
    return x.get("key") or x.get("usageKey") or x.get("taxonKey")


def clean_result(x, rank=None):
    key = key_of(x)
    if key is None:
        return None
    r = (rank or rank_of(x) or "UNRANKED").upper()
    return {
        "id": str(key),
        "key": key,
        "parentId": None,
        "scientificName": x.get("scientificName") or canonical(x),
        "canonicalName": canonical(x),
        "rank": r,
        "status": status_of(x) or "ACCEPTED",
        "vernacularName": x.get("vernacularName"),
    }


def search_url(higher_key, rank, limit, offset=0, status="ACCEPTED"):
    # GBIF accepts the camelCase search parameters documented by the Species API.
    params = {
        "datasetKey": DATASET,
        "higherTaxonKey": higher_key,
        "rank": rank,
        "limit": limit,
        "offset": offset,
    }
    if status:
        params["status"] = status
    return f"{API}/species/search?{urllib.parse.urlencode(params)}"


def search_count(higher_key, rank):
    # Prefer accepted taxa. If a checklist behaves differently for status filtering,
    # retry without the status parameter and filter locally.
    for status in ("ACCEPTED", None):
        page = get_json(search_url(higher_key, rank, 0, 0, status))
        count = int(page.get("count") or 0)
        if count:
            return count, status
    return 0, "ACCEPTED"


def search_all(higher_key, rank, max_records=DEEP_PAGE_LIMIT):
    count, status = search_count(higher_key, rank)
    if count == 0:
        return [], 0

    if count > max_records:
        raise OverflowError(
            f"{rank} under {higher_key}: {count} records exceeds safe deep-page limit"
        )

    rows = []
    offset = 0
    while offset < count:
        page = get_json(
            search_url(higher_key, rank, PAGE_SIZE, offset, status)
        )
        batch = page.get("results") or []
        if not batch:
            break
        for x in batch:
            if accepted(x):
                rows.append(x)
        offset += len(batch)
        if page.get("endOfRecords"):
            break

    # De-duplicate by checklist usage key.
    out = {}
    for x in rows:
        k = key_of(x)
        if k is not None:
            out[str(k)] = x
    return list(out.values()), count


def classification_key(x, rank):
    # Search responses normally expose familyKey/genusKey.  The fallbacks
    # make this work with variants that use string keys or classification arrays.
    field = rank.lower() + "Key"
    value = x.get(field)
    if value is not None:
        return str(value)

    cls = x.get("classification")
    if isinstance(cls, list):
        for item in cls:
            if str(item.get("rank") or "").upper() == rank.upper():
                k = key_of(item)
                if k is not None:
                    return str(k)
    return None


def classification_name(x, rank):
    value = x.get(rank.lower())
    if value:
        return str(value)
    cls = x.get("classification")
    if isinstance(cls, list):
        for item in cls:
            if str(item.get("rank") or "").upper() == rank.upper():
                return canonical(item)
    return None


def synthetic_family(order_id, name="Overige families"):
    fid = f"__other_family_{safe_id(order_id)}"
    return fid, {
        "id": fid,
        "parentId": str(order_id),
        "canonicalName": name,
        "scientificName": name,
        "rank": "FAMILY",
        "status": "ACCEPTED",
        "synthetic": True,
    }


def synthetic_genus(family_id, name="Overige soorten"):
    gid = f"__other_genus_{safe_id(family_id)}"
    return gid, {
        "id": gid,
        "parentId": str(family_id),
        "canonicalName": name,
        "scientificName": name,
        "rank": "GENUS",
        "status": "ACCEPTED",
        "synthetic": True,
    }


def fetch_order(order):
    order_id = str(order["id"])
    higher_key = order.get("key", order_id)

    family_raw, family_count = search_all(higher_key, "FAMILY")
    genus_raw, genus_count = search_all(higher_key, "GENUS")
    species_count, _ = search_count(higher_key, "SPECIES")

    families = {}
    family_name_to_id = {}

    for x in family_raw:
        r = clean_result(x, "FAMILY")
        if not r:
            continue
        r["parentId"] = order_id
        families[r["id"]] = r
        family_name_to_id[norm_name(r["canonicalName"])] = r["id"]

    genera = {}
    genus_name_to_id = {}

    for x in genus_raw:
        r = clean_result(x, "GENUS")
        if not r:
            continue

        fid = classification_key(x, "FAMILY")
        if not fid or fid not in families:
            fname = classification_name(x, "FAMILY")
            fid = family_name_to_id.get(norm_name(fname)) if fname else None

        if not fid:
            fid, f = synthetic_family(order_id)
            families.setdefault(fid, f)

        r["parentId"] = fid
        genera[r["id"]] = r
        genus_name_to_id[norm_name(r["canonicalName"])] = r["id"]

    # For most orders, fetch species in one search.  Large orders (e.g. Coleoptera,
    # Lepidoptera) are split by family so the GBIF 100k deep-page boundary is
    # never crossed.
    species_rows = []

    if species_count <= DEEP_PAGE_LIMIT:
        species_rows, _ = search_all(higher_key, "SPECIES")
    else:
        print(
            f"  large order: {species_count} species; splitting species fetch by family"
        )
        for fi, family in enumerate(list(families.values()), start=1):
            if family.get("synthetic"):
                continue
            try:
                rows, cnt = search_all(family.get("key", family["id"]), "SPECIES")
                species_rows.extend(rows)
            except OverflowError:
                # Extremely large family: split by genus.
                family_genera = [
                    g for g in genera.values()
                    if str(g.get("parentId")) == str(family["id"])
                ]
                for g in family_genera:
                    rows, _ = search_all(g.get("key", g["id"]), "SPECIES")
                    species_rows.extend(rows)

            if fi % 25 == 0:
                print(f"    families {fi}/{len(families)}")

    # De-duplicate species across split queries.
    dedup = {}
    for x in species_rows:
        k = key_of(x)
        if k is not None:
            dedup[str(k)] = x
    species_rows = list(dedup.values())

    species = {}

    for x in species_rows:
        r = clean_result(x, "SPECIES")
        if not r:
            continue

        fid = classification_key(x, "FAMILY")
        if not fid or fid not in families:
            fname = classification_name(x, "FAMILY")
            fid = family_name_to_id.get(norm_name(fname)) if fname else None

        if not fid:
            fid, f = synthetic_family(order_id)
            families.setdefault(fid, f)

        gid = classification_key(x, "GENUS")
        if not gid or gid not in genera:
            gname = classification_name(x, "GENUS")
            gid = genus_name_to_id.get(norm_name(gname)) if gname else None

        if not gid:
            gid, g = synthetic_genus(fid)
            genera.setdefault(gid, g)

        # If the genus existed but was attached to another/unknown family,
        # the species classification wins.
        genera[gid]["parentId"] = fid

        r["parentId"] = gid
        r["speciesCount"] = 1
        r["weight"] = 1
        r["mapWeight"] = 1
        species[r["id"]] = r

    genus_counts = defaultdict(int)
    family_counts = defaultdict(int)

    for s in species.values():
        gid = str(s["parentId"])
        genus_counts[gid] += 1
        g = genera.get(gid)
        if g:
            family_counts[str(g["parentId"])] += 1

    for gid, g in genera.items():
        g["speciesCount"] = genus_counts.get(gid, 0)
        # Keep empty accepted genera visible but minimal.
        g["weight"] = max(1, g["speciesCount"])
        g["mapWeight"] = g["weight"]

    for fid, f in families.items():
        f["speciesCount"] = family_counts.get(fid, 0)
        f["weight"] = max(1, f["speciesCount"])
        f["mapWeight"] = f["weight"]

    return {
        "families": families,
        "genera": genera,
        "species": species,
        "apiCounts": {
            "families": family_count,
            "genera": genus_count,
            "species": species_count,
        },
    }


def write_order(staging, order, result, search_shards):
    order_id = str(order["id"])
    order_name = order.get("canonicalName") or order.get("scientificName") or order_id
    families = result["families"]
    genera = result["genera"]
    species = result["species"]

    orders_dir = staging / "orders"
    families_dir = staging / "families"

    order_payload = {
        "meta": {
            "orderId": order_id,
            "orderName": order_name,
            "families": len(families),
            "genera": len(genera),
            "species": len(species),
            "apiCounts": result["apiCounts"],
            "weightRule": "family/genus = descendant species count",
        },
        "taxa": list(families.values()) + list(genera.values()),
    }

    (orders_dir / f"{safe_id(order_id)}.json").write_text(
        json.dumps(order_payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    by_family_species = defaultdict(list)
    for s in species.values():
        gid = str(s["parentId"])
        g = genera.get(gid)
        if g:
            by_family_species[str(g["parentId"])].append(s)

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

    for t in list(families.values()) + list(genera.values()):
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
        g = genera.get(gid)
        if not g:
            continue
        fid = str(g["parentId"])
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


def main():
    taxa_path = ROOT / "data" / "taxa.json"
    payload = json.loads(taxa_path.read_text(encoding="utf-8"))
    base_taxa = payload.get("taxa", [])

    orders = [
        t for t in base_taxa
        if str(t.get("rank", "")).upper() == "ORDER"
    ]
    if not orders:
        raise RuntimeError("No ORDER taxa found in data/taxa.json")

    print(f"Generating lower-rank detail for {len(orders)} orders")
    print("Method: /species/search with higherTaxonKey + rank (not childrenAll)")

    # ---------- PRE-FLIGHT ----------
    known_names = ["Lepidoptera", "Trichoptera", "Coleoptera", "Passeriformes"]
    probe = next(
        (o for name in known_names for o in orders
         if norm_name(o.get("canonicalName")) == norm_name(name)),
        orders[0],
    )

    print(f"Preflight: {probe.get('canonicalName')} ({probe.get('key', probe['id'])})")
    probe_result = fetch_order(probe)

    pf = len(probe_result["families"])
    pg = len(probe_result["genera"])
    ps = len(probe_result["species"])
    print(f"Preflight result: {pf} families, {pg} genera, {ps} species")

    if pf == 0 or pg == 0 or ps == 0:
        raise RuntimeError(
            "DETAIL PREFLIGHT FAILED: known order returned empty lower taxonomy. "
            "Nothing was written to data/. This prevents another long empty run."
        )

    # Transactional staging: existing detail data is untouched until all validation passes.
    staging_root = ROOT / ".detail-staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging = staging_root / "data"
    for name in ("orders", "families", "search"):
        (staging / name).mkdir(parents=True, exist_ok=True)

    search_shards = defaultdict(list)
    order_meta = {}
    totals = defaultdict(int)
    nonempty_orders = 0

    probe_id = str(probe["id"])
    prefetched = {probe_id: probe_result}

    for idx, order in enumerate(orders, start=1):
        order_id = str(order["id"])
        order_name = order.get("canonicalName") or order.get("scientificName") or order_id

        try:
            result = prefetched.pop(order_id, None) or fetch_order(order)
        except Exception as exc:
            print(f"WARN {idx}/{len(orders)} {order_name}: {exc}")
            continue

        write_order(staging, order, result, search_shards)

        f = len(result["families"])
        g = len(result["genera"])
        s = len(result["species"])
        totals["families"] += f
        totals["genera"] += g
        totals["species"] += s
        if s > 0:
            nonempty_orders += 1

        order_meta[order_id] = {
            "name": order_name,
            "families": f,
            "genera": g,
            "species": s,
            "apiCounts": result["apiCounts"],
        }

        print(f"[{idx}/{len(orders)}] {order_name}: {f} families, {g} genera, {s} species")

    # Conservative global validation. The preflight catches endpoint mistakes immediately;
    # these checks catch partial or prematurely truncated runs.
    if nonempty_orders < min(50, max(10, len(orders) // 20)):
        raise RuntimeError(
            f"DETAIL SANITY FAILED: only {nonempty_orders}/{len(orders)} orders have species"
        )
    if totals["families"] < 100 or totals["genera"] < 500 or totals["species"] < 5000:
        raise RuntimeError(
            "DETAIL SANITY FAILED: totals implausibly low: "
            f"{dict(totals)}"
        )

    for prefix, rows in search_shards.items():
        rows.sort(key=lambda x: x["n"])
        (staging / "search" / f"{prefix}.json").write_text(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest = {
        "generatedAt": generated_at,
        "method": "GBIF Species Search higherTaxonKey + rank",
        "orders": order_meta,
        "totals": dict(totals),
        "nonemptyOrders": nonempty_orders,
        "searchShardCount": len(search_shards),
    }
    (staging / "detail_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    # Only now replace live detail directories.
    live_data = ROOT / "data"
    for name in ("orders", "families", "search"):
        dst = live_data / name
        src = staging / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))

    shutil.move(
        str(staging / "detail_manifest.json"),
        str(live_data / "detail_manifest.json"),
    )

    payload.setdefault("meta", {})
    payload["meta"]["detailGeneratedAt"] = generated_at
    payload["meta"]["detailOrders"] = len(order_meta)
    payload["meta"]["detailTotals"] = dict(totals)
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

    shutil.rmtree(staging_root, ignore_errors=True)

    print(
        "DONE: "
        f"{len(order_meta)} orders, "
        f"{totals['families']} families, "
        f"{totals['genera']} genera, "
        f"{totals['species']} species"
    )


if __name__ == "__main__":
    main()
