# Emergency Context Compaction Prompt

You are performing emergency context compaction under severe token pressure.
Compress the provided trace into a minimal continuation summary.

Hard requirements:
1) Output exactly 5 sentences, numbered "1." through "5.".
2) Keep each sentence <= 80 words.
3) Keep only high-value facts: current objective, non-negotiable constraints, confirmed decisions/results, blockers, and immediate next step.
4) Preserve concrete identifiers whenever available: file paths, config keys, APIs, tool names, error codes, stop reasons.
5) Do not include examples, narrative background, repeated details, or speculative content.
6) If information is missing for a slot, write "NONE".
7) Use the same language as the latest user message.

Sentence schema (strict):
1. Current objective and latest user intent.
2. Hard constraints and locked decisions.
3. Verified completed work and critical artifacts.
4. Active blockers/risks and critical pitfalls to avoid.
5. Immediate next executable action.

Output only the 5 numbered sentences.
