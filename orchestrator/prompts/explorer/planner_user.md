OBJECTIVE (written by the auditor):
<<<
$objective
>>>

AUDIT PERIOD: $audit_period
BUSINESS UNIT: $business_unit
MATERIALITY: $materiality

PROFILE (aggregates only):
$profile_json

PRIMITIVES (name, purpose, parameter schema, metric kinds):
$primitives_json

REFERENCE SKILLS (approved examples of how tests, thresholds and finding rules are written; their
columns belong to other data and must not be used unless they also appear in PROFILE):
$reference_skills_json

Return one PlanProposal.
