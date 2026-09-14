#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
API = CFG["taxonomy"]["apiBase"].rstrip("/")
DATASET = CFG["taxonomy"]["datasetKey"]

HEADERS = {"User-Agent": "AtlasOfLife/0.42.2 parallel detail generator"}

REJECTED = {
    "SYNONYM", "HETEROTYPIC_SYNONYM", "HOMOTYPIC_SYNONYM",
    "PROPARTE_SYNONYM", "MISAPPLIED", "DOUBTFUL"
}

PAGE_SIZE = 1000
DEEP_PAGE_LIMIT = 95000
AUDIT_WORKERS = int(os.environ.get("DETAIL_AUDIT_WORKERS", "16"))
ORDER_WORKERS = int(os.environ.get("DETAIL_ORDER_WORKERS", "4"))
FAMILY_WORKERS = int(os.environ.get("DETAIL_FAMILY_WORKERS", "6"))


def get_json(url: str, retries: int = 6):
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
            time.sleep(min(20, 1.4 * (2 ** attempt)))
    raise last


def safe_id(value) -> str:
    return "".join(
        c if c.isalnum() or c in "._-" else "_" for c in str(value)
    )


def norm_name(value: str | None) -> str:
    s = unicodedata.normalize("NFD", str(value or "")).lower()
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


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
    # First try accepted usages; fallback to unfiltered if this checklist does
    # not expose accepted counts in the expected way.
    for status in ("ACCEPTED", None):
        page = get_json(search_url(higher_key, rank, 0, 0, status))
        count = int(page.get("count") or 0)
        if count:
            return count, status
    return 0, "ACCEPTED"


def search_all(higher_key, rank, expected_count=None, max_records=DEEP_PAGE_LIMIT):
    count, status = search_count(higher_key, rank)
    if expected_count is not None and expected_count > count:
        count = expected_count
    if count == 0:
        return [], 0
    if count > max_records:
        raise OverflowError(f"{rank} under {higher_key}: {count} > safe page limit")

    rows, offset = [], 0
    while offset < count:
        page = get_json(search_url(higher_key, rank, PAGE_SIZE, offset, status))
        batch = page.get("results") or []
        if not batch:
            break
        rows.extend(x for x in batch if accepted(x))
        offset += len(batch)
        if page.get("endOfRecords"):
            break

    out = {}
    for x in rows:
        k = key_of(x)
        if k is not None:
            out[str(k)] = x
    return list(out.values()), count


def classification_key(x, rank):
    value = x.get(rank.lower() + "Key")
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


def synthetic_family(order_id):
    fid = f"__other_family_{safe_id(order_id)}"
    return fid, {
        "id": fid, "parentId": str(order_id),
        "canonicalName": "Overige families",
        "scientificName": "Overige families",
        "rank": "FAMILY", "status": "ACCEPTED", "synthetic": True,
    }


def synthetic_genus(family_id):
    gid = f"__other_genus_{safe_id(family_id)}"
    return gid, {
        "id": gid, "parentId": str(family_id),
        "canonicalName": "Overige soorten",
        "scientificName": "Overige soorten",
        "rank": "GENUS", "status": "ACCEPTED", "synthetic": True,
    }


def load_base():
    taxa_path = ROOT / "data" / "taxa.json"
    payload = json.loads(taxa_path.read_text(encoding="utf-8"))
    orders = [
        t for t in payload.get("taxa", [])
        if str(t.get("rank", "")).upper() == "ORDER"
    ]
    if not orders:
        raise RuntimeError("No ORDER taxa found in data/taxa.json")
    return taxa_path, payload, orders


# -------------------------------------------------------------------
# PHASE 1 — FAST AUDIT
# -------------------------------------------------------------------

def audit_one(order):
    oid = str(order["id"])
    key = order.get("key", oid)
    name = order.get("canonicalName") or order.get("scientificName") or oid

    families, _ = search_count(key, "FAMILY")
    genera, _ = search_count(key, "GENUS")
    species, _ = search_count(key, "SPECIES")

    return oid, {
        "name": name,
        "key": key,
        "families": families,
        "genera": genera,
        "species": species,
    }


def audit():
    taxa_path, payload, orders = load_base()
    print(f"AUDIT: {len(orders)} orders with {AUDIT_WORKERS} parallel workers")

    results = {}
    started = time.time()

    with ThreadPoolExecutor(max_workers=AUDIT_WORKERS) as ex:
        futures = {ex.submit(audit_one, o): o for o in orders}
        completed = 0
        for fut in as_completed(futures):
            order = futures[fut]
            oid, rec = fut.result()
            results[oid] = rec
            completed += 1
            if completed % 25 == 0 or completed == len(orders):
                elapsed = time.time() - started
                print(
                    f"AUDIT {completed}/{len(orders)} · "
                    f"{elapsed:.0f}s · latest {rec['name']}: "
                    f"{rec['families']} F / {rec['genera']} G / {rec['species']} S"
                )

    by_name = {norm_name(v["name"]): v for v in results.values()}
    probes = ["Lepidoptera", "Trichoptera", "Coleoptera", "Passeriformes"]
    probe_records = [by_name.get(norm_name(n)) for n in probes]
    probe_records = [x for x in probe_records if x]

    if not probe_records:
        raise RuntimeError("AUDIT FAILED: none of the known validation orders were found")

    bad_probes = [
        x for x in probe_records
        if x["families"] <= 0 or x["genera"] <= 0 or x["species"] <= 0
    ]
    if bad_probes:
        raise RuntimeError(f"AUDIT FAILED known orders: {bad_probes}")

    nonzero_species = sum(1 for x in results.values() if x["species"] > 0)
    total_species = sum(x["species"] for x in results.values())
    total_families = sum(x["families"] for x in results.values())
    total_genera = sum(x["genera"] for x in results.values())

    suspicious = [
        x for x in results.values()
        if (x["genera"] > 0 and x["species"] == 0)
        or (x["families"] > 0 and x["genera"] == 0 and x["species"] > 0)
    ]

    if nonzero_species < 100 or total_species < 100000:
        raise RuntimeError(
            f"AUDIT FAILED implausible totals: {nonzero_species} nonempty orders, "
            f"{total_species} species"
        )

    report = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "parallel GBIF Species Search count audit",
        "workers": AUDIT_WORKERS,
        "summary": {
            "orders": len(orders),
            "ordersWithSpecies": nonzero_species,
            "families": total_families,
            "genera": total_genera,
            "species": total_species,
            "suspiciousOrders": len(suspicious),
        },
        "knownOrderChecks": probe_records,
        "suspicious": sorted(
            suspicious,
            key=lambda x: (-x["genera"], x["name"])
        )[:250],
        "orders": results,
    }

    out = ROOT / "data" / "detail_audit.json"
    out.write_text(
        json.dumps(report, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print("AUDIT OK")
    print(json.dumps(report["summary"], ensure_ascii=False))
    for rec in probe_records:
        print(
            f"CHECK {rec['name']}: "
            f"{rec['families']} families, {rec['genera']} genera, "
            f"{rec['species']} species"
        )
    if suspicious:
        print(
            f"NOTE: {len(suspicious)} orders have an unusual zero-rank pattern. "
            "They are recorded in data/detail_audit.json; generation continues "
            "only for orders whose audited species count is > 0."
        )


# -------------------------------------------------------------------
# PHASE 2 — PARALLEL DETAIL GENERATION
# -------------------------------------------------------------------

def fetch_species_for_family(family):
    return search_all(
        family.get("key", family["id"]),
        "SPECIES",
        expected_count=None,
    )[0]


def fetch_order(order, audit_rec):
    order_id = str(order["id"])
    higher_key = order.get("key", order_id)

    family_raw, _ = search_all(
        higher_key, "FAMILY", expected_count=audit_rec["families"]
    )
    genus_raw, _ = search_all(
        higher_key, "GENUS", expected_count=audit_rec["genera"]
    )

    families, family_name_to_id = {}, {}
    for x in family_raw:
        r = clean_result(x, "FAMILY")
        if not r:
            continue
        r["parentId"] = order_id
        families[r["id"]] = r
        family_name_to_id[norm_name(r["canonicalName"])] = r["id"]

    genera, genus_name_to_id = {}, {}
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

    audited_species = int(audit_rec["species"])
    if audited_species <= 0:
        return {
            "families": families,
            "genera": genera,
            "species": {},
            "audit": audit_rec,
        }

    if audited_species <= DEEP_PAGE_LIMIT:
        species_rows, _ = search_all(
            higher_key, "SPECIES", expected_count=audited_species
        )
    else:
        print(
            f"  {audit_rec['name']}: {audited_species} species; "
            f"parallel split across {len(families)} families"
        )
        species_rows = []
        real_families = [f for f in families.values() if not f.get("synthetic")]

        def family_job(f):
            try:
                return fetch_species_for_family(f)
            except OverflowError:
                family_genera = [
                    g for g in genera.values()
                    if str(g.get("parentId")) == str(f["id"])
                ]
                rows = []
                for g in family_genera:
                    rows.extend(
                        search_all(g.get("key", g["id"]), "SPECIES")[0]
                    )
                return rows

        with ThreadPoolExecutor(max_workers=FAMILY_WORKERS) as ex:
            futures = {ex.submit(family_job, f): f for f in real_families}
            completed = 0
            for fut in as_completed(futures):
                species_rows.extend(fut.result())
                completed += 1
                if completed % 25 == 0:
                    print(
                        f"    {audit_rec['name']} families "
                        f"{completed}/{len(real_families)}"
                    )

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
        "audit": audit_rec,
    }


def append_search_part(parts_dir, row):
    prefix = search_prefix(row.get("n"))
    with (parts_dir / f"{prefix}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_order(staging, order, result):
    order_id = str(order["id"])
    order_name = order.get("canonicalName") or order.get("scientificName") or order_id
    families = result["families"]
    genera = result["genera"]
    species = result["species"]
    audit_rec = result["audit"]

    orders_dir = staging / "orders"
    families_dir = staging / "families"
    parts_dir = staging / "_search_parts"

    order_payload = {
        "meta": {
            "orderId": order_id,
            "orderName": order_name,
            "families": len(families),
            "genera": len(genera),
            "species": len(species),
            "audited": audit_rec,
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
        (families_dir / f"{safe_id(fid)}.json").write_text(
            json.dumps({
                "meta": {
                    "familyId": fid,
                    "orderId": order_id,
                    "species": len(sp),
                    "weightRule": "every species = fixed weight 1",
                },
                "taxa": sp,
            }, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    for t in list(families.values()) + list(genera.values()):
        if t.get("synthetic"):
            continue
        n = norm_name(t.get("canonicalName"))
        if not n:
            continue
        append_search_part(parts_dir, {
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
        append_search_part(parts_dir, {
            "n": n,
            "id": str(s["id"]),
            "o": order_id,
            "f": str(g["parentId"]),
            "g": gid,
            "r": "SPECIES",
        })

    order["speciesCount"] = len(species)
    order["familyCount"] = len(families)
    order["genusCount"] = len(genera)
    order["detailAvailable"] = True


def finalise_search(staging):
    parts = staging / "_search_parts"
    search = staging / "search"
    search.mkdir(parents=True, exist_ok=True)

    for p in parts.glob("*.jsonl"):
        rows = []
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
        rows.sort(key=lambda x: x["n"])
        (search / (p.stem + ".json")).write_text(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    shutil.rmtree(parts)


def generate():
    taxa_path, payload, orders = load_base()
    audit_path = ROOT / "data" / "detail_audit.json"
    if not audit_path.exists():
        raise RuntimeError("Run generate-details.py --audit first")

    audit_report = json.loads(audit_path.read_text(encoding="utf-8"))
    audit_orders = audit_report.get("orders", {})

    targets = [
        o for o in orders
        if int(audit_orders.get(str(o["id"]), {}).get("species", 0)) > 0
    ]
    skipped = len(orders) - len(targets)

    print(
        f"GENERATE: {len(targets)} orders with species; "
        f"skip {skipped} zero-species orders; {ORDER_WORKERS} order workers"
    )

    staging_root = ROOT / ".detail-staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging = staging_root / "data"
    for name in ("orders", "families", "_search_parts"):
        (staging / name).mkdir(parents=True, exist_ok=True)

    totals = defaultdict(int)
    order_meta = {}
    failures = []
    started = time.time()

    with ThreadPoolExecutor(max_workers=ORDER_WORKERS) as ex:
        futures = {
            ex.submit(fetch_order, o, audit_orders[str(o["id"])]): o
            for o in targets
        }

        completed = 0
        for fut in as_completed(futures):
            order = futures[fut]
            name = order.get("canonicalName") or str(order["id"])
            try:
                result = fut.result()
                write_order(staging, order, result)
            except Exception as exc:
                failures.append({"order": name, "id": str(order["id"]), "error": str(exc)})
                print(f"ERROR {name}: {exc}")
                continue

            f = len(result["families"])
            g = len(result["genera"])
            s = len(result["species"])
            totals["families"] += f
            totals["genera"] += g
            totals["species"] += s
            order_meta[str(order["id"])] = {
                "name": name, "families": f, "genera": g, "species": s,
                "audit": result["audit"],
            }

            completed += 1
            elapsed = time.time() - started
            print(
                f"DONE {completed}/{len(targets)} · {elapsed/60:.1f} min · "
                f"{name}: {f} F / {g} G / {s} S"
            )

    if failures:
        failure_path = ROOT / "data" / "detail_failures.json"
        failure_path.write_text(
            json.dumps(failures, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise RuntimeError(
            f"DETAIL GENERATION FAILED for {len(failures)} orders. "
            "See data/detail_failures.json. Live detail data was NOT replaced."
        )

    finalise_search(staging)

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest = {
        "generatedAt": generated_at,
        "method": "v42.2 audited + parallel detail generation",
        "workers": {
            "order": ORDER_WORKERS,
            "familyWithinLargeOrder": FAMILY_WORKERS,
        },
        "orders": order_meta,
        "totals": dict(totals),
        "auditSummary": audit_report.get("summary", {}),
    }
    (staging / "detail_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

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
    print("GENERATION OK")
    print(json.dumps(dict(totals), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true")
    group.add_argument("--generate", action="store_true")
    args = parser.parse_args()

    if args.audit:
        audit()
    else:
        generate()


if __name__ == "__main__":
    main()
