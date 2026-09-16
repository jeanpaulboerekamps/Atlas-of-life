#!/usr/bin/env python3
"""Build Atlas of Life shards from the official iNaturalist taxonomy DwC-A.

The generated JSON keeps iNaturalist taxon ids as the canonical identifiers.
Only ranks displayed by the Atlas are materialised; intermediate ranks are
collapsed while the original iNaturalist parent chain remains the source of
truth. Dutch and English preferred names are added from the DwC-A extensions.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import shutil
import tempfile
import time
import unicodedata
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARCHIVE = "https://www.inaturalist.org/taxa/inaturalist-taxonomy.dwca.zip"
USER_AGENT = "AtlasOfLife-iNaturalist-taxonomy/1.0"
BASE_RANKS = {"root", "stateofmatter", "domain", "kingdom", "phylum", "class", "order"}
DETAIL_RANKS = {"family", "genus", "species"}
VISIBLE_RANKS = BASE_RANKS | DETAIL_RANKS
RANK_ORDER = {
    "root": 0,
    "stateofmatter": 1,
    "domain": 2,
    "kingdom": 3,
    "phylum": 4,
    "class": 5,
    "order": 6,
    "family": 7,
    "genus": 8,
    "species": 9,
}


def compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def norm_name(value: str | None) -> str:
    text = unicodedata.normalize("NFD", str(value or "")).casefold()
    return "".join(c for c in text if unicodedata.category(c) != "Mn").strip()


def search_prefix(value: str | None) -> str:
    cleaned = "".join(c for c in norm_name(value) if c.isalnum())
    return (cleaned[:2] + "__")[:2]


def safe_id(value: object) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value))


def taxon_id(value: str | None) -> str | None:
    value = str(value or "").strip().rstrip("/")
    if not value:
        return None
    tail = value.rsplit("/", 1)[-1]
    return tail if tail.isdigit() else None


def acquire_archive(source: str) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    local = Path(source)
    if local.exists():
        return local.resolve(), None
    temp = tempfile.TemporaryDirectory(prefix="atlas-inat-")
    target = Path(temp.name) / "inaturalist-taxonomy.dwca.zip"
    request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)
    return target, temp


def read_taxa(archive: zipfile.ZipFile) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with archive.open("taxa.csv") as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
        for row in csv.DictReader(text):
            identifier = taxon_id(row.get("id") or row.get("taxonID"))
            if not identifier:
                continue
            rows[identifier] = {
                "id": identifier,
                "parent": taxon_id(row.get("parentNameUsageID")),
                "name": str(row.get("scientificName") or "").strip(),
                "rank": str(row.get("taxonRank") or "unranked").strip().casefold(),
                "modified": row.get("modified") or None,
            }
    return rows


def choose_names(archive: zipfile.ZipFile, filename: str, wanted: set[str], country: str | None) -> dict[str, str]:
    chosen: dict[str, tuple[int, str]] = {}
    with archive.open(filename) as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
        for row in csv.DictReader(text):
            identifier = taxon_id(row.get("id"))
            name = str(row.get("vernacularName") or "").strip()
            if not identifier or identifier not in wanted or not name:
                continue
            locality = str(row.get("countryCode") or "").upper()
            score = 2 if country and locality == country else 1 if not locality else 0
            previous = chosen.get(identifier)
            if previous is None or score > previous[0]:
                chosen[identifier] = (score, name)
    return {identifier: value[1] for identifier, value in chosen.items()}


def lineage(rows: dict[str, dict], identifier: str, cache: dict[str, list[str]]) -> list[str]:
    if identifier in cache:
        return cache[identifier]
    path: list[str] = []
    seen: set[str] = set()
    current = identifier
    while current and current in rows and current not in seen:
        seen.add(current)
        path.append(current)
        current = rows[current].get("parent")
    path.reverse()
    cache[identifier] = path
    return path


def nearest_rank(rows: dict[str, dict], ids: list[str], rank: str) -> str | None:
    for identifier in reversed(ids):
        if rows[identifier]["rank"] == rank:
            return identifier
    return None


def make_node(row: dict, parent_id: str | None, nl: dict[str, str], en: dict[str, str]) -> dict:
    identifier = row["id"]
    node = {
        "id": identifier,
        "key": int(identifier),
        "parentId": parent_id,
        "scientificName": row["name"],
        "canonicalName": row["name"],
        "rank": row["rank"].upper(),
        "status": "ACTIVE",
    }
    if nl.get(identifier):
        node["vernacularNameNl"] = nl[identifier]
    if en.get(identifier):
        node["vernacularNameEn"] = en[identifier]
    if nl.get(identifier) or en.get(identifier):
        node["vernacularName"] = nl.get(identifier) or en.get(identifier)
    return node


def synthetic_node(identifier: str, parent_id: str, name_nl: str, name_en: str, rank: str) -> dict:
    return {
        "id": identifier,
        "parentId": parent_id,
        "scientificName": name_en,
        "canonicalName": name_en,
        "vernacularName": name_nl,
        "vernacularNameNl": name_nl,
        "vernacularNameEn": name_en,
        "rank": rank,
        "status": "SYNTHETIC",
        "synthetic": True,
    }


def build(rows: dict[str, dict], nl: dict[str, str], en: dict[str, str], output: Path, max_species: int | None = None):
    species_ids = [identifier for identifier, row in rows.items() if row["rank"] == "species"]
    if max_species:
        species_ids = species_ids[:max_species]

    lineage_cache: dict[str, list[str]] = {}
    base: dict[str, dict] = {}
    families: dict[str, dict] = {}
    genera: dict[str, dict] = {}
    species: dict[str, dict] = {}
    species_order: dict[str, str] = {}
    species_family: dict[str, str] = {}
    life_id = next(
        (
            identifier for identifier, row in rows.items()
            if row["rank"] in {"root", "stateofmatter"}
            and row["name"].casefold() == "life"
        ),
        None,
    )

    for index, sid in enumerate(species_ids, 1):
        ids = lineage(rows, sid, lineage_cache)
        if not ids:
            continue

        # Materialise the visible upper lineage with intermediate ranks collapsed.
        visible_upper = [identifier for identifier in ids if rows[identifier]["rank"] in BASE_RANKS]
        previous = None
        for identifier in visible_upper:
            if identifier not in base:
                base[identifier] = make_node(rows[identifier], previous, nl, en)
            elif previous is not None and not base[identifier].get("parentId"):
                base[identifier]["parentId"] = previous
            previous = identifier

        order_id = nearest_rank(rows, ids, "order")
        if not order_id:
            anchor = previous
            if not anchor:
                # A very small number of active iNaturalist taxa can refer to a
                # parent omitted from the monthly archive. Keep those ids
                # placeable under an explicit unplaced branch instead of
                # silently dropping them.
                if life_id:
                    base.setdefault(life_id, make_node(rows[life_id], None, nl, en))
                    anchor = life_id
                else:
                    anchor = "inat:life"
                    base.setdefault(anchor, synthetic_node(
                        anchor, "", "Leven", "Life", "ROOT"
                    ))
            order_id = f"inat:other-order:{anchor}"
            base.setdefault(order_id, synthetic_node(
                order_id, anchor, "Overige taxa", "Other taxa", "ORDER"
            ))

        family_id = nearest_rank(rows, ids, "family")
        if family_id:
            families.setdefault(family_id, make_node(rows[family_id], order_id, nl, en))
            families[family_id]["parentId"] = order_id
        else:
            family_id = f"inat:other-family:{order_id}"
            families.setdefault(family_id, synthetic_node(
                family_id, order_id, "Overige families", "Other families", "FAMILY"
            ))

        genus_id = nearest_rank(rows, ids, "genus")
        if genus_id:
            genera.setdefault(genus_id, make_node(rows[genus_id], family_id, nl, en))
            genera[genus_id]["parentId"] = family_id
        else:
            genus_id = f"inat:other-genus:{family_id}"
            genera.setdefault(genus_id, synthetic_node(
                genus_id, family_id, "Overige soorten", "Other species", "GENUS"
            ))

        species[sid] = make_node(rows[sid], genus_id, nl, en)
        species[sid].update({"speciesCount": 1, "weight": 1, "mapWeight": 1})
        species_order[sid] = order_id
        species_family[sid] = family_id

        if index % 100000 == 0:
            print(f"Prepared {index:,}/{len(species_ids):,} species", flush=True)

    genus_counts = defaultdict(int)
    family_counts = defaultdict(int)
    order_counts = defaultdict(int)
    for sid, sp in species.items():
        gid = str(sp["parentId"])
        fid = species_family[sid]
        oid = species_order[sid]
        genus_counts[gid] += 1
        family_counts[fid] += 1
        order_counts[oid] += 1

    for gid, node in genera.items():
        node["speciesCount"] = genus_counts.get(gid, 0)
        node["weight"] = node["mapWeight"] = max(1, node["speciesCount"])
    for fid, node in families.items():
        node["speciesCount"] = family_counts.get(fid, 0)
        node["weight"] = node["mapWeight"] = max(1, node["speciesCount"])
    for oid, count in order_counts.items():
        if oid in base:
            base[oid]["speciesCount"] = count
            base[oid]["inatAreaWeight"] = math.pow(max(1, count), math.log10(2.0))
            base[oid]["mapWeight"] = max(0.01, base[oid]["inatAreaWeight"])
            base[oid]["detailAvailable"] = True

    children = defaultdict(list)
    for node in base.values():
        if node.get("parentId") in base:
            children[str(node["parentId"])].append(str(node["id"]))

    memo: dict[str, float] = {}
    def signal(identifier: str) -> float:
        if identifier in memo:
            return memo[identifier]
        node = base[identifier]
        value = float(node.get("mapWeight", 0)) if node["rank"] == "ORDER" else sum(
            signal(child) for child in children.get(identifier, [])
        )
        memo[identifier] = max(0.01, value)
        return memo[identifier]

    for identifier, node in base.items():
        if node["rank"] != "ORDER":
            node["mapWeight"] = signal(identifier)

    staging = output.with_name(output.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    for name in ("orders", "families", "search"):
        (staging / name).mkdir(parents=True, exist_ok=True)

    by_order_families = defaultdict(list)
    by_order_genera = defaultdict(list)
    by_family_species = defaultdict(list)
    for node in families.values():
        by_order_families[str(node["parentId"])].append(node)
    for node in genera.values():
        fid = str(node["parentId"])
        oid = str(families[fid]["parentId"])
        by_order_genera[oid].append(node)
    for sid, node in species.items():
        by_family_species[species_family[sid]].append(node)

    search_parts = defaultdict(list)
    def add_search(node: dict, order_id: str, family_id: str | None = None, genus_id: str | None = None):
        aliases = []
        for value in (node.get("canonicalName"), node.get("vernacularNameNl"), node.get("vernacularNameEn")):
            value = norm_name(value)
            if value and value not in aliases:
                aliases.append(value)
        if not aliases:
            return
        row = {"n": aliases[0], "a": aliases, "id": str(node["id"]), "o": order_id, "r": node["rank"]}
        if family_id:
            row["f"] = family_id
        if genus_id:
            row["g"] = genus_id
        for alias in aliases:
            search_parts[search_prefix(alias)].append(row)

    order_manifest = {}
    for oid, count in order_counts.items():
        order_families = by_order_families[oid]
        order_genera = by_order_genera[oid]
        payload = {
            "meta": {
                "orderId": oid,
                "orderName": base[oid]["canonicalName"],
                "families": len(order_families),
                "genera": len(order_genera),
                "species": count,
                "taxonomy": "iNaturalist",
            },
            "taxa": order_families + order_genera,
        }
        (staging / "orders" / f"{safe_id(oid)}.json").write_text(compact_json(payload), encoding="utf-8")
        order_manifest[oid] = payload["meta"]
        for node in order_families:
            if not node.get("synthetic"):
                add_search(node, oid, str(node["id"]))
        for node in order_genera:
            if not node.get("synthetic"):
                add_search(node, oid, str(node["parentId"]), str(node["id"]))

    for fid, nodes in by_family_species.items():
        oid = str(families[fid]["parentId"])
        (staging / "families" / f"{safe_id(fid)}.json").write_text(compact_json({
            "meta": {"familyId": fid, "orderId": oid, "species": len(nodes), "taxonomy": "iNaturalist"},
            "taxa": nodes,
        }), encoding="utf-8")
        for node in nodes:
            add_search(node, oid, fid, str(node["parentId"]))

    for prefix, search_rows in search_parts.items():
        unique = {}
        for row in search_rows:
            unique[(row["id"], tuple(row["a"]))] = row
        ordered = sorted(unique.values(), key=lambda row: row["n"])
        (staging / "search" / f"{prefix}.json").write_text(compact_json(ordered), encoding="utf-8")

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    base_nodes = sorted(base.values(), key=lambda node: (RANK_ORDER.get(node["rank"].casefold(), 99), node["canonicalName"]))
    (staging / "taxa.json").write_text(compact_json({
        "meta": {
            "source": "iNaturalist Taxonomy Darwin Core Archive",
            "sourceUrl": DEFAULT_ARCHIVE,
            "generatedAt": generated_at,
            "taxonomyIdField": "iNaturalist taxon id",
            "nameLocales": ["nl", "en"],
            "taxa": len(base_nodes) + len(families) + len(genera) + len(species),
            "orders": len(order_counts),
            "detailTotals": {"families": len(families), "genera": len(genera), "species": len(species)},
        },
        "taxa": base_nodes,
    }), encoding="utf-8")
    manifest = {
        "generatedAt": generated_at,
        "method": "iNaturalist DwC-A, visible-rank collapse",
        "nameLocales": ["nl", "en"],
        "orders": order_manifest,
        "totals": {"orders": len(order_counts), "families": len(families), "genera": len(genera), "species": len(species)},
    }
    (staging / "detail_manifest.json").write_text(compact_json(manifest), encoding="utf-8")

    shutil.rmtree(output, ignore_errors=True)
    staging.rename(output)
    print("Generated", compact_json(manifest["totals"]), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE, help="Local DwC-A zip or URL")
    parser.add_argument("--output", type=Path, default=ROOT / "data")
    parser.add_argument("--max-species", type=int, default=None, help="Development smoke-test limit")
    args = parser.parse_args()

    archive_path, temp = acquire_archive(args.archive)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            rows = read_taxa(archive)
            visible = {identifier for identifier, row in rows.items() if row["rank"] in VISIBLE_RANKS}
            nl = choose_names(archive, "VernacularNames-dutch.csv", visible, "NL")
            en = choose_names(archive, "VernacularNames-english.csv", visible, None)
        build(rows, nl, en, args.output.resolve(), args.max_species)
    finally:
        if temp:
            temp.cleanup()


if __name__ == "__main__":
    main()
