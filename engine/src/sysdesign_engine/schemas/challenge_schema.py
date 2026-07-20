import re
import json
from pathlib import Path
from typing import Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from sysdesign_engine.schemas.components_schema import ComponentLibrary

EFFECT_LIST = ["store", "retrieve", "deliver", "mutate"]
ZIPF_RE = re.compile(r"^zipf\(\s*([0-9]*\.?[0-9]+)\s*\)$")
INVARIANT_2 = (
    "invariant 2: challenges never name components, capabilities, ops, or "
    "hints - contracts + workload + traffic + SLOs only"
)


#------------------------Challenge JSON error helper----------------------------------------------
class ChallengeError(Exception):
    """
    Raised when a challenge JSON fails validation or 
    leaks engine/libraryconcepts (component types, capabilities, topology) into the contract.
    in the future i could allow specific component types to have some settings, idk
    """



#-------------------------entities---------------------------------
class Entity(BaseModel):
    """Basically the request value(contains size, keyspace[distribution of data] )"""
    model_config = ConfigDict(extra="forbid")

    record_kb: float = Field(..., gt=0, description="KB per record") #size
    keyspace: int = Field(..., gt=0, description="distinct key count for this entity") #dist



#---------------------------APIs (contracts)----------------------
class ReadSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: str
    read_units: float = Field(1.0, gt=0)


class WriteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: str
    write_units: float = Field(1.0, gt=0)

class ComputeSpec(BaseModel):
    entity:str
    compute_units: float = Field(1.0,gt=0)


class APIContract(BaseModel):
    """One APIs contract. Two different types:

    - single: `effect` is {store, retrieve, deliver, mutate} , `entity` , ( `to` for deliver, `op`/`condition` for mutate).

    - composite: `reads`/`writes` lists , `compute_units`, a data-dependency chain (retrieve -> compute -> store). No top-level `entity`.

    `compute_units` on a simple store/retrieve contract is COST ONLY (charged at the first compute-capable node traversed, or not at all); 
    it becomes a routing GOAL only inside a composite contract.
    """
    model_config = ConfigDict(extra="forbid")

    #single
    effect: Optional[str] = None   
    entity: Optional[str] = None
    to: Optional[str] = Field(None, description="deliver only: recipient session role")
    op: Optional[str] = Field(None, description="mutate only: e.g. 'decrement'")
    condition: Optional[str] = Field(None, description="mutate only: e.g. 'counter > 0'")

    #composite/multiple diffs
    reads: Optional[List[ReadSpec]] = None
    writes: Optional[List[WriteSpec]] = None
    # computes: Optional [ComputeSpec] = None    #i can have this multi compute thing, but for mvp ill leave it as below 1 amount of compute for the api

    #single and if not defined in the challenge, defaults to 1 (no mult)
    compute_units: float = Field(1.0, ge=0.0)
    read_units: float = Field(1.0, gt=0)
    write_units: float = Field(1.0, gt=0)

    @model_validator(mode="after")
    def _shape(self) -> "APIContract":
        is_composite = self.reads is not None or self.writes is not None  #gonna have to add onto this if i ever add more composite types lol
        if is_composite:
            if self.effect is not None:
                raise ValueError("composite contracts (reads/writes) must not set 'effect'")
            if self.entity is not None:
                raise ValueError(
                    "composite contracts reference entities via reads[].entity / writes[].entity, not a top-level 'entity'")
            if not self.reads and not self.writes:
                raise ValueError("composite contract needs at least one of reads/writes")
            if self.to is not None or self.op is not None or self.condition is not None:
                raise ValueError("'to'/'op'/'condition' are not valid on composite contracts")
            return self

        if self.effect is None:
            raise ValueError("API must declare either 'effect' or reads/writes")
        if self.effect not in EFFECT_LIST:
            raise ValueError(f"'effect' must be one of {EFFECT_LIST}, got {self.effect!r}")
        if self.entity is None:
            raise ValueError(f"'{self.effect}' contract requires 'entity'")

        if self.effect == "deliver":
            if self.to is None:
                raise ValueError("'deliver' contract requires 'to'")
        elif self.to is not None:
            raise ValueError("'to' is only valid on 'deliver' contracts")

        if self.effect == "mutate":
            if self.op is None:
                raise ValueError("'mutate' contract requires 'op'")
        else:
            if self.op is not None or self.condition is not None:
                raise ValueError("'op'/'condition' are only valid on 'mutate' contracts")
            if self.writes is not None:
                raise ValueError(f"'writes' is not valid on a simple '{self.effect}' contract")

        return self

    def referenced_entities(self) -> set:
        """find all used entities, helps understand if theres an unused one or naw"""
        refs = set()
        if self.entity:
            refs.add(self.entity)
        for r in self.reads or []:
            refs.add(r.entity)
        for w in self.writes or []:
            refs.add(w.entity)
        return refs



#-----------------traffic--------------------------------
class Traffic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shape: str
    mix: Optional[Dict[str, float]] = None
    key_dist: Optional[str] = None
    key_dist_alpha: Optional[float] = None
    unknown_key_pct: float = Field(0.0, ge=0, le=1)

    # request_response
    rps: Optional[float] = Field(None, gt=0)

    # burst: [[t_sec, rps], ...]
    rps_profile: Optional[List[List[float]]] = None

    # session
    connect_rps: Optional[float] = Field(None, gt=0)
    session_duration_s: Optional[float] = Field(None, gt=0)
    messages_per_sec: Optional[float] = Field(None, gt=0)

    @model_validator(mode="after")
    def _shape_fields(self) -> "Traffic":
        if self.shape not in ("request_response", "burst", "session"):
            raise ValueError(
                f"traffic.shape must be one of ['request_response', 'burst', 'session'], got {self.shape!r}")

        if self.key_dist is not None:
            m = ZIPF_RE.match(self.key_dist)
            if not m:
                raise ValueError(f"key_dist {self.key_dist!r} must look like 'zipf(<alpha>)'")
            self.key_dist_alpha = float(m.group(1))

        if self.mix is not None:
            for name, weight in self.mix.items():
                if not (0.0 <= weight <= 1.0):
                    raise ValueError(f"traffic.mix[{name!r}] must be in [0, 1], got {weight}")
            total = sum(self.mix.values())
            if abs(total - 1.0) > 1e-6:
                raise ValueError(f"traffic.mix must sum to 1.0, got {total}")

        shape_fields = {
            "request_response": ("rps",),
            "burst": ("rps_profile",),
            "session": ("connect_rps", "session_duration_s", "messages_per_sec"),
        }
        required = shape_fields[self.shape]
        missing = [f for f in required if getattr(self, f) in (None, [])]
        if missing:
            raise ValueError(f"'{self.shape}' traffic requires: {missing}")

        other_shapes_fields = {f for s, fs in shape_fields.items() if s != self.shape for f in fs}
        set_from_others = [f for f in other_shapes_fields if getattr(self, f) not in (None, [])]
        if set_from_others:
            raise ValueError(
                f"'{self.shape}' traffic must not set fields from another shape: {set_from_others}")

        if self.shape == "burst":
            for point in self.rps_profile:
                if len(point) != 2:
                    raise ValueError("rps_profile entries must be [t_sec, rps] pairs")
                if point[1] <= 0:
                    raise ValueError("rps_profile rps values must be > 0")

        return self



#----------------------------SLOs (mixed dict: known global keys + per API keys keyed by API name)------------------------------------
class FreshnessSLO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    within_s: float = Field(..., gt=0)
    pct: float = Field(..., gt=0, le=1)


class PerAPISLO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p99_ms: float = Field(..., gt=0)
    completion_threshold: Optional[float] = Field(None, gt=0, le=1)


class SLOs(BaseModel):
    """
    known global keys alongside per-API keys
    (keyed by API name, e.g. "getShort": {"p99_ms": 100}). Extras are
    collected into `per_api` and cross-checked against declared APIs by
    ChallengeRecord."""
    model_config = ConfigDict(extra="allow")

    availability: Optional[float] = Field(None, gt=0, le=1)
    durability: Optional[float] = Field(None, gt=0, le=1)
    monthly_budget_usd: Optional[float] = Field(None, gt=0)
    oversell: Optional[int] = Field(None, ge=0)
    freshness: Optional[FreshnessSLO] = None
    completion_threshold: float = Field(0.999, gt=0, le=1)

    per_api: Dict[str, PerAPISLO] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def _collect_per_api(self) -> "SLOs":
        extras = dict(self.__pydantic_extra__ or {})
        per_api = {}
        for key, value in extras.items():
            try:
                per_api[key] = PerAPISLO.model_validate(value)
            except Exception as e:
                raise ValueError(f"slos.{key}: {e}") from e
        self.per_api = per_api
        return self



#--------------------final challenge record-----------------------
class ChallengeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: Dict[str, Entity]
    apis: Dict[str, APIContract]
    traffic: Traffic
    slos: SLOs

    @model_validator(mode="after")
    def _cross_validate(self) -> "ChallengeRecord":
        entity_names = set(self.entities)
        api_names = set(self.apis)

        for name, api in self.apis.items():
            missing = api.referenced_entities() - entity_names
            if missing:
                raise ValueError(f"api '{name}' references unknown entities {sorted(missing)}")

        if self.traffic.mix is not None:
            unknown = set(self.traffic.mix) - api_names
            if unknown:
                raise ValueError(f"traffic.mix references unknown apis {sorted(unknown)}")

        unknown_slo_apis = set(self.slos.per_api) - api_names
        if unknown_slo_apis:
            raise ValueError(f"slos reference unknown apis {sorted(unknown_slo_apis)}")

        return self

    @model_validator(mode="after")
    def _no_engine_leakage(self, info: ValidationInfo) -> "ChallengeRecord":
        context = info.context or {}
        library: Optional[ComponentLibrary] = context.get("library")
        if library is not None:
            check_no_engine_leakage(self.model_dump(), library)
        return self



def check_no_engine_leakage(raw: dict, library: ComponentLibrary) -> None:
    """Walk every string leaf and dict key in a challenge's raw data and
    reject any that name a component type from the library. This catches
    an author writing "postgres" as an entity/API name or anywhere else in
    free text -- the one thing challenge JSON may never do (SPEC invariant 2).
    """
    banned = {t.lower() for t in library.components}

    def walk(node, path: str):
        if isinstance(node, dict):
            for key, value in node.items():
                _check_token(key, f"{path}.{key}" if path else str(key))
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
        elif isinstance(node, str):
            _check_token(node, path)

    def _check_token(token: str, path: str) -> None:
        if token.lower() in banned:
            raise ChallengeError(
                f"challenge field '{path}' names a component type ('{token}') - challenges may not reference the component library ({INVARIANT_2})")

    walk(raw, "")


def load_challenge_dict(raw: dict, library: Optional[ComponentLibrary] = None) -> ChallengeRecord:
    context = {"library": library} if library is not None else None
    try:
        return ChallengeRecord.model_validate(raw, context=context)
    except ValueError as e:
        raise ChallengeError(str(e)) from e


def load_challenge(path: Union[str, Path],library: Optional[ComponentLibrary] = None) -> ChallengeRecord:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return load_challenge_dict(raw, library)
