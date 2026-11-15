from .compliance import run
from .spec import ARTWORK


def _inches(mil):
    return '{:.3f}'.format(mil / 1000).rstrip('0').rstrip('.')


def _mm(mil):
    return '{:.2f}'.format(mil * 0.0254)


def build_rfq(spec):
    checks = run(spec)
    blockers = [item.title for item in checks if item.severity == 'block']
    lines = []
    if blockers:
        lines.append('UNRESOLVED BLOCKERS: ' + '; '.join(blockers))
        lines.append('Resolve before sending.')
        lines.append('')
    lines.extend(['REQUEST FOR QUOTE', 'Brand: ' + spec.brand, 'Specification version: ' + str(spec.version), ''])
    if spec.box:
        box = spec.box
        lines.extend([
            'BOXES',
            'Quantity: ' + str(box.qty),
            'Inside L × W × H: {} × {} × {} in; {} × {} × {} mm'.format(
                _inches(box.L_mil), _inches(box.W_mil), _inches(box.H_mil),
                _mm(box.L_mil), _mm(box.W_mil), _mm(box.H_mil)),
            'Board: ' + box.board,
            'Print sides: ' + ', '.join(box.print_sides),
            'Ink colors: ' + str(box.ink_colors),
            'Dimension tolerance: ±{} in (±{} mm)'.format(_inches(box.tolerance_mil), _mm(box.tolerance_mil)),
            ARTWORK,
            '',
        ])
    if spec.tee:
        tee = spec.tee
        lines.extend([
            'TEES',
            'Quantity: ' + str(tee.qty),
            'Color: ' + tee.color,
            'Fiber: ' + ', '.join('{}% {}'.format(percent, name) for name, percent in tee.fiber),
            'Sizes: ' + ', '.join('{} {}'.format(name, tee.sizes[name]) for name in ('S', 'M', 'L', 'XL')),
            'Print method: ' + tee.method.upper(),
            'Placements: ' + ', '.join(tee.placements),
            'Origins: blank {}; sewing {}; print {}'.format(tee.origin['blank'], tee.origin['sewing'], tee.origin['print']),
            'Placement tolerance: ±0.5 in.',
            ARTWORK,
            'Label draft: ' + tee.label_content,
            'Care draft: ' + tee.care,
            '',
        ])
    questions = [item.action for item in checks if item.severity in ('warn', 'block')]
    if questions:
        lines.append('OPEN QUESTIONS')
        lines.extend('- ' + question for question in questions)
    lines.extend(['', 'Please quote goods, shipping, lead time, and terms separately.', 'No order is placed by this document.'])
    return '\n'.join(lines)
