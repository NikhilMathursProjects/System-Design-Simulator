import copy
import json

import pytest
from pydantic import ValidationError

from sysdesign_engine.library.component_loader import DEFAULT_LIBRARY_PATH, LibraryError, load_library
from sysdesign_engine.schemas.components_schema import (
    ComponentEntry,
    ComponentLibrary,
    Properties,
    resolve_physics
)


#--------------------------------------helpers:minimal valid entry per capability, used as mutation base----------------------------------------------------
def base_entry(**overrides) -> dict:
    entry = {
        "type": "thing",
        "caps": ["compute"],
        "service_times": {"compute": {"dist": "lognormal", "p50": 1.0, "p99": 2.0}},
        "cost": {},
        "concurrency": 10,
        "availability": 0.99,
        "properties": [],
    }
    entry.update(overrides)
    return entry


def make(**overrides) -> ComponentEntry:
    """updates the basic entry in base_entry() with inputted dict"""
    return ComponentEntry.model_validate(base_entry(**overrides))



#---------------------------------seed library loads----------------------------------------------------
def test_seed_library_loads():
    """testing if all the base components load"""
    lib = load_library()
    assert len(lib) == 10 #cause i only have 10 rn
    assert DEFAULT_LIBRARY_PATH.exists()

def test_seed_library_keys_match_types():
    """test theyre all validly made"""
    lib = load_library()
    for key, entry in lib.components.items():
        assert key == entry.type



#----------------------------------valid check per capability----------------------------------
#testing each capability 
def test_valid_compute():
    """tests if the component that we create (ComponentEntry type) contains the correct vals for compute"""
    e = make(
        type="server", caps=["compute"],
        service_times={
            "compute": {"p50": 1.0, "p99": 2.0},
        }
    )
    assert e.caps == ["compute"]


def test_valid_route():
    e = make(
        type="lb", caps=["route"],
        service_times={
            "forward": {"p50": 1.0, "p99": 2.0},
            # "compute": {"p50": 0.3, "p99": 1.0}
        }
    )
    assert e.caps == ["route"]


def test_valid_store_persistent():
    e = make(
        type="db", caps=["store"], persistent=True,
        service_times={
            "read": {"p50": 1.0, "p99": 2.0},
            "write": {"p50": 1.0, "p99": 2.0}
        }
    )
    assert e.persistent is True


def test_valid_store_volatile():
    e = make(
        type="cache", caps=["store"], persistent=False,
        service_times={
            "read": {"p50": 1.0, "p99": 2.0},
            "write": {"p50": 1.0, "p99": 2.0}
        },
        cost={"per_gb_hour": 0.01},
        properties=[Properties.CAPACITY],
        defaults={"capacity": 100}
    )
    assert e.persistent is False


def test_valid_buffer():
    """kafka or whatvewr ive never used a buffer"""
    e = make(
        type="queue", caps=["buffer"],
        service_times={
            "read": {"p50": 1.0, "p99": 2.0},
            "write": {"p50": 1.0, "p99": 2.0}
        }
    )
    assert e.caps == ["buffer"]


def test_valid_client():
    e = make(type="client", caps=["client"], service_times={})
    assert e.caps == ["client"]


def test_valid_multi_cap_route_and_store():
    e = make(
        type="edge_cache", 
        caps=["route", "store"], 
        persistent=False,
        service_times={
            "forward": {"p50": 0.5, "p99": 1.0},
            "read": {"p50": 0.5, "p99": 1.0},
            "write": {"p50": 0.5, "p99": 1.0}
        },
        cost={"per_gb_hour": 0.01},
        properties=[Properties.CAPACITY],
        defaults={"capacity": 100}
    )
    assert set(e.caps) == {"route", "store"}

# def test_valid_tier_selection
#-----------------------------------------readable error cases-----------------------------------

def test_error_store_without_persistent():
    """without explicitly specifying persistent for component that requires read, write ops"""
    with pytest.raises(ValidationError):
        make(
            type="db", caps=["store"],
            service_times={
                "read": {"p50": 1.0, "p99": 2.0},
                "write": {"p50": 1.0, "p99": 2.0}
            }
        )


def test_error_persistent_on_non_store():
    with pytest.raises(ValidationError):
        make(
            type="server", caps=["compute"], persistent=True,
            service_times={"compute": {"p50": 1.0, "p99": 2.0}}
        )


def test_error_volatile_without_per_gb_hour():
    with pytest.raises(ValidationError):
        make(
            type="cache", caps=["store"], persistent=False,
            service_times={
                "read": {"p50": 1.0, "p99": 2.0},
                "write": {"p50": 1.0, "p99": 2.0}
            },
            properties=[Properties.CAPACITY], defaults={"capacity": 100}
        )


def test_error_volatile_without_capacity_property():
    with pytest.raises(ValidationError):
        make(
            type="cache", caps=["store"], persistent=False,
            service_times={
                "read": {"p50": 1.0, "p99": 2.0},
                "write": {"p50": 1.0, "p99": 2.0}
            },
            cost={"per_gb_hour": 0.01}
        )


def test_error_cap_with_bad_service_op():
    """route shouldnt have a compute service op unless we also have compute defined in the caps"""
    with pytest.raises(ValidationError):
        make(
            type="lb", caps=["route"],
            service_times={
                "forward": {"p50": 1.0, "p99": 2.0},
                "compute": {"p50": 0.3, "p99": 1.0}
            }
        )

def test_error_cap_missing_service_op():
    with pytest.raises(ValidationError):
        make(
            type="db", caps=["store"], persistent=True,
            service_times={"read": {"p50": 1.0, "p99": 2.0}}
        )


def test_error_p50_gte_p99():
    with pytest.raises(ValidationError):
        make(service_times={"compute": {"p50": 5.0, "p99": 5.0}})


def test_error_unknown_property():
    with pytest.raises(ValidationError):
        make(properties=["not_a_real_property"])


def test_error_unknown_extra_field():
    with pytest.raises(ValidationError):
        make(bogus_field="nope")


def test_error_default_for_unexposed_property():
    with pytest.raises(ValidationError):
        make(properties=[], defaults={"replicas": 1})


def test_error_illegal_default_value():
    with pytest.raises(ValidationError):
        make(properties=[Properties.REPLICAS], defaults={"replicas": 0})


def test_error_tiers_without_tier_property():
    with pytest.raises(ValidationError):
        make(properties=[], tiers={"small": {"concurrency": 1, "per_hour_cost": 0.1}})


def test_error_tier_property_without_tiers_block():
    with pytest.raises(ValidationError):
        make(properties=[Properties.TIER], defaults={"tier": "small"})


def test_error_defaults_tier_not_in_tiers():
    with pytest.raises(ValidationError):
        make(
            properties=[Properties.TIER],
            tiers={"small": {"concurrency": 1, "per_hour_cost": 0.1}},
            defaults={"tier": "medium"}
        )


def test_error_duplicate_mismatched_dict_key():
    raw = {
        "postgres": base_entry(
            type="mongo", caps=["store"], persistent=True,
            service_times={
                "read": {"p50": 1.0, "p99": 2.0},
                "write": {"p50": 1.0, "p99": 2.0}
            }
        )
    }
    with pytest.raises(ValidationError):
        ComponentLibrary.model_validate({"components": raw})


def test_loader_wraps_validation_error_readably(tmp_path):
    bad = copy.deepcopy(base_entry())
    bad["service_times"] = {"compute": {"p50": 5.0, "p99": 1.0}}  # p50 >= p99 is wrong
    bad_json = tmp_path / "bad.json"
    bad_json.write_text(json.dumps({"thing": bad}))
    with pytest.raises(LibraryError) as exc_info:
        load_library(bad_json)
    message = str(exc_info.value)
    assert "thing" in message
    assert "p50" in message and "p99" in message




#-----------------------relative-physics sanity (ordering, not absolute values, is what matters)--------------

def test_relative_physics_read_latency_ordering():
    lib = load_library()
    redis_read = lib.components.get("redis").service_times.read.p50
    postgres_read = lib.get("postgres").service_times.read.p50
    s3_read = lib.get("s3").service_times.read.p50
    assert redis_read < postgres_read < s3_read


def test_relative_physics_ram_pricier_than_disk_per_gb():
    lib = load_library()
    redis_gb = lib.get("redis").cost.per_gb_hour
    postgres_gb = lib.get("postgres").cost.per_gb_hour
    assert redis_gb > postgres_gb



#-----------------------------------------resolve_physics------------------------------------------
def test_resolve_physics_tier_changes_concurrency_and_cost():
    lib = load_library()
    entry = lib.get("app_server")
    small = resolve_physics(entry, {"tier": "small"})
    large = resolve_physics(entry, {"tier": "large"})
    assert large.concurrency > small.concurrency
    assert large.per_hour > small.per_hour


def test_resolve_physics_tierless_passthrough():
    lib = load_library()
    entry = lib.get("mongo")  # no tiers block
    phys = resolve_physics(entry, {})
    assert phys.concurrency == entry.concurrency
    # assert phys.per_hour == entry.cost.per_hour
    assert phys.service_multiplier == 1.0


def test_resolve_physics_service_multiplier_applied():
    lib = load_library()
    entry = lib.get("postgres")
    large = resolve_physics(entry, {"tier": "large"})
    assert large.service_multiplier == 0.85


def test_resolve_physics_defaults_to_entry_default_tier():
    lib = load_library()
    entry = lib.get("app_server")
    phys = resolve_physics(entry, {})
    assert phys.concurrency == entry.tiers["small"].concurrency


def test_resolve_pyshics_override_base_components_settings_via_tier():
    """basically i just update the entire entry thats being inserted"""
    lib = load_library()
    entry = lib.get("redis")
    small = resolve_physics(entry, {"tier": "small"})
    # print("redis small")
    large = resolve_physics(entry, {"tier": "large"})
    print("redis large",large)
    assert large.concurrency > small.concurrency
    assert large.per_hour > small.per_hour