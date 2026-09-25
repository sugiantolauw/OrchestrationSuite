TASK: Write the executive summary for this audit's workpaper.

Write two or three short paragraphs an executive will read verbatim, framed by the themes named in
PAYLOAD.theme_titles and the top findings in PAYLOAD.top_findings (already ordered most severe
first). The first paragraph must use {count:run_finding_count} and, only when PAYLOAD.placeholders
gives it a value, the headline placeholder {money:run_exposure_headline}.

Lead with the single most severe or most systemic result, not a generic overview. PAYLOAD.
top_findings is ordered most severe first, ties broken by that finding's own exposure amount --
top_findings[0] is this run's biggest STORY by severity, not necessarily the finding with the
largest amount at risk. Where its own `placeholders` list gives you a striking figure for it (for
example the share of a population affected), name that specific finding and cite that number using
a placeholder from ITS OWN `placeholders` list -- never invent one, and never use a placeholder
listed for a different finding. Do not merely restate the finding count and the headline in
general terms.

Never describe ANY finding with a superlative about exposure or amount at risk -- "highest
exposure", "largest amount at risk", "most exposure", "the primary/main contributor" and similar
-- unless that finding is PAYLOAD.placeholders' own {value:run_exposure_dominant_title}, the
payload's one declared largest-exposure contributor. Severity and exposure size are different
rankings; a finding ranked first by severity (top_findings[0]) is routinely NOT the one ranked
first by exposure.

When PAYLOAD.placeholders gives you {money:run_exposure_dominant_amount}, explain what drives the
headline: name the finding at {value:run_exposure_dominant_title} and state whether that amount is
spend under review or an excess over a threshold, using {value:run_exposure_dominant_basis}
exactly as given -- "spend" means the full amount is under review, not a confirmed loss; "excess"
means only the portion over a threshold is at risk. Never call either one a loss, a shortfall, or
otherwise confirmed. When PAYLOAD.placeholders also gives you
{money:run_approved_not_spent_total}, say plainly that this amount was approved but not spent and
is reported separately, excluded from the headline.

Do not list every finding; describe the shape of what this run found and why it matters. Do not
recommend a specific remediation here -- that belongs to each finding's own recommendation, not the
summary.
$generation_line
PAYLOAD:
$payload_json
