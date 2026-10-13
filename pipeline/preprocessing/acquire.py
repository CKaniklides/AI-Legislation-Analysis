"""
# Stage 1 — Acquire (L0 → L1), Dutch side

Downloads the raw source XML for a single Dutch instrument from the
Basis Wetten Bestand (BWB) on overheid.nl and stores it as layer L1 of the
corpus. EU instruments are acquired separately.

This is a manual, one-time acquisition per instrument, not a scheduled job.
The corpus is backed up by hand. Re-run whenever a new toestand appears.

## What it does

1. **Discover**: queries the overheid.nl SRU service (`x-connection=BWB`)
   for all toestanden (consolidated versions) of the given BWB-id. For each
   one it collects the title, the validity start date, and the URLs of the
   toestand XML and WTI XML. Results are de-duplicated by effective date and
   sorted oldest → newest.
2. **Download**: fetches the latest toestand by default, or all of them with
   `--all-toestanden`. Each file is compared by SHA-256 with what is already
   on disk and only written if it changed. Files are always re-downloaded;
   only the write is skipped.
3. **Log**: appends one JSON line per toestand to `fetch_log.jsonl`
   (timestamp, URLs, HTTP status, hash, size, written/unchanged/failed).

## Output layout

    raw/nl/<BWB-id>/
        fetch_log.jsonl
        <effective-date>/
            toestand.xml    # consolidated legal text
            wti.xml         # amendment history / metadata

## Usage

    python acquire.py BWBR0040940                    # latest toestand only
    python acquire.py BWBR0040940 --all-toestanden   # full version history

## Known limitations

- The SRU query returns at most 200 records and is not paginated.
  Instruments with more toestanden are silently truncated.
- Records without a start date collapse into one entry and are saved
  under a folder named `None`.
- Failed downloads are logged and reported on stderr but do not stop the run.
"""

import argparse
import hashlib
import json
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
_HERE = Path(__file__).resolve().parent
_PIPELINE = _HERE.parent
for _d in (_PIPELINE, _PIPELINE / "c1", _PIPELINE / "preprocessing", _PIPELINE / "c2"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

SRU_URL = "https://zoekservice.overheid.nl/sru/Search"
NS = {
    "srw": "http://www.loc.gov/zing/srw/",
    "gzd": "http://standaarden.overheid.nl/sru",
    "obwb": "http://standaarden.overheid.nl/bwb/terms/",
}

RAW_ROOT = Path(__file__).resolve().parent.parent.parent / "raw" / "nl"


def _fetch(url: str, timeout: int = 90) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "jip-ai-legislation/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def discover_toestanden(bwb_id: str) -> list[dict]:
    """Query the SRU endpoint; return one record per toestand with its dates and file locations."""
    query = f"dcterms.identifier=={bwb_id}"
    url = f"{SRU_URL}?operation=searchRetrieve&version=1.2&x-connection=BWB&query={query}&maximumRecords=200"
    status, body = _fetch(url)
    if status != 200:
        raise RuntimeError(f"SRU query failed for {bwb_id}: HTTP {status}")

    root = ET.fromstring(body)
    records = []
    for rec in root.iter("{http://www.loc.gov/zing/srw/}record"):
        gzd = rec.find(".//{http://standaarden.overheid.nl/sru}gzd")
        if gzd is None:
            continue
        title_el = gzd.find(".//{http://purl.org/dc/terms/}title")
        start_el = gzd.find(".//{http://standaarden.overheid.nl/bwb/terms/}geldigheidsperiode_startdatum")
        toestand_el = gzd.find(".//{http://standaarden.overheid.nl/bwb/terms/}locatie_toestand")
        wti_el = gzd.find(".//{http://standaarden.overheid.nl/bwb/terms/}locatie_wti")
        if toestand_el is None:
            continue
        records.append({
            "title": title_el.text if title_el is not None else None,
            "effective_date": start_el.text if start_el is not None else None,
            "toestand_url": toestand_el.text,
            "wti_url": wti_el.text if wti_el is not None else None,
        })
    # de-dupe by effective_date (SRU can return the same toestand more than once
    # across paginated windows) and sort oldest -> newest
    seen = {}
    for r in records:
        seen[r["effective_date"]] = r
    return sorted(seen.values(), key=lambda r: r["effective_date"] or "")


def _save(content: bytes, dest: Path) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == digest:
        return {"path": str(dest), "sha256": digest, "bytes": len(content), "status": "unchanged"}
    dest.write_bytes(content)
    return {"path": str(dest), "sha256": digest, "bytes": len(content), "status": "written"}


def acquire(bwb_id: str, all_toestanden: bool = False) -> None:
    toestanden = discover_toestanden(bwb_id)
    if not toestanden:
        print(f"[{bwb_id}] no toestanden found via SRU", file=sys.stderr)
        return
    targets = toestanden if all_toestanden else [toestanden[-1]]
    print(f"[{bwb_id}] {len(toestanden)} toestand(en) known; fetching {len(targets)}")

    fetch_log = []
    for t in targets:
        date = t["effective_date"]
        out_dir = RAW_ROOT / bwb_id / date
        entry = {"instrument": bwb_id, "effective_date": date,
                 "fetched_at": datetime.now(timezone.utc).isoformat(), "files": {}}

        status, body = _fetch(t["toestand_url"])
        if status == 200:
            entry["files"]["toestand"] = {"url": t["toestand_url"], "http_status": status,
                                           **_save(body, out_dir / "toestand.xml")}
        else:
            entry["files"]["toestand"] = {"url": t["toestand_url"], "http_status": status, "status": "failed"}
            print(f"[{bwb_id}] FAILED toestand {date}: HTTP {status}", file=sys.stderr)

        if t["wti_url"]:
            status, body = _fetch(t["wti_url"])
            if status == 200:
                entry["files"]["wti"] = {"url": t["wti_url"], "http_status": status,
                                          **_save(body, out_dir / "wti.xml")}
            else:
                entry["files"]["wti"] = {"url": t["wti_url"], "http_status": status, "status": "failed"}
                print(f"[{bwb_id}] FAILED wti {date}: HTTP {status}", file=sys.stderr)

        fetch_log.append(entry)
        print(f"[{bwb_id}] {date}: "
              f"toestand={entry['files'].get('toestand', {}).get('status')} "
              f"wti={entry['files'].get('wti', {}).get('status')}")

    log_path = RAW_ROOT / bwb_id / "fetch_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        for entry in fetch_log:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{bwb_id}] fetch log appended: {log_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("bwb_id")
    ap.add_argument("--all-toestanden", action="store_true",
                     help="fetch every historical toestand, not just the current one")
    args = ap.parse_args()
    acquire(args.bwb_id, args.all_toestanden)
