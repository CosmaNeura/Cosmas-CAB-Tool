"""The demo encounter -- the GI-bleed patient from FluencyMed feature1, expanded
into a full VisitBundle with an intake exchange, a short visit transcript, and a
physician-signed note. Submitted as 99213 on purpose, so the pipeline can show an
under-coding correction that the documentation already supports."""
from cosmaneura_billing.schemas import VisitBundle

SAMPLE_VISIT = VisitBundle(
    encounter_id="ENC-100237",
    patient_age=26,
    patient_sex="male",
    encounter_type="office_established",
    place_of_service="11",
    provider_specialty="Family Medicine",
    submitted_em_code="99213",          # what the provider selected before review
    submitted_icd10=["K62.5"],
    intake_text=(
        "Hi doctor, I am a 26 year old male. I am 5 feet and 9 inches tall and weigh 255 pounds. "
        "When I eat spicy food, I poop blood. Sometimes when I have constipation as well, I poop a "
        "little bit of blood. I am really scared that I have colon cancer. I do have diarrhea often. "
        "I do not have a family history of colon cancer. I got blood tests done last night. "
        "Please find my reports attached."
    ),
    transcript=(
        "Provider: Tell me about the bleeding - how much, what color, and how often?\n"
        "Patient: Bright red, a little, mostly after spicy food or when I'm constipated. A few times a month.\n"
        "Provider: Any weight loss, or change in your stools besides the blood?\n"
        "Patient: I've lost a bit of weight and I get diarrhea pretty often.\n"
        "Provider: I reviewed the CBC you brought - it's reassuring, no anemia. Given the bleeding and "
        "the weight change, I'm going to order a stool occult blood test today and refer you for a "
        "colonoscopy to be safe. We'll hold off on any new medication for now."
    ),
    signed_note=(
        "26 y/o male, established patient. CC: intermittent hematochezia. "
        "HPI: bright red blood per rectum, small volume, associated with spicy food and constipation; "
        "frequent diarrhea; reports mild weight loss. Denies family history of colorectal cancer. "
        "Data: reviewed outside CBC (no anemia). Ordered fecal occult blood test. "
        "Vitals: ht 69in, wt 255lb, BMI 37.6. "
        "Assessment/Plan: (1) Hematochezia - refer for diagnostic colonoscopy, FOBT today; "
        "(2) Constipation - dietary fiber, hydration; (3) Diarrhea - monitor. "
        "Return if bleeding increases or new symptoms. Electronically signed."
    ),
)
