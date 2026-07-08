
"""
These are the things the user can set
server: acts as literally 1 server
apps: int     [Literally the number of apps running concurrently on this 1 server]
request_processing_time: float    [The amount of time this specific request will take to process on this specific component(server)]


cache:  acts as 1 cache instance
lru:bool =False    [sets the cache into lru or not type]
ttl: Optional[float] = None    [if not set, no ttl is placed, otherwise it places the same ttl on all kv stored in the cache]
number_of_connections: int       [number of parallel connections this cache can handle and perform queries/ handle requests]
cache_size: int                 [literally cache size]


db:    1 db instance
num_connections: int           [number of parallel connections this db can handle]
query_processing_time: float     [how long 1 query should take(function on that op is handled by OpPerformance obj)]
components = {
#     "server":{
#         "apps":5,
#         "request_processing_time":2  #in ms
#     },
#     "cache":{
#         "lru": False,
#         "ttl": 60.0,
#         "number_of_connections":500,
#         "cache_size":10_000
#     },
#     "db":{
#         "num_connections": 500,
#         "query_processing_time": 100 #ms
#     }
# }
"""
from pydantic import BaseModel,Field
from typing import (
    List,
    Dict,
    Any,
    Literal,
    Optional,
)

CAPABILITY_LIST = Literal["store","compute","route","buffer","client"]



class OpPerformance(BaseModel):
    """Performance for a single operation like [read, write, buffer, compute] etc"""
    distribution: Literal["lognormal","exponential","constant","base"] = "lognormal"  #this defines the distribution of user submitting/ requesting data (i use request_key = abs(np.random.normal(0,10000)), this provides a decreasing occurence of values as we move forward , thus simulating common urls or whatever), im defining my method as base
    p50: float = Field(...,gt=0,description="Median latency ms/s not yet")
    p99: float = Field(...,gt=0,description="99th percentile latency ms/s idk")
    per_kb: float = Field(0.0,ge=0,description="Additional ms per KB")



class CostModel(BaseModel):
    """Defining how much some cost is , idk what all to set yet tho"""
    per_request: float = Field(0.0,ge=0)   
    per_hour: float = Field(..., ge=0)
    per_million_req: float = Field(0.0, ge=0)


#settings for each component that is injected into the init
class Settings(BaseModel):
    name: str                                                                        #variable name set by user or us of that setting value
    maximum_value:Optional[float|int]
    minimum_value: Optional[float|int] 
    choices: List[str]
    data_type: Optional[Literal["float","int","double","bool","choice"]] = "Any"


class Properties(BaseModel):
    REPLICAS = "replicas"
    CAPACITY = "capacity"
    LB_POLICY = "lb_policy"
    FILL_ON_MISS = "fill_on_miss"
    WRITE_POLICY = "write_policy"
    SHARDS = "shards"



class ComponentEntry(BaseModel):
    type: str = Field(...,min_length=1)   #type of component
    caps = List[CAPABILITY_LIST]  #its list of capabs
    authoritative: bool = False    #used mainly for store caps cause we get to know an end to store on
    
    base_settings = List[Settings] = []  #basic settings for that specific component, for now we can set it within the json of all components as base_settings 
    user_settings: List[Settings] = []   #stuff changed by the user (basically i show the base settings to a user when they create a component in the ui, and when they change anything its updated here)
    base_cost: float 
    base_service_time: float
    base_concurrency: int
    property: List[Properties]