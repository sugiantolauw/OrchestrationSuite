TASK: Group this run's findings into themes for the "What we found" section of the workpaper.

Group the findings listed in PAYLOAD.findings into at most six themes. A theme needs at least two
member findings, or exactly one High-severity finding on its own. Every finding belongs to at most
one theme; a finding need not belong to any theme. Give each theme a short title, a summary of
what it covers, and a root-cause hypothesis worded as a hypothesis ("may indicate", "could
reflect"), never as a claim of intent or cause. Add a review observation only for a pattern that
spans more than one test and that no single member finding's own observation already states; a
review observation is not a finding -- give it no severity and introduce no fact beyond what the
cited findings themselves already state. Propose an alternative severity for a finding only where
you genuinely disagree with the severity Python already computed, and give a one-sentence reason;
leave every finding you agree with out of severity_proposals entirely.
$generation_line
IDENTIFIERS: each finding's own test id, given in PAYLOAD.findings[].test_id, may be written
exactly as shown.

PAYLOAD:
$payload_json
