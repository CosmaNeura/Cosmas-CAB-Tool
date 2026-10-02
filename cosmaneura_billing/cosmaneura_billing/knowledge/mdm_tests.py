"""Versioned, provisional identities for named MDM diagnostic tests.

These are authored clinical names and conservative overlap sets, NOT CPT
descriptors, billing codes, a fee schedule, or evidence that a test is eligible
for MDM credit. The caller must verify source, order/review, date and actor.
An identity is one candidate data unit; its atoms are never separate credits.
Intersecting atom sets may not earn separate credits. Intersection is NOT
transitive: BMP and hepatic panel are disjoint, although CMP overlaps both.

Coverage is intentionally finite. Unspecified named CBC/UA and imaging side
use a union of the supported variants, so they cannot add credit beside a
specific variant. Undefined panels, methods, specimens and unfamiliar
qualifiers remain unresolved. Calculated results do not create extra assays.

Extension rule: add an explicit whole-name alias, stable key, authored label,
specimen/method/site-aware atoms, and positive, near-name negative and overlap
tests; obtain independent review and increment VERSION. Neither a CPT number
nor a LOINC name alone proves MDM identity/overlap. No runtime network lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

VERSION = "mdm-tests.2026-09-30.v1"


@dataclass(frozen=True)
class TestIdentity:
    key: str
    label: str
    components: frozenset[str]
    has_professional_component: bool


def _words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", text).casefold()))


_ALIASES: dict[tuple[str, ...], TestIdentity] = {}
_IDENTITIES: dict[str, TestIdentity] = {}


def _add(key: str, label: str, components, aliases: str | tuple[str, ...], *, professional=False):
    identity = TestIdentity(key, label, frozenset(components), professional)
    if not identity.components or key in _IDENTITIES:
        raise ValueError("Duplicate or empty MDM test identity")
    _IDENTITIES[key] = identity
    for alias in aliases.split("|") if isinstance(aliases, str) else aliases:
        normalized = _words(alias)
        if not normalized or normalized in _ALIASES and _ALIASES[normalized] != identity:
            raise ValueError("Conflicting MDM test alias")
        _ALIASES[normalized] = identity


def _lab(key: str, label: str, aliases: str, *, atom: str | None = None):
    _add("lab." + key, label, {"lab:" + (atom or key)}, aliases)


# Specimen and analytical method are included when they distinguish tests.
# Short ambiguous element symbols, generic 'hormone/thyroid/respiratory panel',
# 'pregnancy test', 'COVID test', and 'rapid strep' are deliberately absent.
for _key, _label, _aliases in (
    ("sodium", "Sodium", "sodium|serum sodium|sodium level"),
    ("potassium", "Potassium", "potassium|serum potassium|potassium level"),
    ("chloride", "Chloride", "chloride|serum chloride"),
    ("bicarbonate", "Bicarbonate / total carbon dioxide", "bicarbonate|serum bicarbonate|total carbon dioxide|serum total co2"),
    ("bun", "Blood urea nitrogen", "bun|blood urea nitrogen"),
    ("creatinine", "Serum creatinine", "creatinine|serum creatinine"),
    ("glucose", "Blood glucose", "glucose|blood glucose|serum glucose|fasting glucose|fasting plasma glucose"),
    ("calcium", "Total calcium", "calcium|total calcium|serum calcium"),
    ("calcium_ionized", "Ionized calcium", "ionized calcium|ionised calcium"),
    ("albumin", "Serum albumin", "albumin|serum albumin"),
    ("protein_total", "Serum total protein", "total protein|serum total protein"),
    ("bilirubin_total", "Total bilirubin", "total bilirubin|serum total bilirubin"),
    ("bilirubin_direct", "Direct bilirubin", "direct bilirubin|conjugated bilirubin"),
    ("alkaline_phosphatase", "Alkaline phosphatase", "alkaline phosphatase|alk phos|alp"),
    ("ast", "Aspartate aminotransferase", "ast|aspartate aminotransferase|sgot"),
    ("alt", "Alanine aminotransferase", "alt|alanine aminotransferase|sgpt"),
    ("cholesterol_total", "Total cholesterol", "total cholesterol"),
    ("cholesterol_hdl", "HDL cholesterol", "hdl cholesterol|hdl c"),
    ("triglycerides", "Triglycerides", "triglycerides|triglyceride level"),
    ("cholesterol_ldl_direct", "Direct LDL cholesterol", "direct ldl|direct ldl cholesterol|ldl cholesterol direct"),
    ("magnesium", "Serum magnesium", "magnesium|serum magnesium"),
    ("phosphorus", "Serum phosphorus", "phosphorus|serum phosphorus|serum phosphate"),
    ("a1c", "Hemoglobin A1c", "a1c|hba1c|hemoglobin a1c|haemoglobin a1c|glycated hemoglobin|poc a1c|point of care hemoglobin a1c"),
    ("tsh", "Thyroid stimulating hormone", "tsh|thyroid stimulating hormone|thyrotropin"),
    ("t4_free", "Free thyroxine", "free t4|ft4|free thyroxine"),
    ("t4_total", "Total thyroxine", "total t4|total thyroxine"),
    ("t3_free", "Free triiodothyronine", "free t3|ft3|free triiodothyronine"),
    ("t3_total", "Total triiodothyronine", "total t3|total triiodothyronine"),
    ("tsh_receptor_antibody", "TSH receptor antibody", "tsh receptor antibody|tsh receptor antibodies|thyrotropin receptor antibody|trab"),
    ("thyroid_peroxidase_antibody", "Thyroid peroxidase antibody", "thyroid peroxidase antibody|thyroid peroxidase antibodies|tpo antibody|tpo antibodies"),
    ("ferritin", "Ferritin", "ferritin|serum ferritin"),
    ("iron", "Serum iron", "serum iron|iron level"),
    ("tibc", "Total iron binding capacity", "tibc|total iron binding capacity"),
    ("transferrin", "Transferrin", "transferrin|serum transferrin"),
    ("b12", "Vitamin B12", "vitamin b12|b12 level|cobalamin"),
    ("folate", "Serum folate", "serum folate|folate level"),
    ("vitamin_d_25", "25-hydroxy vitamin D", "25 hydroxy vitamin d|25 oh vitamin d|vitamin d 25 hydroxy"),
    ("vitamin_d_125", "1,25-dihydroxy vitamin D", "1 25 dihydroxy vitamin d|1 25 oh2 vitamin d"),
    ("crp", "C reactive protein", "crp|c reactive protein"),
    ("crp_high_sensitivity", "High sensitivity C reactive protein", "hs crp|high sensitivity crp|high sensitivity c reactive protein"),
    ("esr", "Erythrocyte sedimentation rate", "esr|erythrocyte sedimentation rate|sedimentation rate"),
    ("lipase", "Lipase", "lipase|serum lipase"),
    ("uric_acid", "Uric acid", "uric acid|serum uric acid"),
    ("fsh", "Follicle stimulating hormone", "fsh|follicle stimulating hormone"),
    ("lh", "Luteinizing hormone", "lh|luteinizing hormone|luteinising hormone"),
    ("estradiol", "Estradiol", "estradiol|oestradiol|serum estradiol"),
    ("progesterone", "Progesterone", "progesterone|serum progesterone"),
    ("hydroxyprogesterone_17", "17-hydroxyprogesterone", "17 hydroxyprogesterone|17 oh progesterone"),
    ("prolactin", "Prolactin", "prolactin|serum prolactin"),
    ("amh", "Anti-Mullerian hormone", "amh|anti mullerian hormone|antimullerian hormone"),
    ("testosterone_total", "Total testosterone", "total testosterone|serum total testosterone"),
    ("testosterone_free", "Free testosterone", "free testosterone|measured free testosterone"),
    ("dhea", "Dehydroepiandrosterone", "dhea|dehydroepiandrosterone"),
    ("dheas", "Dehydroepiandrosterone sulfate", "dheas|dhea s|dhea sulfate|dehydroepiandrosterone sulfate"),
    ("shbg", "Sex hormone binding globulin", "shbg|sex hormone binding globulin"),
    ("hcg_serum_quantitative", "Quantitative serum hCG", "quantitative serum hcg|serum quantitative hcg|serum hcg quantitative|quantitative serum beta hcg|serum beta hcg quantitative"),
    ("hcg_serum_qualitative", "Qualitative serum hCG", "qualitative serum hcg|serum qualitative hcg|serum hcg qualitative"),
    ("hcg_urine_qualitative", "Qualitative urine hCG", "urine hcg|qualitative urine hcg|urine pregnancy test|urine beta hcg"),
    ("wbc", "White blood cell count", "white blood cell count|wbc count"),
    ("rbc", "Red blood cell count", "red blood cell count|rbc count"),
    ("hemoglobin", "Hemoglobin", "hemoglobin|haemoglobin|hemoglobin level"),
    ("hematocrit", "Hematocrit", "hematocrit|haematocrit"),
    ("platelets", "Platelet count", "platelet count|platelets"),
    ("differential_automated", "Automated blood cell differential", "automated differential|automated white blood cell differential"),
    ("differential_manual", "Manual blood cell differential", "manual differential|manual white blood cell differential"),
    ("urine_culture", "Urine culture", "urine culture|urine bacterial culture"),
    ("urine_albumin", "Urine albumin", "urine albumin|urine microalbumin"),
    ("urine_creatinine", "Urine creatinine", "urine creatinine"),
    ("urine_protein", "Urine protein", "urine protein|urine total protein"),
    ("pt", "Prothrombin time", "prothrombin time|pt inr"),
    ("aptt", "Activated partial thromboplastin time", "aptt|activated partial thromboplastin time|partial thromboplastin time"),
    ("covid_naat", "SARS-CoV-2 nucleic acid test", "sars cov 2 naat|sars cov 2 pcr|covid 19 pcr|covid pcr|covid naat"),
    ("covid_antigen", "SARS-CoV-2 antigen test", "sars cov 2 antigen|covid antigen|covid 19 antigen"),
    ("flu_a_naat", "Influenza A nucleic acid test", "influenza a naat|influenza a pcr"),
    ("flu_b_naat", "Influenza B nucleic acid test", "influenza b naat|influenza b pcr"),
    ("strep_a_antigen", "Group A streptococcal antigen", "group a strep antigen|streptococcus group a antigen|rapid strep antigen"),
    ("strep_a_naat", "Group A streptococcal nucleic acid test", "group a strep naat|group a strep pcr|strep a pcr"),
    ("throat_culture", "Throat bacterial culture", "throat culture|throat bacterial culture"),
    ("chlamydia_naat", "Chlamydia trachomatis nucleic acid test", "chlamydia naat|chlamydia pcr|chlamydia trachomatis naat"),
    ("gonorrhea_naat", "Neisseria gonorrhoeae nucleic acid test", "gonorrhea naat|gonorrhoea naat|gonorrhea pcr|neisseria gonorrhoeae naat"),
    ("hiv_ag_ab", "HIV antigen and antibody test", "hiv antigen antibody|hiv ag ab|fourth generation hiv test|hiv 4th generation test"),
    ("hiv_rna", "HIV RNA test", "hiv rna|hiv rna pcr|hiv viral load"),
    ("hcv_antibody", "Hepatitis C antibody", "hepatitis c antibody|hcv antibody|hcv ab"),
    ("hcv_rna", "Hepatitis C RNA test", "hepatitis c rna|hcv rna|hcv rna pcr"),
    ("hb_surface_antigen", "Hepatitis B surface antigen", "hepatitis b surface antigen|hbsag"),
    ("hb_surface_antibody", "Hepatitis B surface antibody", "hepatitis b surface antibody|hepatitis b surface antibodies|anti hbs"),
    ("hb_core_antibody_total", "Total hepatitis B core antibody", "total hepatitis b core antibody|hepatitis b core total antibody"),
    ("rpr", "Rapid plasma reagin", "rpr|rapid plasma reagin"),
):
    _lab(_key, _label, _aliases)


def _atoms(*keys):
    return frozenset("lab:" + key for key in keys)


_BMP = _atoms("sodium", "potassium", "chloride", "bicarbonate", "bun", "creatinine", "glucose", "calcium")
_LIVER = _atoms("albumin", "protein_total", "bilirubin_total", "alkaline_phosphatase", "ast", "alt")
_CBC = _atoms("wbc", "rbc", "hemoglobin", "hematocrit", "platelets", "rbc_indices")
_DIFF = _atoms("differential_automated", "differential_manual")
_add("lab.bmp", "Basic metabolic panel", _BMP, "bmp|basic metabolic panel")
_add("lab.cmp", "Comprehensive metabolic panel", _BMP | _LIVER, "cmp|comprehensive metabolic panel")
_add("lab.hepatic_panel", "Hepatic function panel", _LIVER | _atoms("bilirubin_direct"), "hepatic function panel|liver function panel")
_add("lab.lipid_panel", "Lipid panel", _atoms("cholesterol_total", "cholesterol_hdl", "triglycerides"), "lipid panel|lipid profile|fasting lipid panel")
_add("lab.cbc_unspecified", "CBC (differential unspecified)", _CBC | _DIFF, "cbc|complete blood count")
_add("lab.cbc_no_diff", "CBC without differential", _CBC, "cbc without differential|cbc without diff|complete blood count without differential")
_add("lab.cbc_diff_unspecified", "CBC with differential (method unspecified)", _CBC | _DIFF, "cbc with differential|cbc with diff|complete blood count with differential")
_add("lab.cbc_auto_diff", "CBC with automated differential", _CBC | _atoms("differential_automated"), "cbc with automated differential|complete blood count with automated differential|cbc automated differential|cbc with an automated differential")
_add("lab.cbc_manual_diff", "CBC with manual differential", _CBC | _atoms("differential_manual"), "cbc with manual differential|complete blood count with manual differential|cbc manual differential|cbc with a manual differential")
_add("lab.ua_unspecified", "Urinalysis (microscopy unspecified)", _atoms("urine_chemistry", "urine_microscopy"), "urinalysis|ua")
_add("lab.ua_dipstick", "Urine dipstick / chemical urinalysis", _atoms("urine_chemistry"), "urine dipstick|dipstick urinalysis|urinalysis dipstick|urinalysis without microscopy|automated urine dipstick|manual urine dipstick")
_add("lab.ua_microscopy", "Urine microscopy", _atoms("urine_microscopy"), "urine microscopy|urine microscopic examination|urine sediment microscopy")
_add("lab.ua_with_microscopy", "Urinalysis with microscopy", _atoms("urine_chemistry", "urine_microscopy"), "urinalysis with microscopy|ua with microscopy|urinalysis with microscopic examination")
_add("lab.urine_acr", "Urine albumin/creatinine ratio", _atoms("urine_albumin", "urine_creatinine"), "urine albumin creatinine ratio|urine microalbumin creatinine ratio|urine acr|uacr")
_add("lab.urine_pcr", "Urine protein/creatinine ratio", _atoms("urine_protein", "urine_creatinine"), "urine protein creatinine ratio|urine pcr")
_add("lab.hemoglobin_hematocrit", "Hemoglobin and hematocrit", _atoms("hemoglobin", "hematocrit"), "hemoglobin and hematocrit|h and h")
_add("lab.covid_flu_naat", "SARS-CoV-2 and influenza A/B nucleic acid panel", _atoms("covid_naat", "flu_a_naat", "flu_b_naat"), "sars cov 2 influenza a b naat panel|covid influenza a b pcr panel|covid flu a b naat panel")
_add("lab.ct_gc_naat", "Chlamydia and gonorrhea nucleic acid panel", _atoms("chlamydia_naat", "gonorrhea_naat"), "chlamydia gonorrhea naat|chlamydia gonorrhea pcr|chlamydia and gonorrhea naat")


# Radiographs preserve anatomy and laterality. Known view counts retain their
# label/key but overlap within the same side/site; extra views do not silently
# create extra credit. Unspecified and bilateral laterality overlap both sides.
for _site in ("knee", "ankle", "foot", "hand", "wrist", "elbow", "shoulder", "hip"):
    for _side in ("unspecified", "right", "left", "bilateral"):
        _sides = ("right", "left") if _side in {"unspecified", "bilateral"} else (_side,)
        _components = {f"imaging:xray:{_site}:{side}" for side in _sides}
        _site_phrase = _site if _side == "unspecified" else _side + " " + _site
        _names = tuple(name for modality in ("x ray", "xray", "radiograph", "radiographs")
                       for name in (_site_phrase + " " + modality, modality + " of the " + _site_phrase,
                                    modality + " " + _site_phrase))
        if _side == "bilateral":
            _names += tuple(sides + " " + _site + " " + modality
                            for sides in ("right and left", "left and right")
                            for modality in ("x ray", "xray", "radiograph", "radiographs"))
            _names += tuple(first + " " + _site + " and " + second + " " + _site + " " + modality
                            for first, second in (("right", "left"), ("left", "right"))
                            for modality in ("x ray", "xray", "radiograph", "radiographs"))
        for _views in (None, 1, 2, 3, 4):
            _aliases = _names if _views is None else tuple(
                name + " " + str(_views) + (" view" if _views == 1 else " views") for name in _names)
            _label = _site_phrase.capitalize() + " radiograph" + (f" ({_views} views)" if _views else "")
            _add(f"imaging.xray.{_site}.{_side}.views{_views or 'unspecified'}", _label, _components, _aliases, professional=True)

for _key, _label, _components, _aliases in (
    ("chest_xray", "Chest radiograph (views unspecified)", {"imaging:xray:chest"}, "chest x ray|chest xray|chest radiograph|chest radiographs|cxr"),
    ("chest_xray_1view", "Chest radiograph (1 view)", {"imaging:xray:chest"}, "chest xray 1 view|chest x ray 1 view|single view chest xray|portable chest xray 1 view"),
    ("chest_xray_2view", "Chest radiograph (2 views)", {"imaging:xray:chest"}, "chest xray 2 views|chest x ray 2 views|two view chest xray|chest xray pa and lateral"),
    ("pelvis_xray", "Pelvis radiograph", {"imaging:xray:pelvis"}, "pelvis xray|pelvis x ray|pelvic radiograph"),
    ("lumbar_xray", "Lumbar spine radiograph", {"imaging:xray:lumbar"}, "lumbar spine xray|lumbar spine x ray|lumbar spine radiograph"),
    ("pelvic_us_transabdominal", "Transabdominal pelvic ultrasound", {"imaging:us:pelvis:transabdominal"}, "transabdominal pelvic ultrasound|transabdominal pelvic sonogram|pelvic ultrasound transabdominal"),
    ("pelvic_us_transvaginal", "Transvaginal pelvic ultrasound", {"imaging:us:pelvis:transvaginal"}, "transvaginal ultrasound|transvaginal pelvic ultrasound|transvaginal sonogram|tvus|pelvic ultrasound transvaginal"),
    ("pelvic_us_unspecified", "Pelvic ultrasound (technique unspecified)", {"imaging:us:pelvis:transabdominal", "imaging:us:pelvis:transvaginal"}, "pelvic ultrasound|pelvic sonogram"),
    ("thyroid_us", "Thyroid ultrasound", {"imaging:us:thyroid"}, "thyroid ultrasound|thyroid sonogram"),
    ("renal_us", "Renal ultrasound", {"imaging:us:renal"}, "renal ultrasound|kidney ultrasound|renal sonogram"),
    ("ecg_unspecified", "Electrocardiogram (lead configuration unspecified)", {"cardiac:ecg:12lead", "cardiac:ecg:rhythm"}, "ecg|ekg|electrocardiogram"),
    ("ecg_12_lead", "12-lead electrocardiogram", {"cardiac:ecg:12lead"}, "12 lead ecg|12 lead ekg|12 lead electrocardiogram|ecg 12 lead|ekg 12 lead"),
    ("ecg_rhythm", "ECG rhythm strip", {"cardiac:ecg:rhythm"}, "ecg rhythm strip|ekg rhythm strip|electrocardiogram rhythm strip"),
):
    _add("study." + _key, _label, _components, _aliases, professional=True)


def resolve_test(subject: str) -> TestIdentity | None:
    """Resolve a complete alias only; unknown qualifiers never lose meaning."""
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 300:
        return None
    # Hyphens, slashes and grouping parentheses may spell one known alias.
    # Sentence/list separators cannot turn independently named tests into one.
    if re.search(r"[;:.\n?!]|(?<!\d),|,(?!\d)", subject.strip().rstrip(".")):
        return None
    return _ALIASES.get(_words(subject))


# Mention recognition is intentionally narrower than general clinical NLP.
# Complete explicit test names may occur in lists or ordinary order/result
# prose. Unknown words immediately adjoining a name are not discarded as a
# guessed test modifier (e.g. TSH receptor binding assay, free beta-hCG).
_BEFORE = frozenset((
    "a an the and or plus with obtain obtained order ordered ordering review reviewed reviewing "
    "repeat repeated check checked checking shows showed include included includes including "
    "performed perform collected collect requested request pending prior external recent new "
    "for of had has her his their my patient report reported results result normal abnormal elevated low "
    "consider considered considering interpret interpreted interpreting read reading discussed discuss "
    "declined decline refused refuse cancelled canceled cancel recommended recommend no not"
).split())
_AFTER = frozenset((
    "was were is are showed shows show returned resulted remains remained pending normal "
    "abnormal elevated low negative positive ordered reviewed obtained performed collected "
    "today yesterday tomorrow on dated from to in at of result results level value "
    "values reported requested will would should can may has had interpretation interpretations "
    "if when because while although despite after before following declined refused cancelled canceled no not"
).split())
_CONNECTORS = frozenset({"and", "or", "plus"})
_BY_FIRST: dict[str, list[tuple[tuple[str, ...], TestIdentity]]] = {}
for _alias, _identity in _ALIASES.items():
    _BY_FIRST.setdefault(_alias[0], []).append((_alias, _identity))
for _matches in _BY_FIRST.values():
    _matches.sort(key=lambda entry: len(entry[0]), reverse=True)


def test_mentions(text: str) -> tuple[TestIdentity, ...]:
    """Return distinct, explicitly named identities in source order.

    This identifies names only: negated/conditional/historical mentions remain
    names, NOT eligible orders/results. The source qualifier owns assertion and
    MDM eligibility. Longer specific aliases shadow generic embedded aliases.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
        return ()
    normalized = unicodedata.normalize("NFKC", text).casefold()
    spans = list(re.finditer(r"[a-z0-9]+", normalized))
    words = tuple(m.group() for m in spans)
    candidates = []
    for start, word in enumerate(words):
        for alias, identity in _BY_FIRST.get(word, ()):
            end = start + len(alias)
            if words[start:end] == alias:
                gaps = [normalized[spans[i].end():spans[i + 1].start()]
                        for i in range(start, end - 1)]
                if any(re.search(r"[;:.!?\n]", gap) or "," in gap
                       and not (words[start + i].isdigit() and words[start + i + 1].isdigit())
                       for i, gap in enumerate(gaps)):
                    continue
                candidates.append((start, end, identity))
                break  # Never fall back from a rejected specific name.
    starts = {start for start, _, _ in candidates}
    result = []
    consumed = -1
    for start, end, identity in candidates:
        if start < consumed:
            continue
        # A longer explicit name consumes its embedded generic names even when
        # its surrounding phrase is unsupported.
        consumed = end
        before_gap = normalized[spans[start - 1].end():spans[start].start()] if start else ""
        after_gap = normalized[spans[end - 1].end():spans[end].start()] if end < len(spans) else ""
        before_ok = start == 0 or bool(re.search(r"[\n,;:.(]", before_gap)) or words[start - 1] in _BEFORE
        if end == len(words) or re.search(r"[\n.!?]", after_gap):
            after_ok = True
        elif words[end].isdigit():
            # A number can be a lab result, but an unsupported number of
            # views/leads/parts must not downgrade to the generic study.
            after_ok = not any(word in {"view", "views", "lead", "leads", "part", "parts", "generation"}
                               for word in words[end + 1:end + 3])
        elif words[end] in _AFTER:
            after_ok = True
        elif words[end] in _CONNECTORS:
            after_ok = end + 1 in starts or end + 1 < len(words) and words[end + 1] in _BEFORE
        elif re.search(r"[,;:)]", after_gap) and end in starts:
            after_ok = True
        else:
            after_ok = False
        if before_ok and not after_ok:
            # An unknown modifier does not authorize a nested constituent as
            # a separate test: e.g. CBC (with an unusual manual differential).
            # Stop suppression at the next explicit list/sentence boundary.
            boundary = end
            while boundary < len(words):
                gap = normalized[spans[boundary - 1].end():spans[boundary].start()]
                if boundary > end and re.search(r"[,;.!?\n]", gap):
                    break
                boundary += 1
            consumed = boundary
        if before_ok and after_ok and identity not in result:
            result.append(identity)
    return tuple(result)


# Keep pytest from treating an imported API function as a test case.
test_mentions.__test__ = False
TestIdentity.__test__ = False
