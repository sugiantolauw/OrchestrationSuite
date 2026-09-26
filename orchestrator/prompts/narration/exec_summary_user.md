TASK: Write the executive summary for this audit's workpaper.

Write two or three short paragraphs an executive will read verbatim, framed by the themes named in
PAYLOAD.theme_titles and the top findings in PAYLOAD.top_findings (already ordered most severe
first). The first paragraph must use {count:run_finding_count} and, only when PAYLOAD.placeholders
gives it a value, the headline placeholder {money:run_exposure_headline}.

LEAD PARAGRAPH -- the High-severity findings. When PAYLOAD.placeholders gives you one or more
run_high_finding_N_title entries (run_high_finding_1_title, run_high_finding_2_title, and so on),
these are this run's actual High-severity findings -- what failed, not a generic overview. Name
EVERY one of them in the lead paragraph using its own {value:run_high_finding_N_title} placeholder,
each with its own key figure (for example the share of a population affected) cited from the
matching item in PAYLOAD.top_findings[].placeholders -- never invent one, and never use a
placeholder listed for a different finding. Every key figure's placeholder must be one that
literally appears, character for character, in that finding's own PAYLOAD.top_findings[]
.placeholders entry. There is no run_high_finding_N_share or other placeholder built by analogy
with run_high_finding_N_title -- each High-severity finding's own key figure has a DIFFERENT name
(for example {pct:att_missing_pct} for one finding, {money:duplicate_amount} for another), never a
matching numbered pattern across findings. If PAYLOAD.placeholders gives you no
run_high_finding_N_title at all, this run has no High-severity finding -- lead instead with
PAYLOAD.top_findings[0], this run's most severe or most systemic result, the same way, citing a
striking figure from its own placeholders. Do not merely restate the finding count and the headline
in general terms.

SECOND PARAGRAPH -- the amount at risk and what drives it. When PAYLOAD.placeholders gives you
{money:run_exposure_dominant_amount}, explain what drives the {money:run_exposure_headline}
headline: name the finding at {value:run_exposure_dominant_title} and state whether that amount is
spend under review or an excess over a threshold, using {value:run_exposure_dominant_basis}
exactly as given -- "spend" means the full amount is under review, not a confirmed loss; "excess"
means only the portion over a threshold is at risk. Never call either one a loss, a shortfall, or
otherwise confirmed. When PAYLOAD.placeholders also gives you
{money:run_approved_not_spent_total}, say plainly and separately that this amount was approved but
not spent and is reported separately, excluded from the headline.

Severity and exposure size are two different rankings, routinely NOT the same finding: this run's
own largest-exposure contributor ({value:run_exposure_dominant_title}, second paragraph) is
frequently NOT one of its High-severity findings (lead paragraph, above). Never describe ANY
finding with a superlative about exposure or amount at risk -- "highest exposure", "largest amount
at risk", "most exposure" -- unless that finding is PAYLOAD.placeholders' own
{value:run_exposure_dominant_title}. Separately, NEVER call ANY finding "the top-ranked severity
issue", "the highest-severity finding", "the most severe finding", "the most serious finding", "the
primary finding", "the main finding", "the key finding", or "the top finding" unless that finding's
own title is one of PAYLOAD.placeholders' run_high_finding_N_title entries -- this applies even to
{value:run_exposure_dominant_title} itself: naming it as the driver of the exposure headline
(second paragraph) is correct; calling it "primary", "main", or "top" is not, unless it is ALSO one
of the High-severity findings named in the lead paragraph.

Do not list every finding; describe the shape of what this run found and why it matters. Do not
recommend a specific remediation here -- that belongs to each finding's own recommendation, not the
summary.
$generation_line
PAYLOAD:
$payload_json
