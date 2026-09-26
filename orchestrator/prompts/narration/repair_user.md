This task's own rules still apply in full, unless a violation below requires changing what one of
them says:
$task_rules

Your previous output broke these rules (rule id, field, excerpt):
$violations_json

Your previous output:
$previous_output

Return the complete corrected JSON object, matching the same schema. Change only what the rules
above require. Every identifier, placeholder and field the violations do not name must stay
exactly as it was -- do not "fix" or rewrite anything the violations list does not point at. In
particular, never introduce a placeholder token that did not already appear in your previous
output unless a violation above specifically requires adding a missing one, and never build a new
placeholder name by analogy with one that already appears (for example, an existing
{value:run_high_finding_1_title} does not mean a matching "_share" or other suffixed name exists).
Every placeholder you write, new or kept, must appear character for character in this task's own
PLACEHOLDERS table.
