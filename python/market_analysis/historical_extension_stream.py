"""One-pass study point adaptation and disposable extension output spools."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import tempfile

from .historical_replay_runtime import _canonical_bytes
from .historical_stage_records import StageRecords


def paired_study_points(prepared):
    if prepared.experiment_points is not None:
        yield from zip(prepared.replay_result.points, prepared.experiment_points)
        return
    from .experiments.market_state_common import validate_experiment_points
    for point in prepared.replay_result.points:
        experiment = point.experiment_point()
        validate_experiment_points((experiment,))
        if experiment.partition != prepared.study_phase:
            raise ValueError("extension point differs from frozen phase")
        yield point, experiment
    if not prepared.replay_result.points.validated_completion:
        raise ValueError("extension input did not finish spool validation")


@contextmanager
def temporary_points(points):
    from .historical_study_runtime import encode
    with tempfile.TemporaryDirectory(prefix="historical-extension-") as directory:
        path = Path(directory) / "points.jsonl"
        count, digest = 0, hashlib.sha256()
        with path.open("wb") as stream:
            for point in points:
                raw = _canonical_bytes(encode(point))
                stream.write(raw)
                digest.update(raw)
                count += 1
        yield StageRecords(str(path), count, digest.hexdigest())
