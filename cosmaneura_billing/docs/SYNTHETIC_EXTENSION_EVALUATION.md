# Blinded evaluation for the source-only synthetic extension

`cosmaneura-demo-source-extension-v1` is a demonstration and evaluation
artifact, not a training corpus. The installed package and deployment image
contain only eight wholly synthetic source encounters. They contain no
expected codes, answer labels, adjudication decisions, or model outputs.

Evaluation labels must be created and retained outside the repository,
installed wheel, container build context, application database, model request,
and demo UI. Use the following workflow:

1. Bind the immutable extension-pack digest and each case digest into a review
   record before a coder sees the source material.
2. Assign every case independently to two credentialed coders. Neither coder
   sees model output, the other coder's work, or the original four-case pack's
   embedded expectations.
3. Require every proposed billable code, including encounter, status, and
   screening codes, to include the current-fiscal-year identifier, source
   field, and an exact supporting quote. Record explicit exclusions separately.
   A code may not be both captured and excluded.
4. Send disagreements to a third credentialed adjudicator. Single-coder and
   unresolved cases remain provisional and are excluded from reported metrics.
5. Report active disease/symptom results separately from encounter, status, and
   screening-code results; never silently discard either lane. In particular,
   the normal-screening case tests absence of an active disease-or-symptom
   capture, not absence of every possible diagnosis code. Any applicable
   screening or encounter code remains subject to independent adjudication.
6. Store the final review record in an access-controlled evaluation location
   whose release process independently proves it is absent from the wheel,
   image, API, model input, and reviewer-facing demo.

The eight cases exercise multiple-confirmed-condition, duplicate-mention,
active-versus-resolved, symptom-versus-unconfirmed-cause, cross-document,
medication/test-only, unsupported-specificity, and normal-screening/no-active-
disease-or-symptom-capture shapes. Those shape descriptions are release checks,
not coding answers.

Because the installed artifact must remain code-free, its validator rejects
every bounded three-to-seven-character ICD-shaped alphanumeric token, in dotted
or compact form. This is intentionally fail-closed and also rejects clinical
look-alikes such as `A1C` and `H1N1`; source authors must spell those concepts
out instead of weakening the release boundary.
