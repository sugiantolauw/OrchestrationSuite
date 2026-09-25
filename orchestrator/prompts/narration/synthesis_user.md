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
IDENTIFIERS: each finding's own test id, given in PAYLOAD.findings[].test_id, may be written --
but only copied character-for-character in full, including any suffix such as "_dom" or "_air_dom".
Do not shorten it, drop a suffix, or invent a shorter form (for example, a test_id of "T6.1d_dom"
must be written "T6.1d_dom", never "T6.1d" -- the run may also have a separate "T6.1d_int" finding,
so the short form would not say which one you mean). The "key" field on each finding (for example
"T6_1d") is PAYLOAD's own internal label for this schema's finding_keys/severity_proposals arrays;
it is not a valid identifier and must never appear in prose.

PAYLOAD:
$payload_json
