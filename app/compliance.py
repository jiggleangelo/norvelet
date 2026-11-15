from .models import CheckResult

RULEPACK_VERSION = 'demo-2026-10-24'


def run(spec):
    out = []
    def add(i, severity, title, detail, action):
        out.append(CheckResult(i, severity, title, detail, action, None))
    if spec.tee:
        add('TEX-FIBER', 'info', 'Fiber label', 'Fiber content must use generic names and percentages.', 'Confirm against the blank supplier spec sheet.')
        unknown = any(v == 'Unknown' for v in spec.tee.origin.values())
        add('TEX-ORIGIN', 'warn' if unknown or not spec.origin_user_asserted else 'info', 'Country of origin', 'Country-of-origin labeling is required.', 'Confirm blank, sewing and print origins using supplier evidence.')
        add('TEX-IDENTITY', 'info', 'Dealer identity', 'Label needs the manufacturer/dealer name or RN.', 'Confirm the displayed identity.')
        add('TEX-CARE', 'info', 'Care label', 'Generated care text is a draft.', 'Confirm a permanent care label under the FTC rule.')
        if spec.tee.method == 'screen':
            add('P65-INK', 'warn', 'Ink review', 'Plastisol or other screen inks may require review.', 'Request a Prop 65 supplier statement.')
    if spec.audience == 'children':
        add('CPSIA-CHILD', 'block', "Children's product unsupported", 'Testing and certificates are outside this pilot.', 'Children’s products cannot be ordered in this pilot.')
    add('P65-GENERAL', 'info', 'California Prop 65', 'The brand remains responsible for applicable warnings.', 'Request supplier declarations.')
    claims = set(spec.claims)
    if 'eco_friendly' in claims:
        add('CLM-ECO', 'warn', 'Eco-friendly claim', 'Unqualified general environmental claim.', 'Use a specific, substantiated claim.')
    if 'recyclable' in claims:
        add('CLM-RECYCLE', 'warn', 'Recyclable claim', 'Access to recycling may require qualification.', 'Substantiate and qualify by product and market.')
    if 'degradable' in claims:
        add('CLM-DEGRADE', 'block', 'Degradability claim', 'Competent, reliable evidence is required.', 'Remove it or obtain certification and counsel sign-off.')
    if 'organic' in claims:
        add('CLM-ORGANIC', 'block', 'Organic claim', 'The demo blank has no organic certification.', 'Select a certified blank and obtain evidence.')
    if 'made_in_usa' in claims:
        supported = spec.tee and spec.origin_user_asserted and all(v == 'USA' for v in spec.tee.origin.values())
        add('CLM-USA', 'warn' if supported else 'block', 'Made in USA claim',
            'User-supplied origin attestation; not independently verified.' if supported else 'Blank, sewing and printing must be confirmed as US-origin.',
            'Obtain counsel review of the all-or-virtually-all standard and separate packaging evidence.' if supported else 'Remove the claim or confirm changed sourcing for blank, sewing and printing.')
    if 'non_toxic' in claims:
        add('CLM-NONTOXIC', 'warn', 'Non-toxic claim', 'The whole-product claim needs substantiation.', 'Obtain evidence covering inks and materials.')
    if spec.box:
        add('PKG-EPR', 'info', 'Packaging EPR', 'State producer obligations can vary.', 'Track units shipped by state.')
        add('PKG-BMC', 'info', "Box maker's certificate", 'The manufacturer certifies the final rating.', 'Confirm the production BMC.')
    order = {'block': 0, 'warn': 1, 'info': 2}
    return sorted(out, key=lambda r: (order[r.severity], r.rule_id))


def blocking(results):
    return any(r.severity == 'block' for r in results)
