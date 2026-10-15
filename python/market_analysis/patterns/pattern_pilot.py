"""Blind doji pilot: seeded stratified samples for visual inspection of the detector (docs/pattern-study.md,
"Doji pilot"), and the defect statistics of the annotations.

The pilot audits the DETECTOR, not the outcome: it shows what the event definition (doji-geo-v2:
``body_ratio <= 0.10`` and ``range_atr >= 0.5``) selects, what it just misses, and what ordinary
same-size candles look like. It computes no return and no outcome.

Classes (disjoint, all with ``range_atr >= 0.5`` and a usable ATR/sigma, inside the development segments):

- ``event``: ``body_ratio <= 0.10``, stratified by subtype x strength bucket (body <= 0.05 | 0.05-0.10) x
  ``range_atr`` tercile (terciles of the events in scope);
- ``control``: size-matched non-doji, ``body_ratio > 0.15`` (disjoint from the near-misses), sampled per
  event ``range_atr`` tercile, ``CONTROL_FACTOR`` x per_stratum each;
- ``near_miss``: ``0.10 < body_ratio <= 0.15``, to audit the boundary, per tercile, ``NEAR_MISS_FACTOR`` x
  per_stratum each.

A stratum with fewer rows than requested yields all of them: counts per class and stratum (requested,
available, sampled) are always reported and a sample is never padded.

Blind mode: 40 candles of context ending AT the signal candle (nothing after it), no outcome, no detector
label, opaque ids. The key (id -> row, class, stratum, detector fields) is a separate file. Open mode shows
12 candles after the signal and the fields in the title.
"""
from __future__ import annotations

import csv
import hashlib
import io
from math import isfinite
from zlib import crc32

import numpy as np

from ..benchmark.canonical import content_hash
from . import pattern_explore as pe
from .pattern_study import wilson

SCHEMA = "pattern-pilot-v1"
REPORT_ROOT = "reports/pattern-pilot"
CLASSES = ("event", "control", "near_miss")
BODY_EVENT_MAX = 0.10
BODY_STRONG_MAX = 0.05
BODY_NEAR_MAX = 0.15
RANGE_ATR_MIN = 0.5
CONTROL_FACTOR = 4
NEAR_MISS_FACTOR = 2
BLIND_CONTEXT = 40       # candles shown, the last one is the signal candle
OPEN_AFTER = 12
DEFAULT_SEED = 20261019
ANNOTATION_FIELDS = ("id", "detector_ok", "defect_type", "visual_strength", "notes")
DETECTOR_OK = ("y", "n", "unsure")
DEFECT_TYPES = ("not_doji", "wrong_subtype", "tiny_range", "data_artifact", "gap_artifact", "other")
VISUAL_STRENGTH = ("strong", "ok", "weak")
KEY_FIELDS = ("id", "symbol", "tf", "open_ms", "decision_ms", "class", "stratum", "body_ratio", "range_atr",
              "subtype")
MODES = ("blind", "open")


class PilotError(ValueError):
    """Invalid pilot input (months, mode, annotation values)."""


# ---------------------------------------------------------------- classes and strata


def class_masks(f: dict, window=None) -> dict:
    """{class: bool mask} over the frame rows; ``window`` = (first_ms, end_ms) on the candle open."""
    pop = pe.population(f)
    if window is not None:
        pop = pop & (f["open_ms"] >= window[0]) & (f["open_ms"] < window[1])
    with np.errstate(invalid="ignore"):
        body, size = f["body_ratio"], f["range_atr"]
        sized = pop & (size >= RANGE_ATR_MIN)
        return {"event": sized & (body <= BODY_EVENT_MAX),
                "near_miss": sized & (body > BODY_EVENT_MAX) & (body <= BODY_NEAR_MAX),
                "control": sized & (body > BODY_NEAR_MAX)}


def tercile_edges(f: dict, masks: dict) -> tuple:
    """range_atr tercile edges of the events in scope (all three classes pooled when fewer than 3 events)."""
    pool = masks["event"]
    if pool.sum() < 3:
        pool = masks["event"] | masks["near_miss"] | masks["control"]
    values = f["range_atr"][pool]
    return tuple(float(v) for v in np.quantile(values, (1 / 3, 2 / 3))) if values.size >= 3 else (np.inf, np.inf)


def stratum_label(f: dict, row: int, cls: str, edges: tuple) -> str:
    tercile = f"t{int(np.digitize(f['range_atr'][row], edges)) + 1}"
    if cls != "event":
        return tercile
    strength = "b<=0.05" if f["body_ratio"][row] <= BODY_STRONG_MAX else "b0.05-0.10"
    return f"{f['doji_subtype'][row] or 'none'}|{strength}|{tercile}"


def _generator(seed: int, tf: int, cls: str, stratum: str) -> np.random.Generator:
    return np.random.default_rng([seed, int(tf), crc32(f"{cls}|{stratum}".encode())])


def sample_frame(f: dict, tf: int, *, per_stratum: int, seed: int, window=None, scope=None,
                 stratify: bool = True) -> dict:
    """Seeded stratified sample of one timeframe's frame.

    Returns {"samples": [{row, class, stratum}], "counts": [{class, stratum, requested, available, sampled}]}.
    ``scope`` restricts the events (a slice mask over the rows); with ``stratify=False`` the events form one
    stratum "all" (a slice pack)."""
    if per_stratum < 1:
        raise PilotError("per_stratum must be at least 1")
    masks = class_masks(f, window)
    if scope is not None:
        masks["event"] = masks["event"] & scope
    edges = tercile_edges(f, masks)
    samples, counts = [], []
    for cls in CLASSES:
        groups = {}
        for row in np.flatnonzero(masks[cls]):
            label = "all" if cls == "event" and not stratify else stratum_label(f, int(row), cls, edges)
            groups.setdefault(label, []).append(int(row))
        factor = {"event": 1, "control": CONTROL_FACTOR, "near_miss": NEAR_MISS_FACTOR}[cls]
        wanted = per_stratum * factor if (stratify or cls != "event") else per_stratum
        for label in sorted(groups):
            pool = groups[label]
            take = min(wanted, len(pool))
            chosen = sorted(_generator(seed, tf, cls, label).choice(pool, take, replace=False).tolist())
            samples += [{"row": row, "class": cls, "stratum": label} for row in chosen]
            counts.append({"class": cls, "stratum": label, "requested": wanted, "available": len(pool),
                           "sampled": take})
    return {"samples": samples, "counts": counts, "tercile_edges": [float(v) for v in edges]}


def opaque_ids(f: dict, tf: int, samples: list, seed: int) -> list:
    """'P' + 8 hex of sha256(seed|symbol|tf|open_ms): independent of class and order; collisions are an error."""
    ids = ["P" + hashlib.sha256(f"{seed}|{f['symbol'][s['row']]}|{tf}|{int(f['open_ms'][s['row']])}".encode())
           .hexdigest()[:8] for s in samples]
    if len(set(ids)) != len(ids):
        raise PilotError("opaque id collision")
    return ids


# ---------------------------------------------------------------- rendering and files


def _fields(f: dict, row: int) -> dict:
    return {"body_ratio": f"{f['body_ratio'][row]:.4f}", "range_atr": f"{f['range_atr'][row]:.4f}",
            "subtype": f["doji_subtype"][row] or ""}


def key_rows(f: dict, tf: int, samples: list, ids: list) -> list:
    return [{"id": i, "symbol": f["symbol"][s["row"]], "tf": tf, "open_ms": int(f["open_ms"][s["row"]]),
             "decision_ms": int(f["decision_ms"][s["row"]]), "class": s["class"], "stratum": s["stratum"],
             **_fields(f, s["row"])} for s, i in zip(samples, ids)]


def _csv(rows: list, fields) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row.get(k, "") for k in fields])
    return out.getvalue()


def contact_sheet(f: dict, tf: int, samples: list, ids: list, mode: str, title: str) -> str:
    """HTML contact sheet. Blind: 40 candles ending at the signal candle, id-only title. Open: 12 candles after
    and the fields in the title. Charts are ordered by opaque id (hash order, unrelated to class)."""
    if mode not in MODES:
        raise PilotError(f"mode must be one of {MODES}")
    after = OPEN_AFTER if mode == "open" else 0
    head = [f"<!doctype html><meta charset='utf-8'><title>{pe._escape(title)}</title>",
            f"<h1>{pe._escape(title)}</h1>",
            "<p>Judge each chart on its own: is the highlighted (last) candle a doji of the stated kind? "
            "Fill annotations.csv by id.</p>" if mode == "blind" else
            "<p>Open mode: the class, the detector fields and 12 candles after the signal are shown.</p>",
            "<div style='display:flex;flex-wrap:wrap;gap:8px'>"]
    body = []
    for sample, ident in sorted(zip(samples, ids), key=lambda pair: pair[1]):
        row = sample["row"]
        label = ident if mode == "blind" else (f"{ident} {sample['class']} {sample['stratum']} "
                                               + " ".join(f"{k}={v}" for k, v in _fields(f, row).items()))
        body.append(pe.candle_svg(f, row, BLIND_CONTEXT - 1, after, label))
    return "\n".join(head + body + ["</div>"]) + "\n"


def pack_files(f: dict, tf: int, picked: dict, seed: int, mode: str, title: str, prefix: str) -> dict:
    """{prefix.html, prefix_key.csv, prefix_annotations.csv} of one sampled pack (the key is a separate file)."""
    ids = opaque_ids(f, tf, picked["samples"], seed)
    return {f"{prefix}.html": contact_sheet(f, tf, picked["samples"], ids, mode, title),
            f"{prefix}_key.csv": _csv(sorted(key_rows(f, tf, picked["samples"], ids), key=lambda r: r["id"]),
                                      KEY_FIELDS),
            f"{prefix}_annotations.csv": _csv([{"id": i} for i in sorted(ids)], ANNOTATION_FIELDS)}


def window_ms(from_month: str, to_month: str) -> tuple:
    from .. import data_lake

    if from_month > to_month:
        raise PilotError("from_month is after to_month")
    return data_lake.month_bounds_ms(from_month)[0], data_lake.month_bounds_ms(to_month)[1]


def check_window(timeframes, from_month: str, to_month: str, segments_for) -> tuple:
    """(first_ms, end_ms) after checking it lies inside the development segments of every timeframe.

    ``segments_for(tf)`` -> ((name, first_ms, end_ms), ...) restricted to the development segments."""
    first, end = window_ms(from_month, to_month)
    for tf in timeframes:
        segments = segments_for(tf)
        if not segments or first < min(s[1] for s in segments) or end > max(s[2] for s in segments):
            raise PilotError(f"{from_month}..{to_month} is not inside the development segments of {tf}m")
    return first, end


def build_pilot(frames: dict, *, symbol: str, window: tuple, per_stratum: int, seed: int, mode: str) -> dict:
    """{"files": {name: text}, "counts": [...], "ids": n} for ``frames`` = {tf: frame} (one symbol)."""
    if mode not in MODES:
        raise PilotError(f"mode must be one of {MODES}")
    files, counts, keys, annotations = {}, [], [], []
    for tf in sorted(frames):
        f = frames[tf]
        picked = sample_frame(f, tf, per_stratum=per_stratum, seed=seed, window=window)
        ids = opaque_ids(f, tf, picked["samples"], seed)
        files[f"pilot_{tf}.html"] = contact_sheet(f, tf, picked["samples"], ids, mode,
                                                  f"Doji pilot {symbol} {tf}m ({mode})")
        keys += key_rows(f, tf, picked["samples"], ids)
        annotations += [{"id": i} for i in sorted(ids)]
        counts += [{"tf": tf, **row} for row in picked["counts"]]
    files["pilot_key.csv"] = _csv(sorted(keys, key=lambda r: (r["tf"], r["id"])), KEY_FIELDS)
    files["annotations.csv"] = _csv(annotations, ANNOTATION_FIELDS)
    return {"files": files, "counts": counts, "ids": len(keys)}


def manifest(*, symbol: str, table_report: str, table_hash: str, window: tuple, per_stratum: int, seed: int,
             mode: str, counts: list, files: dict, code_commit: str, created_utc: str) -> dict:
    body = {"schema": SCHEMA, "symbol": symbol, "table_report": table_report, "table_hash": table_hash,
            "window_ms": list(window), "per_stratum": per_stratum, "seed": seed, "mode": mode,
            "control_factor": CONTROL_FACTOR, "near_miss_factor": NEAR_MISS_FACTOR,
            "classes": {"event": f"body_ratio <= {BODY_EVENT_MAX}, range_atr >= {RANGE_ATR_MIN}",
                        "near_miss": f"{BODY_EVENT_MAX} < body_ratio <= {BODY_NEAR_MAX}, range_atr >= {RANGE_ATR_MIN}",
                        "control": f"body_ratio > {BODY_NEAR_MAX}, range_atr >= {RANGE_ATR_MIN}"},
            "counts": counts, "files": sorted(files),
            "file_sha256": {name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                            for name, text in sorted(files.items())}}
    body["pilot_hash"] = content_hash(body)
    return {**body, "code_commit": code_commit, "created_utc": created_utc}


def pilot_dir(manifest_: dict) -> str:
    return f"{REPORT_ROOT}/{manifest_['pilot_hash'][:16]}"


def public_lines(manifest_: dict) -> list:
    """Counts only (per timeframe and class: sampled / available), the private path and the hash."""
    lines = [f"doji pilot {manifest_['schema']} symbol {manifest_['symbol']} mode {manifest_['mode']} "
             f"seed {manifest_['seed']} per_stratum {manifest_['per_stratum']}"]
    tally = {}
    for row in manifest_["counts"]:
        item = tally.setdefault((row["tf"], row["class"]), [0, 0])
        item[0] += row["sampled"]
        item[1] += row["available"]
    for (tf, cls), (sampled, available) in sorted(tally.items()):
        lines.append(f"tf={int(tf)} class={cls} sampled={sampled} available={available}")
    return lines + [f"private path {pilot_dir(manifest_)}/", f"pilot hash {manifest_['pilot_hash']}"]


# ---------------------------------------------------------------- defect statistics


def _read(text: str) -> list:
    return list(csv.DictReader(io.StringIO(text)))


def read_key(text: str) -> dict:
    rows = _read(text)
    if rows and set(KEY_FIELDS) - set(rows[0]):
        raise PilotError("pilot_key.csv lacks columns " + ", ".join(sorted(set(KEY_FIELDS) - set(rows[0]))))
    out = {}
    for row in rows:
        if row["id"] in out:
            raise PilotError(f"duplicate id {row['id']} in the key")
        out[row["id"]] = row
    return out


def read_annotations(text: str, key: dict) -> list:
    """Annotated rows (detector_ok filled), validated against the allowed values and the key."""
    rows = _read(text)
    if rows and set(ANNOTATION_FIELDS) - set(rows[0]):
        raise PilotError("annotations.csv lacks columns " + ", ".join(sorted(set(ANNOTATION_FIELDS) - set(rows[0]))))
    seen, out = set(), []
    for row in rows:
        ident = row["id"]
        if ident not in key:
            raise PilotError(f"annotation id {ident} is not in the key")
        if ident in seen:
            raise PilotError(f"duplicate annotation id {ident}")
        seen.add(ident)
        ok = (row["detector_ok"] or "").strip().lower()
        if not ok:
            continue
        if ok not in DETECTOR_OK:
            raise PilotError(f"{ident}: detector_ok must be one of {DETECTOR_OK}")
        defect = (row["defect_type"] or "").strip().lower()
        strength = (row["visual_strength"] or "").strip().lower()
        if defect and defect not in DEFECT_TYPES:
            raise PilotError(f"{ident}: defect_type must be one of {DEFECT_TYPES}")
        if strength and strength not in VISUAL_STRENGTH:
            raise PilotError(f"{ident}: visual_strength must be one of {VISUAL_STRENGTH}")
        out.append({**key[ident], "detector_ok": ok, "defect_type": defect, "visual_strength": strength})
    return out


def _rate(rows: list) -> dict:
    """Defect rate = n / (y + n); 'unsure' is counted apart and excluded from the rate."""
    y = sum(r["detector_ok"] == "y" for r in rows)
    n = sum(r["detector_ok"] == "n" for r in rows)
    unsure = sum(r["detector_ok"] == "unsure" for r in rows)
    decided = y + n
    return {"annotated": len(rows), "ok": y, "defect": n, "unsure": unsure,
            "rate": n / decided if decided else float("nan"), "wilson95": wilson(n, decided)}


def defect_tables(key: dict, rows: list) -> dict:
    """Defect rate by class, timeframe, class x timeframe and stratum (n and Wilson 95%), the defect types, and
    the visual_strength split. It reports only; there is no pass threshold."""
    def group(keyfunc):
        parts = {}
        for r in rows:
            parts.setdefault(keyfunc(r), []).append(r)
        return {k: _rate(v) for k, v in sorted(parts.items())}

    types = {}
    for r in rows:
        if r["detector_ok"] == "n":
            types.setdefault(r["class"], {}).setdefault(r["defect_type"] or "unspecified", 0)
            types[r["class"]][r["defect_type"] or "unspecified"] += 1
    strength = {}
    for r in rows:
        bucket = strength.setdefault((r["class"], r["visual_strength"] or "unrated"), [])
        bucket.append(r)
    return {"key_ids": len(key), "annotated": len(rows), "overall": _rate(rows),
            "by_class": group(lambda r: r["class"]), "by_timeframe": group(lambda r: f"{int(r['tf'])}"),
            "by_class_timeframe": group(lambda r: f"{r['class']}|{int(r['tf'])}"),
            "by_stratum": group(lambda r: f"{r['class']}|{int(r['tf'])}|{r['stratum']}"),
            "defect_types": types,
            "by_visual_strength": {f"{c}|{s}": _rate(v) for (c, s), v in sorted(strength.items())}}


def _fmt(rate: dict) -> str:
    lo, hi = rate["wilson95"]
    shown = "n/a" if not isfinite(rate["rate"]) else f"{100 * rate['rate']:.1f}%"
    ci = "n/a" if not isfinite(lo) else f"{100 * lo:.1f}-{100 * hi:.1f}%"
    return f"{shown} [{ci}]"


def defect_markdown(tables: dict) -> list:
    lines = [f"Annotated {tables['annotated']} of {tables['key_ids']} ids. Defect rate = n / (y + n), 'unsure' "
             "counted apart and excluded; Wilson 95% intervals. No threshold is applied.", ""]
    for name, title in (("by_class", "class"), ("by_timeframe", "timeframe"), ("by_class_timeframe", "class|tf"),
                        ("by_stratum", "class|tf|stratum"), ("by_visual_strength", "class|visual_strength")):
        lines += [f"### Defect rate by {title}", "",
                  f"| {title} | annotated | ok | defect | unsure | rate [Wilson95] |", "|---|---:|---:|---:|---:|---|"]
        for key_, rate in tables[name].items():
            lines.append(f"| {key_} | {rate['annotated']} | {rate['ok']} | {rate['defect']} | {rate['unsure']} | "
                         f"{_fmt(rate)} |")
        lines.append("")
    if tables["defect_types"]:
        lines += ["### Defect types (detector_ok = n)", ""]
        lines += [f"- {cls}: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
                  for cls, kinds in sorted(tables["defect_types"].items())]
    return lines


__all__ = ["SCHEMA", "CLASSES", "class_masks", "sample_frame", "opaque_ids", "contact_sheet", "build_pilot",
           "manifest", "pilot_dir", "public_lines", "read_key", "read_annotations", "defect_tables",
           "defect_markdown", "check_window"]
