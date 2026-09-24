TASK: Propose additional findings this run's rule set did not already write up.

PAYLOAD.tests lists every plan test this run computed, with its status, exception count and its
own metrics; each metric names the rule findings (PAYLOAD.rule_findings) that already cite it.
Propose at most $max_candidates additional findings, and only where a test shows exceptions and at
least one of its own metrics is not cited by any existing rule finding. Cite only metrics listed
for that test in PAYLOAD.tests[].metrics; never a metric belonging to a different test, and never
a run-level metric. Do not restate a finding that already exists in PAYLOAD.rule_findings, and
never propose a matter equivalent to one already decided in PAYLOAD.decided_candidates. Give each
candidate a proposed severity and a one-sentence reason for it. Returning an empty candidates list
is a good answer when nothing qualifies -- do not invent a candidate to avoid returning none.
$generation_line
IDENTIFIERS: each proposed candidate's own producing test id(s), given in PAYLOAD.tests[].test_id,
may be written exactly as shown.

PAYLOAD:
$payload_json
