"""T-020/T-021/T-022: write-landing sets + can_complete feasibility + lint.

The routing brain of the engine, computed BEFORE any simulation:

- LANDING SET (per entity E): every store where E's write/mutate APIs can
  actually land data along feasible paths. Policy-aware: persistent stores
  always accept landings; volatile stores only when their write_policy is
  write_through/write_back (write_around means "writes route around me").
  This is what makes the SPEC section-16 cache-aside trace work: the
  default write_around cache is NOT in the landing set, so a read miss
  there keeps routing instead of terminating as not-found.

- can_complete[api][(node, goal_state)]: "from this node, with these goals
  still open, can some forward path finish them all?" Computed bottom-up
  (fixpoint over the boolean lattice) instead of recursive DFS: same
  answer, no cycle-guard/memo soundness traps, microseconds at this size.
  GUARANTEED satisfaction only -- an opportunistic cache hit is chance,
  so it never counts here; the router (T-030) adds opportunistic cache
  side-trips on top, backed by branches this table proves out.

- The two are mutually recursive (retrieve is guaranteed only at landing
  stores; landing membership needs a completing path), so landing sets are
  a Kleene fixpoint from empty sets: monotone, converges in a few rounds.

- lint(): severity 'error' = impossibility (API cannot complete; run
  proceeds anyway per section 17.5 and produces named dead-ends);
  'warn' = risk (all-volatile landing under a durability SLO).

Per-entity retrieve semantics (correctness-critical, SPEC section 5):
at a store IN E's landing set a retrieve TERMINATES (hit, or authoritative
not-found); at a store OUTSIDE it a miss means keep routing. Implemented
per-entity via landing.get(goal.entity) -- never per-node.
"""

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from sysdesign_engine.derivation.goal_dag import (
    Goal,
    GoalDAG,
    GoalKind,
    GoalState,
    derive_challenge,
)
from sysdesign_engine.schemas.canvas_design_schema import CanvasDesign, resolved_settings
from sysdesign_engine.schemas.challenge_schema import ChallengeRecord
from sysdesign_engine.schemas.components_schema import ComponentLibrary

WRITE_KINDS = frozenset({GoalKind.STORE, GoalKind.MUTATE})
WRITE_ACCEPTING_POLICIES = frozenset({"write_through", "write_back"})


@dataclass(frozen=True)
class LintMessage:
    severity: str                       # "error" | "warn" -- never blocks a run
    api: Optional[str]
    message: str
    offending_goal: Optional[str] = None


def goal_label(goal: Goal) -> str:
    return f"{goal.kind.value}({goal.entity})" if goal.entity else goal.kind.value


class Feasibility:
    """Landing sets + per-API can_complete tables for one (design, challenge).

    Build once per lint/run via build_feasibility(). All queries are dict
    lookups afterwards.
    """

    def __init__(self, design: CanvasDesign, library: ComponentLibrary,
                 dags: Dict[str, GoalDAG]):
        self.design = design
        self.library = library
        self.dags = dags

        # static per-node facts
        self._caps: Dict[str, Set[str]] = {}
        self._volatile: Dict[str, bool] = {}
        self._accepts_writes: Dict[str, bool] = {}
        for nid, node in design.nodes.items():
            entry = library.get(node.type)
            caps = set(entry.caps)
            self._caps[nid] = caps
            is_store = "store" in caps
            self._volatile[nid] = is_store and entry.persistent is False
            if not is_store:
                self._accepts_writes[nid] = False
            elif not self._volatile[nid]:
                self._accepts_writes[nid] = True
            else:
                policy = resolved_settings(node, entry).get("write_policy")
                self._accepts_writes[nid] = policy in WRITE_ACCEPTING_POLICIES

        self.landing: Dict[str, FrozenSet[str]] = self._landing_fixpoint()
        self.tables: Dict[str, Dict[Tuple[str, GoalState], bool]] = {
            api: self._build_table(api, dag, self.landing)
            for api, dag in dags.items()
        }

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def can_complete(self, api: str, node_id: str,
                     state: Optional[GoalState] = None) -> bool:
        dag = self.dags[api]
        state = dag.initial_state() if state is None else state
        return self.tables[api][(node_id, state)]

    def api_feasible(self, api: str) -> bool:
        return self.can_complete(api, self.design.client_id)

    def landing_set(self, entity: str) -> FrozenSet[str]:
        return self.landing.get(entity, frozenset())

    def feasible_next(self, api: str, node_id: str, state: GoalState) -> List[str]:
        """Router candidates (T-030): out-neighbors from which the remaining
        goals can still complete. Opportunistic caches are NOT here -- the
        router adds those side-trips itself when a guaranteed branch exists."""
        return [m for m in self.design.out_neighbors(node_id, api=api)
                if self.tables[api][(m, state)]]

    # ------------------------------------------------------------------
    # guaranteed goal satisfaction at a node
    # ------------------------------------------------------------------

    def _guaranteed(self, nid: str, goal: Goal,
                    landing: Dict[str, FrozenSet[str]]) -> bool:
        caps = self._caps[nid]
        if goal.kind in WRITE_KINDS:
            # any write-accepting store; landing sets are DEFINED by where
            # these goals can land, so no landing check here
            return self._accepts_writes[nid]
        if goal.kind is GoalKind.RETRIEVE:
            # only landing stores terminate a retrieve with certainty
            return "store" in caps and nid in landing.get(goal.entity, frozenset())
        if goal.kind is GoalKind.COMPUTE:
            return "compute" in caps
        if goal.kind is GoalKind.DELIVER:
            return "client" in caps
        return False

    # ------------------------------------------------------------------
    # can_complete table: bottom-up over (node, goal_state)
    # ------------------------------------------------------------------

    def _preds_for(self, api: str) -> Dict[str, List[str]]:
        preds: Dict[str, List[str]] = {}
        for e in self.design.edges.values():
            if e.allows(api):
                preds.setdefault(e.req_to, []).append(e.req_from)
        return preds

    def _build_table(self, api: str, dag: GoalDAG,
                     landing: Dict[str, FrozenSet[str]]
                     ) -> Dict[Tuple[str, GoalState], bool]:
        nodes = list(self.design.nodes)
        n = len(dag.goals)
        preds = self._preds_for(api)
        table: Dict[Tuple[str, GoalState], bool] = {}

        # Process states from most-satisfied to least: advance moves point to
        # bigger states (already final), forward moves stay within a state --
        # so each level needs one propagation pass over reverse edges.
        for r in range(n, -1, -1):
            for combo in combinations(range(n), r):
                state = frozenset(combo)
                if len(state) == n:
                    for nid in nodes:
                        table[(nid, state)] = True
                    continue
                work = []
                for nid in nodes:
                    ok = any(
                        table[(nid, state | {i})]
                        for i in dag.eligible(state)
                        if self._guaranteed(nid, dag.goals[i], landing)
                    )
                    table[(nid, state)] = ok
                    if ok:
                        work.append(nid)
                while work:  # if a node can complete, so can anyone who reaches it
                    b = work.pop()
                    for a in preds.get(b, ()):
                        if not table[(a, state)]:
                            table[(a, state)] = True
                            work.append(a)
        return table

    # ------------------------------------------------------------------
    # reachable (node, state) pairs along feasible walks from the client
    # ------------------------------------------------------------------

    def _reachable_states(self, api: str, dag: GoalDAG,
                          landing: Dict[str, FrozenSet[str]],
                          table: Dict[Tuple[str, GoalState], bool]
                          ) -> Set[Tuple[str, GoalState]]:
        start = (self.design.client_id, dag.initial_state())
        seen: Set[Tuple[str, GoalState]] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            nid, state = cur
            for i in dag.eligible(state):
                if self._guaranteed(nid, dag.goals[i], landing):
                    stack.append((nid, dag.advance(state, i)))
            for m in self.design.out_neighbors(nid, api=api):
                if table[(m, state)]:       # router only forwards to viable branches
                    stack.append((m, state))
        return seen

    # ------------------------------------------------------------------
    # landing-set fixpoint (T-020): grows monotonically from empty
    # ------------------------------------------------------------------

    def _landing_fixpoint(self) -> Dict[str, FrozenSet[str]]:
        entities = {g.entity for dag in self.dags.values()
                    for g in dag.goals if g.entity}
        landing: Dict[str, FrozenSet[str]] = {e: frozenset() for e in entities}
        while True:
            new: Dict[str, Set[str]] = {e: set() for e in entities}
            for api, dag in self.dags.items():
                if not any(g.kind in WRITE_KINDS for g in dag.goals):
                    continue
                table = self._build_table(api, dag, landing)
                for nid, state in self._reachable_states(api, dag, landing, table):
                    for i in dag.eligible(state):
                        g = dag.goals[i]
                        if (g.kind in WRITE_KINDS
                                and self._guaranteed(nid, g, landing)
                                and table[(nid, dag.advance(state, i))]):
                            new[g.entity].add(nid)
            frozen = {e: frozenset(s) for e, s in new.items()}
            if frozen == landing:
                return frozen
            landing = frozen

    # ------------------------------------------------------------------
    # lint support: which goal is the blocker for an infeasible API?
    # ------------------------------------------------------------------

    def first_blocked_goal(self, api: str) -> Optional[int]:
        """Explore ignoring completability (pure diagnosis): the first goal, in
        dependency order, that can never be advanced anywhere reachable."""
        dag = self.dags[api]
        seen: Set[Tuple[str, GoalState]] = set()
        stack = [(self.design.client_id, dag.initial_state())]
        advanceable: Set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            nid, state = cur
            for i in dag.eligible(state):
                if self._guaranteed(nid, dag.goals[i], self.landing):
                    advanceable.add(i)
                    stack.append((nid, dag.advance(state, i)))
            for m in self.design.out_neighbors(nid, api=api):
                stack.append((m, state))
        for i in range(len(dag.goals)):
            if i not in advanceable:
                return i
        return None


def build_feasibility(design: CanvasDesign, library: ComponentLibrary,
                      challenge: ChallengeRecord) -> Feasibility:
    return Feasibility(design, library, derive_challenge(challenge))


# ----------------------------------------------------------------------
# T-022: lint messages. No blocking -- errors still run (section 17.5).
# ----------------------------------------------------------------------

_BLOCKED_REASONS = {
    GoalKind.RETRIEVE: "'{api}' has no store in '{entity}'s landing set reachable "
                       "-- a read has nowhere authoritative to terminate",
    GoalKind.STORE: "'{api}' has no reachable store that accepts writes for "
                    "'{entity}' -- its landing set is empty (volatile stores with "
                    "write_policy 'write_around' do not accept write landings)",
    GoalKind.MUTATE: "'{api}' has no reachable store that can hold the mutable "
                     "state for '{entity}' -- its landing set is empty",
    GoalKind.COMPUTE: "'{api}' cannot reach a compute-capable node at the point "
                      "in its chain where computation is required",
    GoalKind.DELIVER: "'{api}' has no route to a client session to deliver "
                      "'{entity}'",
}


def lint(feas: Feasibility, challenge: ChallengeRecord) -> List[LintMessage]:
    messages: List[LintMessage] = []

    for api, dag in sorted(feas.dags.items()):
        if feas.api_feasible(api):
            continue
        blocked = feas.first_blocked_goal(api)
        if blocked is None:
            messages.append(LintMessage(
                severity="error", api=api,
                message=f"'{api}' cannot complete: each goal is satisfiable "
                        f"somewhere, but no single path completes them in "
                        f"dependency order with this wiring",
                offending_goal=None))
        else:
            goal = dag.goals[blocked]
            messages.append(LintMessage(
                severity="error", api=api,
                message=_BLOCKED_REASONS[goal.kind].format(
                    api=api, entity=goal.entity),
                offending_goal=goal_label(goal)))

    if challenge.slos.durability is not None:
        for entity, stores in sorted(feas.landing.items()):
            if not stores or not all(feas._volatile[nid] for nid in stores):
                continue
            store_types = sorted({feas.design.nodes[nid].type for nid in stores})
            for api, dag in sorted(feas.dags.items()):
                if any(g.kind in WRITE_KINDS and g.entity == entity
                       for g in dag.goals):
                    messages.append(LintMessage(
                        severity="warn", api=api,
                        message=f"'{api}' stores '{entity}' only in volatile "
                                f"in-memory store(s) ({', '.join(store_types)}): "
                                f"data will not survive node failure or eviction; "
                                f"the durability SLO "
                                f"({challenge.slos.durability}) is unlikely to "
                                f"be met",
                        offending_goal=None))
    return messages
