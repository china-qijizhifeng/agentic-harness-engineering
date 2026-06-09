# Long-Term Memory

Persistent knowledge that should be retained across sessions. Store important facts about the user, project conventions, architectural decisions, and recurring patterns here.

## Agent Added Memories

- Exact contract surfaces matter more than equivalent substitutes: if the evaluator names a literal public path, socket, artifact filename, or API signature, final validation must hit that exact surface.
- Once an evaluator-style end-to-end check passes, freeze the published state. Cleanup should be narrowly bounded; do not rerun live generators, reset webroots/repos, or rewrite git history after success unless new failing evidence demands it.
- Final validation must close the loop from the submitted artifact itself: reread the on-disk deliverable, recompute boundary-sensitive checks from that file/path, and treat copied helpers, hidden build dirs, loader env hacks, or scratch-only launchers as debug signals rather than publish proof.
- Custom diff/cmp-based validation scripts must fail fast; a later `passed` printout or exit 0 never overrides an earlier expected-vs-actual mismatch.
- Performance tasks need safety margin, not a single near-threshold pass. Prefer repeated alternating comparisons against the canonical baseline and publish only when headroom is clearly comfortable.

