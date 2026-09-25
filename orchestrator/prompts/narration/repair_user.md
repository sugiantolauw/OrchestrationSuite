This task's own rules still apply in full, unless a violation below requires changing what one of
them says:
$task_rules

Your previous output broke these rules (rule id, field, excerpt):
$violations_json

Your previous output:
$previous_output

Return the complete corrected JSON object, matching the same schema. Change only what the rules
above require. Every identifier, placeholder and field the violations do not name must stay
exactly as it was -- do not "fix" or rewrite anything the violations list does not point at.
