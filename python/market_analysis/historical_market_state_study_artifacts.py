"""Whitelisted typed, immutable Part-C artifact I/O (no arbitrary hydration)."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date
import hashlib
import os
from pathlib import Path
import tempfile
import types
from typing import Any, Union, get_args, get_origin, get_type_hints

from . import historical_market_state_study_features as f
from . import historical_market_state_study_evaluation as e
from . import historical_market_state_study_models as m
from . import historical_market_state_study_statistics as s
from .historical_market_state_study_execution import _read_json, StudyArtifactConflictError
from .historical_market_state_study_json import canonical_study_json
from .historical_market_state_study_adapter import ALL_CANDIDATE_IDENTITIES

ARTIFACT_VERSION = 'historical-market-state-part-c-artifact-v1'


@dataclass(frozen=True)
class TrackAReference:
    study_period_index: int
    utc_date: date
    phase: str
    period_report_sha256: str
    native_state_quality_summaries: tuple[dict, ...]
    summaries_sha256: str

    def __post_init__(self):
        f.FROZEN_PERIOD_ROSTER.require_period(self.study_period_index, self.utc_date, self.phase)
        f.require_sha256(self.period_report_sha256)
        if self.summaries_sha256 != hashlib.sha256(canonical_study_json(self.native_state_quality_summaries).encode()).hexdigest():
            raise ValueError('Track-A summaries hash mismatch')
        identities = tuple(tuple(r.get(k) for k in ('experiment_id', 'algorithm_version', 'config_version'))
                           for r in self.native_state_quality_summaries)
        if len(identities) != len(ALL_CANDIDATE_IDENTITIES) or set(identities) != set(ALL_CANDIDATE_IDENTITIES):
            raise ValueError('Track-A summary membership differs from the fixed registry')


@dataclass(frozen=True)
class ScientificTestReport:
    development_freeze_sha256: str
    validation_freeze_sha256: str
    authorization: e.TestAuthorizationFreeze
    upstream_provenance: f.UpstreamInputProvenance | None
    results: tuple[e.PrimaryTestResult, ...]
    holm: tuple[s.HolmMember, ...]
    development_results: tuple[e.DevelopmentConfigResult, ...]
    development_nominations: tuple[e.DevelopmentNomination, ...]
    validation_decisions: tuple[e.ValidationDecision, ...]
    final_family_summaries: tuple[e.FinalFamilyEvidenceSummary, ...]

    def __post_init__(self):
        if (self.development_freeze_sha256 != self.authorization.development_freeze_sha256
                or self.validation_freeze_sha256 != self.authorization.validation_freeze_sha256
                or self.holm != e.study_primary_holm(self.authorization, self.results)):
            raise ValueError('test report parent/Holm identity mismatch')
        authorized = {a.family_id for a in self.authorization.members if a.status == 'AUTHORIZED'}
        if {r.family_id for r in self.results} != authorized:
            raise ValueError('test report must represent every authorized family')
        if authorized:
            if (self.upstream_provenance is None or self.upstream_provenance.phase != 'test'
                    or self.upstream_provenance.shared_contract != self.authorization.shared_contract
                    or any(r.upstream_provenance_sha256 != self.upstream_provenance.provenance_sha256 for r in self.results)):
                raise ValueError('test report provenance mismatch')
        elif self.upstream_provenance is not None:
            raise ValueError('no-authorized-members report must not open test provenance')
        nominations = {n.family_id: n for n in self.development_nominations}
        decisions = {d.identity.family_id: d for d in self.validation_decisions}
        if (len(nominations) != len(self.development_nominations)
                or set(nominations) != set(f.PRIMARY_CONFIRMATORY_FAMILY)
                or len(decisions) != len(self.validation_decisions)):
            raise ValueError('test report phase summaries have incomplete/duplicate membership')
        for member in self.authorization.members:
            if member.nomination_sha256 != nominations[member.family_id].nomination_sha256:
                raise ValueError('test report development summary differs from authorization')
            refs = tuple(sorted((r.identity.config_version, r.result_sha256)
                for r in self.development_results if r.identity.family_id == member.family_id))
            if refs != nominations[member.family_id].development_result_refs:
                raise ValueError('test report development results differ from frozen nomination')
            decision = decisions.get(member.family_id)
            if (member.validation_decision_sha256 is not None
                    and (decision is None or decision.decision_sha256 != member.validation_decision_sha256)):
                raise ValueError('test report validation summary differs from authorization')
        if ({r.identity.family_id for r in self.development_results} != set(f.PRIMARY_CONFIRMATORY_FAMILY)
                or len({r.identity for r in self.development_results}) != len(self.development_results)):
            raise ValueError('test report development result membership mismatch')
        if self.final_family_summaries != e.final_family_evidence_summaries(
                self.authorization, self.development_results, self.development_nominations,
                self.validation_decisions, self.results):
            raise ValueError('test report final classifications differ from the complete frozen evidence family')


# Explicit closed schema registry. Artifact-controlled names never import code.
_TYPES = (
    f.FrozenPeriodRoster, f.FinalizedPeriodProvenance, f.PartBScientificContract,
    f.UpstreamInputProvenance, f.FeatureSpec, f.FamilyEvaluationSpec,
    f.LayerOneStratifierSpec, f.StudyEvaluationPlan, f.AlignedStudyObservation,
    f.AlignedStudyDay, f.EvidenceValueDay, f.TercileSpec,
    m.FrozenStandardizer, m.FrozenLinearModel, m.FrozenLogisticModel,
    e.CandidateConfigIdentity, e.DayEligibility, e.PhaseCoverage, e.ConfigDevelopmentSample,
    e.ExcludedDevelopmentDay, e.CommonDevelopmentConfigSelection,
    e.CommonDevelopmentDaySelection, e.CommonDevelopmentSourceDay, e.CommonDevelopmentSamples,
    e.DayPredictiveResult, e.DevelopmentFold, e.DevelopmentConfigResult,
    e.DevelopmentNomination, e.FrozenPredictivePair, e.LayerOneContinuousEvidence,
    e.FrozenLayerOneBins, e.DevelopmentFreeze, e.ValidationDecision, e.ValidationFreeze,
    e.TestAuthorizationMember, e.TestAuthorizationFreeze, e.PrimaryTestResult, e.FinalFamilyEvidenceSummary,
    s.BootstrapNamespace, s.DayBootstrapResult, s.DaySignTestResult, s.HolmMember,
    TrackAReference, ScientificTestReport,
)
_SCHEMA = {t.__name__: t for t in _TYPES}
_ROOTS = {'development': e.DevelopmentFreeze, 'validation': e.ValidationFreeze,
          'authorization': e.TestAuthorizationFreeze, 'test': ScientificTestReport}


def encode(value):
    if isinstance(value, s.EvidenceClassification):
        return {'$classification': value.value}
    if is_dataclass(value) and not isinstance(value, type):
        if type(value) not in _TYPES:
            raise TypeError('unregistered scientific type')
        return {'$type': type(value).__name__, 'fields': {p.name: encode(getattr(value, p.name)) for p in fields(value)}}
    if type(value) is date:
        return {'$date': value.isoformat()}
    if isinstance(value, tuple):
        return {'$tuple': [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        if any(type(k) is not str for k in value):
            raise TypeError('artifact metadata requires string keys')
        return {'$map': {k: encode(v) for k, v in value.items()}}
    # Canonical serializer validates primitives and nonfinite numbers.
    canonical_study_json(value)
    if value is not None and type(value) not in (str, bool, int, float):
        raise TypeError('unsupported typed scientific value')
    return value


def _matches(value, annotation):
    if annotation is Any:
        return True
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        return any(_matches(value, a) for a in args)
    if origin is tuple:
        if type(value) is not tuple:
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches(v, args[0]) for v in value)
        return len(value) == len(args) and all(_matches(v, a) for v, a in zip(value, args))
    if annotation is float:
        return type(value) in (float, int)
    if annotation in (dict, tuple, str, int, bool, date, type(None)):
        return type(value) is annotation
    return isinstance(value, annotation)


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        canonical_study_json(value)
        return value
    if set(value) == {'$tuple'} and isinstance(value['$tuple'], list):
        return tuple(decode(v) for v in value['$tuple'])
    if set(value) == {'$date'}:
        return date.fromisoformat(value['$date'])
    if set(value) == {'$classification'}:
        return s.EvidenceClassification(value['$classification'])
    if set(value) == {'$map'} and isinstance(value['$map'], dict):
        return {k: decode(v) for k, v in value['$map'].items()}
    if set(value) != {'$type', 'fields'} or value['$type'] not in _SCHEMA:
        raise ValueError('unknown typed artifact schema')
    cls = _SCHEMA[value['$type']]
    data = value['fields']
    if not isinstance(data, dict) or set(data) != {p.name for p in fields(cls)}:
        raise ValueError('typed artifact field membership mismatch')
    decoded = {k: decode(v) for k, v in data.items()}
    annotations = get_type_hints(cls)
    if any(not _matches(v, annotations[k]) for k, v in decoded.items()):
        raise ValueError('typed scientific field type mismatch')
    reconstructed = cls(**{p.name: decoded[p.name] for p in fields(cls) if p.init})
    if encode(reconstructed) != value:
        raise ValueError('scientific inner hash/derived identity mismatch')
    return reconstructed


def _validate_metadata(kind, value, metadata):
    if any(type(v) is not tuple for v in metadata.values()) or any(type(t) is not TrackAReference for t in metadata['track_a']):
        raise ValueError('artifact metadata schema mismatch')
    references = metadata['track_a']
    if any(replace(t) != t for t in references):
        raise ValueError('invalid Track-A reference identity')
    if kind in ('development', 'validation'):
        expected = tuple((p.study_period_index, p.utc_date, p.phase, p.period_report_sha256)
                         for p in value.upstream_provenance.periods)
        if tuple((t.study_period_index, t.utc_date, t.phase, t.period_report_sha256) for t in references) != expected:
            raise ValueError('artifact Track-A references must bind the complete consumed phase roster')
    elif kind == 'authorization':
        if any(metadata.values()):
            raise ValueError('authorization contains no newly consumed period evidence')
    else:
        phases = ('development', 'validation', 'test') if value.upstream_provenance is not None else ('development', 'validation')
        expected = tuple(p for p in f.FROZEN_PERIOD_ROSTER.periods if p[2] in phases)
        if tuple((t.study_period_index, t.utc_date, t.phase) for t in references) != expected:
            raise ValueError('test report Track-A roster differs from the opened phases')
        if value.upstream_provenance is not None and tuple(
                t.period_report_sha256 for t in references if t.phase == 'test') != tuple(
                p.period_report_sha256 for p in value.upstream_provenance.periods):
            raise ValueError('test Track-A references differ from exact test report provenance')


def artifact_body(kind, value, *, track_a=(), layer_one=(), exclusions=()):
    if kind not in _ROOTS or type(value) is not _ROOTS[kind]:
        raise ValueError('artifact root type mismatch')
    _validate_metadata(kind, value, {'track_a': tuple(track_a), 'layer_one': tuple(layer_one), 'exclusions': tuple(exclusions)})
    encoded = encode(value)
    if decode(encoded) != value:
        raise ValueError('artifact root has an invalid reconstructed scientific identity')
    body = {'artifact_version': ARTIFACT_VERSION, 'kind': kind,
            'study_manifest_sha256': f.FROZEN_STUDY_MANIFEST_SHA256,
            'plan_sha256': f.FROZEN_EVALUATION_PLAN.plan_sha256,
            'value': encoded, 'track_a': encode(tuple(track_a)),
            'layer_one': encode(tuple(layer_one)), 'exclusions': encode(tuple(exclusions))}
    body['artifact_sha256'] = hashlib.sha256(canonical_study_json(body).encode()).hexdigest()
    return body


def read_artifact(path, kind):
    body = _read_json(Path(path))
    required = {'artifact_version', 'kind', 'study_manifest_sha256', 'plan_sha256',
                'value', 'track_a', 'layer_one', 'exclusions', 'artifact_sha256'}
    if not isinstance(body, dict) or set(body) != required:
        raise ValueError('artifact envelope field mismatch')
    digest = hashlib.sha256(canonical_study_json({k: v for k, v in body.items() if k != 'artifact_sha256'}).encode()).hexdigest()
    if (digest != body['artifact_sha256'] or body['artifact_version'] != ARTIFACT_VERSION
            or body['kind'] != kind or body['plan_sha256'] != f.FROZEN_EVALUATION_PLAN.plan_sha256
            or body['study_manifest_sha256'] != f.FROZEN_STUDY_MANIFEST_SHA256):
        raise StudyArtifactConflictError('Part-C artifact scientific identity mismatch')
    value = decode(body['value'])
    if type(value) is not _ROOTS[kind]:
        raise ValueError('artifact root schema mismatch')
    metadata = {k: decode(body[k]) for k in ('track_a', 'layer_one', 'exclusions')}
    _validate_metadata(kind, value, metadata)
    return value, metadata


def write_artifact(path, kind, value, **metadata):
    """Atomic create-only publication; identical canonical content is safe resume."""
    path = Path(path)
    content = canonical_study_json(artifact_body(kind, value, **metadata)) + '\n'
    if path.exists():
        read_artifact(path, kind)
        if path.read_text(encoding='utf-8') != content:
            raise StudyArtifactConflictError('different scientific artifact already exists')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content.encode())
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication cannot overwrite a competing scientific writer.
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            if path.read_text(encoding='utf-8') != content:
                raise StudyArtifactConflictError('scientific artifact publication conflict') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
