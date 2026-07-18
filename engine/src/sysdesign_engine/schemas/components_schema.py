from enum import Enum
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

CAPABILITY_LIST = Literal["store", "compute", "route", "buffer", "client"]

# service_times keys each capability must provide
REQUIRED_OPS: Dict[str, set] = {
    "store":   {"read", "write"},   #caches,dbs
    "compute": {"compute"},         #app servers
    "route":   {"forward"},         #load balancers and other things idk
    "buffer":  {"read", "write"},   # enqueue / drain
    "client":  set(),
}


class OpPerformance(BaseModel):
    """Service-time distribution for one op (read/write/compute/forward).

    How long the op takes at this component, not which keys arrive.
    Key popularity (zipf etc.) is challenge traffic, not library.
    """
    model_config = ConfigDict(extra="forbid")

    dist: Literal["lognormal", "exponential", "constant"] = "lognormal"
    p50: float = Field(..., gt=0, description="median latency, ms")
    p99: float = Field(..., gt=0, description="99th percentile latency, ms")
    per_kb_time: float = Field(0.0, ge=0, description="additional ms per KB")

    @model_validator(mode="after")
    def _p50_below_p99(self) -> "OpPerformance":
        if self.dist != "constant" and self.p50 >= self.p99:
            raise ValueError(
                f"p50 ({self.p50}ms) must be strictly below p99 ({self.p99}ms)"
            )
        return self


class CostModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_request: float = Field(0.0, ge=0)
    per_hour: float = Field(0.0, ge=0)
    per_million_req: float = Field(0.0, ge=0)
    per_gb_hour: Optional[float] = Field(
        None, ge=0, description="RAM pricing; required for volatile stores"
    )


class Properties(str, Enum):
    """Knobs a player may set per instance. Closed set: no hit_ratio,
    no processing times, no connection counts. Ever."""

    TIER = "tier"
    REPLICAS = "replicas"
    CAPACITY = "capacity"
    TTL = "ttl"
    LB_POLICY = "lb_policy"
    FILL_ON_MISS = "fill_on_miss"
    FILL_MODE = "fill_mode"
    WRITE_POLICY = "write_policy"
    SHARDS = "shards"


class Settings(BaseModel):
    """Type + constraints for ONE property. Consumed by graph validation
    and the settings-panel UI."""
    model_config = ConfigDict(extra="forbid")

    name: str
    data_type: Literal["float", "int", "bool", "choice"]
    minimum_value: Optional[float] = None
    maximum_value: Optional[float] = None
    choices: Optional[List[str]] = None
    optional: bool = False

    def validate_value(self, name: str, value) -> None:
        if value is None:
            if self.optional:
                return
            raise ValueError(f"'{name}' cannot be null")
        if self.data_type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"'{name}' must be an integer, got {value!r}")
        elif self.data_type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"'{name}' must be a number, got {value!r}")
        elif self.data_type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"'{name}' must be true or false, got {value!r}")
        elif self.data_type == "choice":
            if value not in (self.choices or []):
                raise ValueError(f"'{name}' must be one of {self.choices}, got {value!r}")
        if self.data_type in ("int", "float"):
            if self.minimum_value is not None and value < self.minimum_value:
                raise ValueError(f"'{name}' must be >= {self.minimum_value}, got {value}")
            if self.maximum_value is not None and value > self.maximum_value:
                raise ValueError(f"'{name}' must be <= {self.maximum_value}, got {value}")


SETTING_SPECS: Dict[Properties, Settings] = {
    Properties.TIER:         Settings(name="tier", data_type="choice",choices=["small", "medium", "large"]), 
    Properties.REPLICAS:     Settings(name="replicas", data_type="int",minimum_value=1, maximum_value=64),        #things that can be replicated, have multiple of(everything basically)
    Properties.CAPACITY:     Settings(name="capacity", data_type="int", minimum_value=1),                         #only for caches
    Properties.TTL:          Settings(name="ttl", data_type="float",minimum_value=0.001, optional=True),          #also for cache
    Properties.FILL_ON_MISS: Settings(name="fill_on_miss", data_type="bool"),                                     #for db,cache
    Properties.FILL_MODE:    Settings(name="fill_mode", data_type="choice",choices=["async", "sync"]),            #for db,cachr
    Properties.WRITE_POLICY: Settings(name="write_policy", data_type="choice",choices=["write_around", "write_through", "write_back"]), #db,cache
    Properties.LB_POLICY:    Settings(name="lb_policy", data_type="choice",choices=["round_robin", "least_connections"]),   #lb
    Properties.SHARDS:       Settings(name="shards", data_type="int", minimum_value=1),    #db
}


class TierSpec(BaseModel):
    """One size preset. Authored here; players only pick the name."""
    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(..., ge=1)
    per_hour_cost: float = Field(..., ge=0)
    queue_limit: Optional[int] = Field(None, ge=0)
    service_multiplier: float = Field(
        1.0, gt=0,
        description="scales p50/p99 for ALL ops; use sparingly "
                    "(e.g. 0.85 for a DB whose bigger buffer pool speeds reads)")


class ServiceTimes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read: Optional[OpPerformance] = None
    write: Optional[OpPerformance] = None
    compute: Optional[OpPerformance] = None
    forward: Optional[OpPerformance] = None

    def ops(self) -> set:
        return {k for k in ("read", "write", "compute", "forward")
                if getattr(self, k) is not None}


class ComponentEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., min_length=1)
    caps: List[CAPABILITY_LIST]
    # Physics for stores only: True = state survives node failure (disk),
    # False = wiped on failure + LRU-evictable (RAM).
    persistent: Optional[bool] = None
    service_times: ServiceTimes = Field(default_factory=ServiceTimes)
    cost: CostModel = Field(default_factory=CostModel)
    concurrency: int = Field(..., ge=1, description="parallel slots on ONE instance")
    queue_limit: int = Field(0, ge=0, description="bounded admission queue; 0 = no queueing")
    availability: float = Field(..., gt=0, le=1)
    properties: List[Properties] = Field(default_factory=list)
    defaults: Dict[str, Union[float, int, bool, str]] = Field(default_factory=dict)
    tiers: Optional[Dict[str, TierSpec]] = None

    @model_validator(mode="after")
    def _semantics(self) -> "ComponentEntry":
        is_store = "store" in self.caps

        if len(set(self.caps)) != len(self.caps):
            raise ValueError(f"'{self.type}': duplicate capability in caps")

        # persistent: required iff store, no silent volatile-by-default.
        if is_store and self.persistent is None:
            raise ValueError(
                f"'{self.type}': stores must declare persistent "
                "(true = survives failure/disk, false = wiped/RAM)")
        if not is_store and self.persistent is not None:
            raise ValueError(f"'{self.type}': persistent is store-only physics")

        # volatile stores: RAM-priced + capacity exposed.
        if is_store and self.persistent is False:
            if self.cost.per_gb_hour is None:
                raise ValueError(f"'{self.type}': volatile stores require cost.per_gb_hour")
            if Properties.CAPACITY not in self.properties:
                raise ValueError(f"'{self.type}': volatile stores must expose 'capacity'")

        #Check for correct ops in service times, returns valerror if theres 1 thats not supposed to be there
        declared = self.service_times.ops()
        #check for bad ops
        required_ops = set()
        for cap in self.caps:
            required_ops.update(REQUIRED_OPS[cap])
        if declared-required_ops: #something outside of all required
            raise ValueError(
                f"type: {self.type}, capabilities:{self.caps} contains an operation that should not exist for listed capabilies \n"
                f"Bad Ops: {declared-required_ops}"
            )
    
        #check for missing
        for cap in self.caps:
            missing = REQUIRED_OPS[cap] - declared
            if missing:
                raise ValueError(
                    f" type:'{self.type}', capability:'{cap}' requires service_times "
                    f"entries for: {sorted(missing)}")

        # tier property and tiers block come together, consistently.
        if (Properties.TIER in self.properties) != (self.tiers is not None):
            raise ValueError(
                f"'{self.type}': 'tier' in properties requires a tiers block, "
                "and vice versa")
        if self.tiers is not None:
            allowed = set(SETTING_SPECS[Properties.TIER].choices)
            unknown = set(self.tiers) - allowed
            if unknown:
                raise ValueError(
                    f"'{self.type}': unknown tier names {sorted(unknown)}")
            if self.defaults.get("tier") not in self.tiers:
                raise ValueError(
                    f"'{self.type}': defaults.tier must name one of "
                    f"{sorted(self.tiers)}")

        # defaults must target exposed properties with legal values.
        exposed = {p.value for p in self.properties}
        for key, value in self.defaults.items():
            if key not in exposed:
                raise ValueError(
                    f"'{self.type}': default for '{key}' but it is not in "
                    f"properties {sorted(exposed)}")
            SETTING_SPECS[Properties(key)].validate_value(key, value)
        return self


class ComponentLibrary(BaseModel):
    """Dict-keyed library: { "postgres": {...}, "redis": {...} }"""
    model_config = ConfigDict(extra="forbid")

    components: Dict[str, ComponentEntry]

    @model_validator(mode="after")
    def _keys_match_types(self) -> "ComponentLibrary":
        for key, entry in self.components.items():
            if key != entry.type:
                raise ValueError(
                    f"library key '{key}' != entry type '{entry.type}'")
        return self

    def get(self, type_name: str) -> ComponentEntry:
        try:
            return self.components[type_name]
        except KeyError:
            raise KeyError(
                f"unknown component type '{type_name}' "
                f"(library has: {sorted(self.components)})") from None

    def __contains__(self, t: str) -> bool:
        return t in self.components

    def __len__(self) -> int:
        return len(self.components)

    def __list_components__(self)->List[str]:
        return list(self.components.keys())

    def _literal_components__(self)->List[str]:
        return Literal[self.components.keys()]

class EffectivePhysics(BaseModel):
    """What one instance actually runs with, after tier resolution.
    The DES reads THIS, never raw entry/tier fields."""

    concurrency: int
    queue_limit: int
    per_hour: float
    service_multiplier: float = 1.0


def resolve_physics(entry: ComponentEntry, settings: dict) -> EffectivePhysics:
    tier_name = settings.get("tier", entry.defaults.get("tier"))
    if entry.tiers is None or tier_name is None:
        return EffectivePhysics(
            concurrency=entry.concurrency,
            queue_limit=entry.queue_limit,
            per_hour=entry.cost.per_hour
        )
    t = entry.tiers[tier_name]
    return EffectivePhysics(
        concurrency=t.concurrency,
        queue_limit=t.queue_limit if t.queue_limit is not None else entry.queue_limit,
        per_hour=t.per_hour_cost,
        service_multiplier=t.service_multiplier if t.service_multiplier is not None else 1.0
    )
