"""dk1 daily history download shared by the trend run (#222) and the regime build (#223).

``download_daily`` fetches the ``daily__...`` and ``funding__...`` assets of one published
``dk1-SYMBOL-FIRST_LAST-rN`` release from the private research-data repo, sha256-verified
with the asset API digest or, when that is absent, the release manifest's sha256 (the same
fallback as ``label_build.download_inputs``). The files are complete ranges: rows of later
(for example hidden) months are on disk but the guarded loaders stop reading before them.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from experiment_run import PublicError  # noqa: E402
from label_build import download  # noqa: E402
from market_analysis import daily_lake as dk  # noqa: E402

DAILY_FIRST_MONTH = "2020-01"
DAILY_LAST_MONTH = "2026-09"
_MANIFEST_LIMIT = 16 << 20


def download_daily(repo, symbol: str, first_month: str, last_month: str, revision: int, dest: Path) -> dict:
    """Download one symbol's dk1 daily and funding files into ``dest``; returns their provenance."""
    tag = dk.release_tag(symbol, first_month, last_month, revision)
    release = repo.published_release(tag)
    if release is None or release.get("tag_name") != tag or release.get("draft") is not False:
        raise PublicError(f"missing published release: {tag}")
    assets = repo.assets(release["id"])
    names = {"daily": dk.daily_asset_name(symbol, first_month, last_month),
             "funding": dk.funding_asset_name(symbol, first_month, last_month)}
    missing = [name for name in names.values() if name not in assets]
    if missing:
        raise PublicError(f"{tag}: missing assets {', '.join(missing)}")
    checksums = {}
    if any(not assets[name].get("digest") for name in names.values()):
        manifest_name = dk.manifest_asset_name(symbol, first_month, last_month)
        if manifest_name not in assets:
            raise PublicError(f"{tag}: missing {manifest_name} for checksum fallback")
        manifest_path = Path(dest) / manifest_name
        download(repo, assets[manifest_name], manifest_path, limit=_MANIFEST_LIMIT)
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("release_tag") != tag or manifest.get("symbol") != symbol:
            raise PublicError(f"{tag}: manifest identity mismatch")
        for item in manifest.get("assets", []):
            if item["name"] in checksums:
                raise PublicError(f"{tag}: duplicate manifest asset name")
            checksums[item["name"]] = item["sha256"]
    provenance = {"tag": tag}
    for kind, name in names.items():
        expected = checksums.get(name)
        if not assets[name].get("digest") and expected is None:
            raise PublicError(f"{tag}: no sha256 for {name}")
        sha, _ = download(repo, assets[name], Path(dest) / name, expected)
        provenance[f"{kind}_sha256"] = sha
    return provenance
