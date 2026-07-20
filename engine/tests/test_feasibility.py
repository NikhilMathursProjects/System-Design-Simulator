import time

from sysdesign_engine.feasibility import build_feasibility, lint
from sysdesign_engine.library.component_loader import load_library
from sysdesign_engine.schemas.canvas_design_schema import load_graph_dict
from sysdesign_engine.schemas.challenge_schema import load_challenge, load_challenge_dict

import pytest

from test_challenge_schema import FIXTURE as SHORTENER_CHALLENGE


@pytest.fixture(scope="module")
def library():
    return load_library()


@pytest.fixture(scope="module")
def shortener(library):
    return load_challenge(SHORTENER_CHALLENGE, library)


def design(nodes, edges, library):
    raw_nodes = {}
    for nid, spec in nodes.items():
        type_name, settings = (spec, {}) if isinstance(spec, str) else spec
        raw_nodes[nid] = {"type": type_name, "settings": settings}
    raw_edges = {}
    for i, spec in enumerate(edges):
        src, dst, *rest = spec
        raw_edges[f"e{i}"] = {"req_from": src, "req_to": dst}
        if rest:
            raw_edges[f"e{i}"]["request_types"] = rest[0]
    return load_graph_dict({"nodes": raw_nodes, "edges": raw_edges}, library)


# ---------------------------------------------------------------------------
# read-through / cache-aside: default write_around cache stays OUT of the
# landing set, so read misses keep routing (SPEC section 16 trace)
# ---------------------------------------------------------------------------


def test_read_through(library, shortener):
    d = design({"c": "client", "s": "app_server", "r": "redis", "db": "postgres"},
               [("c", "s"), ("s", "r"), ("r", "db")], library)
    feas = build_feasibility(d, library, shortener)
    assert feas.landing_set("mapping") == frozenset({"db"})
    assert feas.api_feasible("getShort")
    assert feas.api_feasible("setShort")
    # the cache is on a guaranteed path (miss forwards to db), so it IS a
    # feasible next hop even though it can't terminate the read itself
    assert feas.feasible_next("getShort", "s", frozenset()) == ["r"]
    assert lint(feas, shortener) == []


def test_cache_aside(library, shortener):
    d = design({"c": "client", "s": "app_server", "r": "redis", "db": "postgres"},
               [("c", "s"), ("s", "r"), ("s", "db")], library)
    feas = build_feasibility(d, library, shortener)
    assert feas.landing_set("mapping") == frozenset({"db"})
    assert feas.api_feasible("getShort")
    # guaranteed candidates from the server exclude the dead-end cache; the
    # router adds the opportunistic cache side-trip itself (T-030)
    assert feas.feasible_next("getShort", "s", frozenset()) == ["db"]
    assert not feas.can_complete("getShort", "r")


# ---------------------------------------------------------------------------
# cache-only designs: legal iff the cache accepts write landings
# ---------------------------------------------------------------------------


def test_cache_only_write_back_is_legal_but_warned(library, shortener):
    d = design({"c": "client", "s": "app_server",
                "r": ("redis", {"write_policy": "write_back"})},
               [("c", "s"), ("s", "r")], library)
    feas = build_feasibility(d, library, shortener)
    assert feas.landing_set("mapping") == frozenset({"r"})
    assert feas.api_feasible("getShort")     # retrieve TERMINATES at the cache
    assert feas.api_feasible("setShort")
    msgs = lint(feas, shortener)
    assert [m.severity for m in msgs] == ["warn"]
    assert "volatile" in msgs[0].message and "durability" in msgs[0].message


def test_cache_only_write_around_dead_ends(library, shortener):
    d = design({"c": "client", "s": "app_server", "r": "redis"},
               [("c", "s"), ("s", "r")], library)
    feas = build_feasibility(d, library, shortener)
    assert feas.landing_set("mapping") == frozenset()
    assert not feas.api_feasible("setShort")
    assert not feas.api_feasible("getShort")
    by_api = {m.api: m for m in lint(feas, shortener)}
    assert by_api["setShort"].severity == "error"
    assert by_api["setShort"].offending_goal == "store(mapping)"
    assert "write_around" in by_api["setShort"].message
    assert by_api["getShort"].offending_goal == "retrieve(mapping)"


# ---------------------------------------------------------------------------
# invariant 1: shape is never validated
# ---------------------------------------------------------------------------


def test_direct_client_to_db_is_feasible(library, shortener):
    d = design({"c": "client", "db": "postgres"}, [("c", "db")], library)
    feas = build_feasibility(d, library, shortener)
    assert feas.api_feasible("getShort")     # compute_units is cost-only
    assert feas.api_feasible("setShort")
    assert lint(feas, shortener) == []


def test_missing_db(library, shortener):
    d = design({"c": "client", "s": "app_server"}, [("c", "s")], library)
    feas = build_feasibility(d, library, shortener)
    assert not feas.api_feasible("setShort")
    assert not feas.api_feasible("getShort")
    assert {m.severity for m in lint(feas, shortener)} == {"error"}


# ---------------------------------------------------------------------------
# composite read -> compute -> store: ordering is enforced by the table
# ---------------------------------------------------------------------------


COMPOSITE_CHALLENGE = {
    "entities": {"profile": {"record_kb": 2.0, "keyspace": 1000},
                 "profile_enriched": {"record_kb": 4.0, "keyspace": 1000}},
    "apis": {
        "putProfile": {"effect": "store", "entity": "profile"},
        "enrich": {"reads": [{"entity": "profile"}], "compute_units": 2.0,
                    "writes": [{"entity": "profile_enriched"}]},
    },
    "traffic": {"shape": "request_response", "rps": 100,
                "mix": {"putProfile": 0.5, "enrich": 0.5}},
    "slos": {"availability": 0.99},
}


def test_composite_needs_compute_after_read(library):
    challenge = load_challenge_dict(COMPOSITE_CHALLENGE, library)
    # no way back from the db to a compute node -> enrich cannot complete
    d = design({"c": "client", "s": "app_server", "db": "postgres"},
               [("c", "s"), ("s", "db")], library)
    feas = build_feasibility(d, library, challenge)
    assert feas.api_feasible("putProfile")
    assert not feas.api_feasible("enrich")
    msg = {m.api: m for m in lint(feas, challenge)}["enrich"]
    assert msg.offending_goal == "compute"

    # a return edge db -> s lets the chain complete: read at db, compute at
    # s, store back at db
    d2 = design({"c": "client", "s": "app_server", "db": "postgres"},
                [("c", "s"), ("s", "db"), ("db", "s")], library)
    feas2 = build_feasibility(d2, library, challenge)
    assert feas2.api_feasible("enrich")


# ---------------------------------------------------------------------------
# mixed landing sets + request_types steering (per-entity, per-edge)
# ---------------------------------------------------------------------------


MIXED_CHALLENGE = {
    "entities": {"trips": {"record_kb": 1.0, "keyspace": 100000},
                 "locations": {"record_kb": 0.2, "keyspace": 50000}},
    "apis": {
        "storeTrip": {"effect": "store", "entity": "trips"},
        "getTrip": {"effect": "retrieve", "entity": "trips"},
        "storeLoc": {"effect": "store", "entity": "locations"},
        "getLoc": {"effect": "retrieve", "entity": "locations"},
    },
    "traffic": {"shape": "request_response", "rps": 100,
                "mix": {"storeTrip": 0.25, "getTrip": 0.25,
                        "storeLoc": 0.25, "getLoc": 0.25}},
    "slos": {"availability": 0.99},
}


def test_mixed_landing_sets_with_request_types(library):
    challenge = load_challenge_dict(MIXED_CHALLENGE, library)
    d = design(
        {"c": "client", "s": "app_server",
         "db": "postgres", "r": ("redis", {"write_policy": "write_back"})},
        [("c", "s"),
         ("s", "db", ["storeTrip", "getTrip"]),
         ("s", "r", ["storeLoc", "getLoc"])],
        library)
    feas = build_feasibility(d, library, challenge)
    # per-entity landing, shaped by the player's edge whitelists
    assert feas.landing_set("trips") == frozenset({"db"})
    assert feas.landing_set("locations") == frozenset({"r"})
    for api in challenge.apis:
        assert feas.api_feasible(api), api


def test_request_types_stranding_reads_is_an_error(library, shortener):
    # writes may pass, reads may not: setShort lands fine, getShort is stranded
    d = design({"c": "client", "s": "app_server", "db": "postgres"},
               [("c", "s"), ("s", "db", ["setShort"])], library)
    feas = build_feasibility(d, library, shortener)
    assert feas.api_feasible("setShort")
    assert feas.landing_set("mapping") == frozenset({"db"})
    assert not feas.api_feasible("getShort")
    msg = {m.api: m for m in lint(feas, shortener)}["getShort"]
    assert msg.severity == "error"
    assert msg.offending_goal == "retrieve(mapping)"


def test_request_types_stranding_writes_kills_reads_too(library, shortener):
    # blocking the WRITES empties the landing set, so reads ALSO have nowhere
    # authoritative to terminate: both APIs error
    d = design({"c": "client", "s": "app_server", "db": "postgres"},
               [("c", "s"), ("s", "db", ["getShort"])], library)
    feas = build_feasibility(d, library, shortener)
    assert feas.landing_set("mapping") == frozenset()
    assert not feas.api_feasible("setShort")
    assert not feas.api_feasible("getShort")
    assert {m.api for m in lint(feas, shortener)} == {"setShort", "getShort"}


# ---------------------------------------------------------------------------
# cycles terminate; scale stays fast (T-021 done-criterion)
# ---------------------------------------------------------------------------


def test_cyclic_graph_terminates_correctly(library, shortener):
    d = design({"c": "client", "s": "app_server", "r": "redis", "db": "postgres"},
               [("c", "s"), ("s", "r"), ("r", "db"), ("r", "s"), ("db", "s")],
               library)
    feas = build_feasibility(d, library, shortener)
    assert feas.api_feasible("getShort")
    assert feas.landing_set("mapping") == frozenset({"db"})


def test_25_node_graph_6_goal_dag_is_fast(library):
    challenge = load_challenge_dict({
        "entities": {"a": {"record_kb": 1.0, "keyspace": 100},
                     "b": {"record_kb": 1.0, "keyspace": 100},
                     "out": {"record_kb": 1.0, "keyspace": 100}},
        "apis": {
            "seedA": {"effect": "store", "entity": "a"},
            "seedB": {"effect": "store", "entity": "b"},
            "bigJoin": {"reads": [{"entity": "a"}, {"entity": "b"},
                                   {"entity": "a", "read_units": 2.0}],
                         "compute_units": 3.0,
                         "writes": [{"entity": "out"}, {"entity": "out",
                                     "write_units": 2.0}]},
        },
        "traffic": {"shape": "request_response", "rps": 100,
                    "mix": {"seedA": 0.4, "seedB": 0.4, "bigJoin": 0.2}},
        "slos": {"availability": 0.99},
    }, library)

    nodes = {"c": "client"}
    edges = [("c", "s0")]
    for i in range(12):                      # chain of 12 servers
        nodes[f"s{i}"] = "app_server"
        if i:
            edges.append((f"s{i-1}", f"s{i}"))
    for j in range(12):                      # 12 stores fanned off the tail
        nodes[f"d{j}"] = "postgres"
        edges.append(("s11", f"d{j}"))
    edges.append(("d0", "s0"))               # return edge for the composite
    d = design(nodes, edges, library)        # 25 nodes total

    t0 = time.perf_counter()
    feas = build_feasibility(d, library, challenge)
    for api in challenge.apis:
        assert feas.api_feasible(api), api
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5, f"feasibility took {elapsed:.3f}s"
