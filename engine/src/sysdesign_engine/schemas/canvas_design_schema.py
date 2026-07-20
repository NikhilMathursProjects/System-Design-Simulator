"""T-012 user graph model: what the canvas sends to the engine.

Nodes carry ONLY {type, name, settings} -- never physics. The engine looks
up physics from the component library by `type`; a graph that could ship
its own service times would let players forge their own physics. TODO:[future dynamic builder work]

Edges are directed wires, 
request_types = null DEFAULT 
an edge is a plain road that carries every API. A player may OPTIONALLY set request_types = ["getShort", ...] to whitelist which APIs may traverse that wire 
a steering knob for split read/write paths. 
Whether the guided path actually WORKS is not decided here: schema/cross-checks catch
unknown API names, and the per-API feasibility table (T-020/T-021) runs on each API's filtered subgraph  
restrictions that strand an API surface as lint errors and named dead-ends at run time, never silent failures.

Wire format (frozen -- this is the frontend<->backend contract):

    {
        "nodes": {
            "n1": {"type": "client"},
            "n2": {"type": "app_server", "name": "web tier",
                    "settings": {"replicas": 2, "tier": "medium"}},
            "n3": {"type": "postgres"}
        },
        "edges": {
            "e1": {"req_from": "n1", "req_to": "n2"},
            "e2": {"req_from": "n2", "req_to": "n3",
                    "request_types": ["setShort"]}
        }
    }
"""

from typing import Dict, List, Optional, Union, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from sysdesign_engine.schemas.challenge_schema import ChallengeRecord
from sysdesign_engine.schemas.components_schema import (
    SETTING_SPECS,
    ComponentEntry,
    ComponentLibrary,
    Properties,
)

SettingValue = Union[float, int, bool, str, Any] #could be anything 




class GraphError(Exception):
    """Raised when a user canvas design fails validation. Message reads like a sentence and names the offending node/edge."""



class NodeEntry(BaseModel):
    """
    One component instance. The dict key in CanvasDesign.nodes is the node_id: `id`\n 
    NodeEntry contains:\n
    - `type`: component type
    - `settings`: User defined settings for that component
    """
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., min_length=1, description="component type")
    name: Optional[str] = Field(None, description="users label [cosmetic only]")
    settings: Dict[str, SettingValue] = Field(default_factory=dict)



class EdgeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_types: Optional[List[str]] = Field(None, description="APIs allowed on this wire; null = all")
    req_from: str = Field(..., min_length=1)
    req_to: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _request_types_sane(self) -> "EdgeEntry":
        if self.request_types is not None:
            if len(self.request_types) == 0:
                #basically None/null, so i set it as None
                self.request_types = None
                return self
            
            for name in self.request_types:
                if not name or not name.strip():
                    raise ValueError("request_types entries must be non empty API names, they must not be \"\" empty strings or anything like that, another frontend issue")
            if len(set(self.request_types)) != len(self.request_types):
                raise ValueError(f"request_types has duplicates: {self.request_types}, must be error from frontend side (check the creator)")
        return self

    def allows(self, api: str) -> bool:
        return self.request_types is None or api in self.request_types


class CanvasDesign(BaseModel):
    """
    The player's whole design. 
    Structural rules validate always; 
    API name rules need the challenge to see if the name is correct validate_against_challenge
    library rules (component type exists, settings legal) validate when a ComponentLibrary is passed via pydantic context: 
    - model_validate(raw, context={"library": lib}) \n
    """
    model_config = ConfigDict(extra="forbid")

    nodes: Dict[str, NodeEntry]
    edges: Dict[str, EdgeEntry]

    @model_validator(mode="after")
    def _structure(self) -> "CanvasDesign":
        if not self.nodes or not self.edges:
            raise ValueError("design has no nodes or edges, thus nothing to simulate, will never happen ill have frontend check if no components, notif:  `theres no nodes!`")

        for edge_id, edge in self.edges.items():
            for end, node_id in (("req_from", edge.req_from), ("req_to", edge.req_to)):
                if node_id not in self.nodes:
                    raise ValueError(
                        f"edge '{edge_id}': {end} '{node_id}' is not a node in this design")
            if edge.req_from == edge.req_to:
                raise ValueError(
                    f"edge '{edge_id}': self-loop on '{edge.req_from}' is not allowed")

        clients = [nid for nid, n in self.nodes.items() if n.type == "client"]
        if len(clients) != 1:
            raise ValueError(
                f"design must contain exactly one client entry node, found {len(clients)}{f' ({sorted(clients)})' if clients else ''}")
        return self

    @model_validator(mode="after")
    def _against_library(self, info: ValidationInfo) -> "CanvasDesign":
        context = info.context or {}
        library: Optional[ComponentLibrary] = context.get("library")
        if library is not None:
            validate_against_library(self, library)
        return self

    @property
    def client_id(self) -> str:
        return next(nid for nid, n in self.nodes.items() if n.type == "client")

    def out_neighbors(self, node_id: str, api: Optional[str] = None) -> list:
        """
        Downstream node ids reachable from node_id; with `api`, only over edges whose request_types allow it
        This is the candidate set the routing kernel (T-030) and feasibility table (T-021)
        """
        return sorted({
            e.req_to for e in self.edges.values()
            if e.req_from == node_id and (api is None or e.allows(api))
        })


def validate_against_library(design: CanvasDesign, library: ComponentLibrary) -> None:
    """Every node's type exists; every settings key is exposed by that type; every value passes its SETTING_SPECS constraint."""
    for node_id, node in design.nodes.items():
        if node.type not in library:
            raise ValueError(
                f"node '{node_id}': unknown component type '{node.type}'\n (library has: {sorted(library.components)})"
            )
        entry = library.get(node.type)
        exposed = {p.value for p in entry.properties}
        for key, value in node.settings.items():
            if key not in exposed:
                raise ValueError(
                    f"node '{node_id}' ({node.type}): setting '{key}' is not exposed by this component (allowed: {sorted(exposed) or 'none'})"
                )
            try:
                SETTING_SPECS[Properties(key)].validate_value(key, value)
            except ValueError as e:
                raise ValueError(f"node '{node_id}' ({node.type}): {e}") from e


def validate_against_challenge(design: CanvasDesign, challenge: ChallengeRecord) -> None:
    """Every API named on an edge's request_types must be an API the
    challenge declares. Whether the guided path is FEASIBLE is the
    feasibility table's job (T-020/T-021), not a schema concern."""
    declared = set(challenge.apis)
    for edge_id, edge in design.edges.items():
        if edge.request_types is None:
            continue
        unknown = set(edge.request_types) - declared
        if unknown:
            raise GraphError(
                f"edge '{edge_id}': request_types name unknown APIs "
                f"{sorted(unknown)} (challenge declares: {sorted(declared)})")


def resolved_settings(node: NodeEntry, entry: ComponentEntry) -> Dict[str, SettingValue]:
    """Library defaults overlaid with the player's explicit choices. This is
    the dict the DES hands to resolve_physics() at instantiation."""
    merged = dict(entry.defaults)
    merged.update(node.settings)
    return merged


def load_graph_dict(raw: dict, library: Optional[ComponentLibrary] = None) -> CanvasDesign:
    context = {"library": library} if library is not None else None
    try:
        return CanvasDesign.model_validate(raw, context=context)
    except ValueError as e:
        raise GraphError(str(e)) from e