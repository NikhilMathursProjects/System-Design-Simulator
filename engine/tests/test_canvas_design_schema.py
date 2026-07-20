import pytest
from sysdesign_engine.library.component_loader import load_library
from sysdesign_engine.schemas.canvas_design_schema import (
    CanvasDesign,
    GraphError,
    load_graph_dict,
    resolved_settings,
    validate_against_challenge,
)
from sysdesign_engine.schemas.challenge_schema import load_challenge

from test_challenge_schema import FIXTURE as SHORTENER_CHALLENGE


@pytest.fixture(scope="module")
def library():
    return load_library()


@pytest.fixture(scope="module")
def challenge(library):
    return load_challenge(SHORTENER_CHALLENGE, library)


def shortener_graph() -> dict:
    """client -> server -> {cache, db}: the 4-edge cache-aside URL shortener."""
    return {
        "nodes": {
            "c1": {"type": "client"},
            "s1": {"type": "app_server", "name": "web tier",
                    "settings": {"replicas": 2, "tier": "medium"}},
            "r1": {"type": "redis", "settings": {"capacity": 50000}},
            "d1": {"type": "postgres"},
        },
        "edges": {
            "e1": {"req_from": "c1", "req_to": "s1"},
            "e2": {"req_from": "s1", "req_to": "r1"},
            "e3": {"req_from": "s1", "req_to": "d1"},
            "e4": {"req_from": "r1", "req_to": "d1"},
        },
    }


# ---------------------------------------------------------------------------
# round-trip: the frozen frontend<->backend contract
# ---------------------------------------------------------------------------


def test_round_trips(library):
    raw = shortener_graph()
    design = load_graph_dict(raw, library)
    dumped = design.model_dump(exclude_none=True, exclude_defaults=True)
    assert load_graph_dict(dumped, library) == design


def test_parses_without_library():
    load_graph_dict(shortener_graph())


def test_helpers(library):
    design = load_graph_dict(shortener_graph(), library)
    assert design.client_id == "c1"
    assert design.out_neighbors("s1") == ["d1", "r1"]
    assert design.out_neighbors("d1") == []


def test_resolved_settings_overlay_defaults(library):
    design = load_graph_dict(shortener_graph(), library)
    entry = library.get("redis")
    merged = resolved_settings(design.nodes["r1"], entry)
    assert merged["capacity"] == 50000               # player choice wins
    assert merged["write_policy"] == "write_around"  # default fills the gap
    assert merged["replicas"] == 1


# ---------------------------------------------------------------------------
# request_types: optional per-edge API whitelist
# ---------------------------------------------------------------------------


def test_plain_edge_carries_every_api(library):
    design = load_graph_dict(shortener_graph(), library)
    assert design.edges["e2"].allows("getShort")
    assert design.edges["e2"].allows("setShort")


def test_typed_edge_filters_out_neighbors(library):
    raw = shortener_graph()
    raw["edges"]["e2"]["request_types"] = ["getShort"]
    design = load_graph_dict(raw, library)
    assert design.out_neighbors("s1", api="getShort") == ["d1", "r1"]
    assert design.out_neighbors("s1", api="setShort") == ["d1"]


def test_empty_request_types_normalized_to_allow_all():
    """[] is normalized to None (= no restriction): the frontend sends [] when
    nothing is selected in the UI, meaning 'no restriction chosen'."""
    raw = shortener_graph()
    raw["edges"]["e2"]["request_types"] = []
    design = load_graph_dict(raw)
    assert design.edges["e2"].request_types is None
    assert design.edges["e2"].allows("getShort")


def test_duplicate_request_types_rejected():
    raw = shortener_graph()
    raw["edges"]["e2"]["request_types"] = ["getShort", "getShort"]
    with pytest.raises(GraphError) as e:
        load_graph_dict(raw)
    assert "duplicates" in str(e.value)


def test_request_types_checked_against_challenge(library, challenge):
    raw = shortener_graph()
    raw["edges"]["e2"]["request_types"] = ["getShort", "getShrt"]
    design = load_graph_dict(raw, library)
    with pytest.raises(GraphError) as e:
        validate_against_challenge(design, challenge)
    assert "getShrt" in str(e.value)
    assert "challenge declares" in str(e.value)


def test_valid_request_types_pass_challenge_check(library, challenge):
    raw = shortener_graph()
    raw["edges"]["e2"]["request_types"] = ["getShort"]
    raw["edges"]["e3"]["request_types"] = ["setShort", "getShort"]
    design = load_graph_dict(raw, library)
    validate_against_challenge(design, challenge)


# ---------------------------------------------------------------------------
# structural rejections
# ---------------------------------------------------------------------------


def test_rejects_empty_design():
    with pytest.raises(GraphError):
        load_graph_dict({"nodes": {}, "edges": {}})


def test_rejects_edge_to_unknown_node():
    raw = shortener_graph()
    raw["edges"]["e9"] = {"req_from": "s1", "req_to": "ghost"}
    with pytest.raises(GraphError) as e:
        load_graph_dict(raw)
    assert "'ghost' is not a node" in str(e.value)


def test_rejects_self_loop():
    raw = shortener_graph()
    raw["edges"]["e9"] = {"req_from": "s1", "req_to": "s1"}
    with pytest.raises(GraphError) as e:
        load_graph_dict(raw)
    assert "self-loop" in str(e.value)


def test_rejects_zero_clients():
    raw = shortener_graph()
    del raw["nodes"]["c1"]
    del raw["edges"]["e1"]
    with pytest.raises(GraphError) as e:
        load_graph_dict(raw)
    assert "exactly one client" in str(e.value)


def test_rejects_two_clients():
    raw = shortener_graph()
    raw["nodes"]["c2"] = {"type": "client"}
    with pytest.raises(GraphError) as e:
        load_graph_dict(raw)
    assert "exactly one client" in str(e.value)


def test_rejects_physics_smuggling():
    """Nodes may not carry library-entry fields -- physics comes from the
    library by type, never from the player."""
    raw = shortener_graph()
    raw["nodes"]["s1"]["service_times"] = {"compute": {"p50": 0.001, "p99": 0.002}}
    with pytest.raises(GraphError):
        load_graph_dict(raw)


# ---------------------------------------------------------------------------
# library-checked rejections
# ---------------------------------------------------------------------------


def test_rejects_unknown_component_type(library):
    raw = shortener_graph()
    raw["nodes"]["x1"] = {"type": "quantum_db"}
    with pytest.raises(GraphError) as e:
        load_graph_dict(raw, library)
    assert "unknown component type 'quantum_db'" in str(e.value)


def test_rejects_unexposed_setting(library):
    raw = shortener_graph()
    raw["nodes"]["d1"] = {"type": "postgres", "settings": {"capacity": 1000}}
    with pytest.raises(GraphError) as e:
        load_graph_dict(raw, library)
    assert "'capacity' is not " in str(e.value)


def test_rejects_illegal_setting_value(library):
    raw = shortener_graph()
    raw["nodes"]["r1"]["settings"] = {"write_policy": "yolo"}
    with pytest.raises(GraphError) as e:
        load_graph_dict(raw, library)
    assert "write_policy" in str(e.value)


def test_rejects_out_of_range_setting(library):
    raw = shortener_graph()
    raw["nodes"]["s1"]["settings"] = {"replicas": 500}
    with pytest.raises(GraphError) as e:
        load_graph_dict(raw, library)
    assert "replicas" in str(e.value)


def test_direct_client_to_db_is_legal(library):
    """Invariant 1: shape is never validated. A client wired straight to a
    DB is a valid graph; it just eats the metrics."""
    raw = {
        "nodes": {"c1": {"type": "client"}, "d1": {"type": "postgres"}},
        "edges": {"e1": {"req_from": "c1", "req_to": "d1"}},
    }
    design = load_graph_dict(raw, library)
    assert isinstance(design, CanvasDesign)
