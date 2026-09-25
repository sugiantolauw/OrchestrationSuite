TASK: Group this run's findings into themes for the "What we found" section of the workpaper.

Group the findings listed in PAYLOAD.findings into at most six themes. A theme needs at least two
member findings, or exactly one High-severity finding on its own. Every finding belongs to at most
one theme; a finding need not belong to any theme. Give each theme a short title and a summary of
what it covers.

The root-cause hypothesis must be SPECIFIC to this theme's own member findings, worded as a
hypothesis ("may indicate", "could reflect"), never as a claim of intent or cause. A generic
sentence that could equally describe any audit theme ("may indicate gaps in oversight
procedures", "could reflect inconsistencies between systems") is not acceptable, even though it is
technically hedged -- it is not grounded in what THIS theme's findings actually show. Instead, name
which of the theme's own tests (their test_id, per the IDENTIFIERS rule below) you are drawing the
hypothesis from and what pattern connects them -- for example, that two tests' exceptions both
concentrate in the same population, or that one test's high exception rate and another's
concentration in a small set of employees, taken together, point toward a shared underlying gap.
Every fact you point to must already be visible in PAYLOAD.findings' own fields for that theme's
members (title, test_id, severity, monetary_basis, or a value from that finding's own
`placeholders`); never introduce a number, population or comparison the payload does not already
give you for one of the theme's own findings.

Add a review observation only for a pattern that spans more than one test and that no single
member finding's own observation already states; a review observation is not a finding -- give it
no severity and introduce no fact beyond what the cited findings themselves already state. Propose
an alternative severity for a finding only where you genuinely disagree with the severity Python
already computed, and give a one-sentence reason; leave every finding you agree with out of
severity_proposals entirely.
$generation_line
IDENTIFIERS: PAYLOAD.identifiers lists one {key, test_ids} pair per finding. These are two
different kinds of identifier for two different places, and they are never interchangeable:

- `key` (for example "T6_1d") goes ONLY in this schema's own JSON fields --
  themes[].finding_keys and severity_proposals[].finding_key. Copy it character-for-character
  from PAYLOAD.identifiers; never edit it, never add a suffix to it, and never write it in prose.
- `test_ids` (for example ["T6.1d_dom"]) is what you write in prose -- title, summary,
  root_cause_hypothesis, review_observations. Copy the id in full, including any suffix such as
  "_dom" or "_air_dom"; do not shorten it, drop the suffix, or invent a shorter form (a test id of
  "T6.1d_dom" must be written "T6.1d_dom", never "T6.1d" -- the run may also have a separate
  "T6.1d_int" finding, so the short form would not say which one you mean). Never write a `key`
  value in prose, and never write a `test_ids` value in finding_keys/severity_proposals.

PAYLOAD:
$payload_json
