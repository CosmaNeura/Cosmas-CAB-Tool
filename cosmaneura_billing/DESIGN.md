# CosmaNeura agentic billing layer — design & decisions

This document explains what I built on top of Homen's prototype, how it works,
the one place I changed the brief, and a security item you should act on this
week. It is written to be read by you, not just engineers.

## What the prototype already is

The repo (`FluencyMed-main`, pitch deck branded CosmaNeura) is a clinical
documentation and telehealth tool aimed at independent providers, with a pilot
described as a Christian healthcare group of ~180 physicians serving ~6,000
patients. It has five working features: clinical-note summarization, mapping of
clinical text to AHRQ Clinical Classifications Software Refined (CCSR) categories,
a patient-facing "leading questions" intake chatbot, an end-of-session summary
with a preliminary impression, and live-call transcription that also maps to CCSR.

Crucially, the README already names the exact feature you asked me to build: the
physician-signed note "is used to find top billing code suggestions with evidence
provided to enhance administrative process flow." So this is not a new direction —
it is the billing layer the prototype was always pointing at. I built it to consume
the prototype's own outputs: the intake text, the transcript, and the signed note.

## Architecture

The pipeline is five small agents behind one orchestrator. Each does one job and
hands a typed object to the next, so every step is inspectable and testable.

1. Coding agent — extracts the clinical facts from the documentation (an LLM call
   in production, deterministic offline), levels the E/M visit by published rules,
   and proposes ICD-10 diagnoses by mapping documented concepts to real codes.
   Every proposed code carries the span of text that supports it.
2. Documentation-integrity agent — rejects codes the documentation contradicts
   (in the demo, a family-history code when the patient explicitly denied family
   history), and flags documented-but-unaddressed findings as gaps rather than
   billing them (the patient's BMI is in the vitals but obesity is never addressed
   in the plan, so it is held, not coded).
3. Compliance guardrail — the hard gate. It can keep a code, downgrade an E/M
   level to what the documentation supports, or remove a code. It can never raise
   a level, add a code, or pass a code that lacks a documentation span. Because the
   only directions of travel are keep / downgrade / remove, the system is
   structurally incapable of upcoding. `prove_no_upcode.py` demonstrates this:
   feed it a 99215 against documentation that supports 99214 and it downgrades.
4. Denial-risk agent — approximates common payer edits (low-specificity codes,
   a diagnosis that supports the E/M, modifier checks). It lowers the chance of a
   clean claim being denied; it never raises reimbursement.
5. Feedback agent — emits the typed "subtle nudges" back into the platform.

The orchestrator assembles two role-scoped views from the same underlying result,
which matters because your two users have very different needs.

## The two end users

The provider sees a post-visit card built for thirty seconds of attention and
framed clinically: what was captured, documentation opportunities (phrased as
"capture what you already did," never "code higher to earn more"), clinically
indicated follow-up for the patient, and — for transparency — what was not billed
and why. The headline item is usually an under-coding correction: most independent
practices systematically under-code out of caution, and in the demo the visit was
submitted as 99213 when the documentation already supports 99214. Recovering
legitimately-earned revenue the practice is leaving on the table is most of the
financial value here, and none of it requires changing care.

The back-office biller sees a claim-ready worksheet: primary diagnosis first,
the E/M code, denial flags, whether anything needs provider sign-off, and a full
audit trail of every decision in order. That audit trail is the product's spine —
it is what makes a claim defensible to a payer or an auditor, and it is what a
compliance officer at a 180-physician group will ask to see before they buy.

## The feedback loop

You asked for "subtle nudges or prompts to help patient, provider and biller
maximize revenue while not degrading the doctor-patient experience." The loop is
built as typed events, each routed to a specific surface:

- documentation-gap and coding-education events go to the provider;
- care-quality events go to the provider on the patient's behalf;
- intake-completeness events go back to the intake chatbot so next time's history
  is more complete.

Every event aimed at a person must state a patient or clinical benefit, and the
feedback agent drops any that cannot. That constraint is enforced in code, not
just intended.

## One change from the brief, and why it makes the product stronger

The brief asked for nudges that help "patient, provider and biller maximize
revenue." I built everything except one piece: there is no path anywhere in the
system that nudges a patient toward additional billable services, and no event
that asks a provider to do, order, or document something for revenue rather than
for the patient. The patient-facing loop instead improves the completeness of the
clinical history, which genuinely helps both care and documentation.

I made that call deliberately, and I think it is the right one for this company
specifically, for three reasons. First, an AI system whose design goal is to
increase what gets billed by shaping the encounter is exactly the pattern that the
False Claims Act and OIG enforcement treat as healthcare fraud when it inflates
claims — and the system's own logs would be the evidence. Second, your pilot is a
Catholic/Christian physician group; a tool that could be characterized as steering
patients for money is an existential brand and trust risk with precisely that
buyer, and it runs against the ethical tradition they practice in, where the
patient's good is the end of medicine. Third, the defensible version is also the
more valuable one: "every code traces to documented, medically-necessary care"
is the sentence that closes deals with providers, payers, and compliance officers,
and it is the sentence the audit trail backs up. The system maximizes *captured,
defensible* revenue — which for an under-coding practice is a large, real number —
without ever optimizing the care decision or the patient's history for billing.

If by "maximize revenue" you meant capturing legitimately-earned work that the
practice currently under-codes, this build does exactly that and we are aligned.
If you did mean steering the encounter, that is the one thing I'd push back on,
and I'd want to talk it through before building it.

## Security item — act on this

The prototype repo commits live credentials: an OpenAI key file
(`openaiapikey.txt`), a `.env`, an OpenAI organization id, and a hard-coded
Pinecone API key inside `feature3`. Anyone with the repo has them. These should be
rotated now, removed from the git history (not just deleted in a new commit), and
moved to a secrets manager. For a company handling patient data this is both a
security and a compliance exposure.

## What is real vs. stubbed, and the path to production

Real in this build: the agent architecture and orchestration; the rule-based E/M
leveling; the ICD-10/CCSR lookup running on the prototype's authentic AHRQ data;
the guardrail invariant and its proof; the two role views and the typed feedback
loop. Stubbed for offline running: the clinical fact extraction uses a
deterministic model so the demo runs with no key — swap in `AnthropicLLM` (already
written) for real inference by setting `ANTHROPIC_API_KEY`.

Before this is production software you will need: a CPT license from the AMA for
the official code descriptors (the code numbers are usable, the long descriptors
are licensed — this code references codes by number with short generic labels on
purpose); real payer-specific edit rules (NCCI/LCD) behind the denial agent; a
HIPAA posture (BAAs with every model and infra vendor, PHI handling, audit
logging, access controls); and a certified-coder review loop during rollout to
calibrate the agents against human coders before any claim goes out unattended.

## Suggested next steps

A clickable provider card and biller worksheet wired to this engine (I can build
the front end next); connecting the feedback events to the existing intake chatbot
so the loop actually closes; and a small validation set of de-identified encounters
scored against a certified coder to measure capture accuracy and the under-coding
recovery rate, which is the metric that will sell this.
