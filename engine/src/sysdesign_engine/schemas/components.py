from pydantic import BaseModel,Field
from enum import Enum
from typing import (
    List,
    Dict,
    Literal,
    Optional,
)

CAPABILITY_LIST = Literal["store","compute","route","buffer","client"]


class OpPerformance(BaseModel):
    """Performance for a single operation like [read, write, buffer, compute] etc"""
    distribution: Literal["lognormal","exponential","constant","base"] = "lognormal"  #this defines the distribution of user submitting/ requesting data (i use request_key = abs(np.random.normal(0,10000)), this provides a decreasing occurence of values as we move forward , thus simulating common urls or whatever), im defining my method as base
    p50: float = Field(...,gt=0,description="Median latency ms/s not yet")
    p99: float = Field(...,gt=0,description="99th percentile latency ms/s idk")
    per_kb_time: float = Field(0.0,ge=0,description="Additional ms per KB")



class CostModel(BaseModel):
    """Defining how much some cost is , idk what all to set yet tho"""
    per_request: Optional[float] = Field(0.0,ge=0)   
    per_hour: Optional[float] = Field(0.0, ge=0)
    per_million_req: Optional[float] = Field(0.0, ge=0)




#settings for each component that is injected into the init
class Settings(BaseModel):
    name: str 
    maximum_value:Optional[float|int] = None
    minimum_value: Optional[float|int] = None 
    data_type: Literal["float","int","double","bool","choice"]
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
                raise ValueError(f"'{name}' must be >= {self.minimum_value}")
            if self.maximum_value is not None and value > self.maximum_value:
                raise ValueError(f"'{name}' must be <= {self.maximum_value}")



class Properties(str,Enum):
    TIER = "tier"  #small | medium | large    for mvp user choses preset values for some component
    REPLICAS = "replicas"
    CAPACITY = "capacity"
    LB_POLICY = "lb_policy"
    FILL_ON_MISS = "fill_on_miss"
    FILL_MODE = "fill_mode"
    WRITE_POLICY = "write_policy"
    SHARDS = "shards"

SETTING_SPECS: Dict[Properties, Settings] = {
    Properties.TIER:         Settings(name="tier",data_type="choice",choices=["small","medium","large"]),
    Properties.REPLICAS:     Settings(name="replicas",data_type="int", minimum_value=1, maximum_value=64),
    Properties.CAPACITY:     Settings(name="capacity",data_type="int", minimum_value=1),
    Properties.TTL:          Settings(name="ttl",data_type="float", minimum_value=0.001, optional=True),
    Properties.FILL_ON_MISS: Settings(name="fill_on_miss",data_type="bool"),
    Properties.FILL_MODE:    Settings(name="fill_mode",data_type="choice", choices=["async", "sync"]),
    Properties.WRITE_POLICY: Settings(name="write_policy",data_type="choice",choices=["write_around", "write_through", "write_back"]),
    Properties.LB_POLICY:    Settings(name="lb_policy",data_type="choice",choices=["round_robin", "least_connections"]),
    Properties.SHARDS:       Settings(name="shards",data_type="int", minimum_value=1),
}

class TierSpec(BaseModel):
    """Any size preset, we jujst choose the name and have this changed"""
    concurrency: int = Field(..., ge=1)
    per_hour_cost: float = Field(..., ge=0)       
    queue_limit: Optional[int] = Field(None, ge=0)
    service_multiplier: float = Field(1.0, gt=0,description="scales p50/p99 for ALL ops; use sparingly (e.g. 0.8 for a DB whose bigger buffer pool speeds reads)")



class ServiceTimes(BaseModel):
    read: Optional[OpPerformance] = None
    write: Optional[OpPerformance] = None
    compute: Optional[OpPerformance] = None
    forward: Optional[OpPerformance] = None



class ComponentEntry(BaseModel):
    type: str = Field(...,min_length=1)   
    caps: List[CAPABILITY_LIST] 
    authoritative: bool = False 
    service_times: ServiceTimes = Field(default_factory=ServiceTimes)
    cost: CostModel 
    concurrency: int
    queue_limit: int = Field(default=0, ge=0, description="bounded admission queue; 0 = no queueing")
    availability: float = Field(gt=0, le=1)
    properties: List[Properties]
    defaults: Optional[Dict[str, float | int | bool | str]] = Field(default_factory=dict)