from xml.sax.saxutils import escape, quoteattr


def blank_size(L, W, H):
    return 2 * L + 2 * W + 1250 + 4 * 62, H + W + 2 * 62


def _attr(value):
    return quoteattr(str(value))


def _text(value):
    return escape(str(value))


def render(box):
    length, width = blank_size(box.L_mil, box.W_mil, box.H_mil)
    panels = [box.L_mil, box.W_mil, box.L_mil, box.W_mil, 1250]
    labels = ['Side', 'End', 'Side', 'End', 'Glue']
    x = 0
    scores = []
    texts = []
    for panel, label in zip(panels, labels):
        texts.append(f'<text x={_attr(x + panel // 2)} y={_attr(width // 2)} text-anchor="middle">{_text(label)}</text>')
        x += panel
        if x < length:
            scores.append(f'<line class="score" x1={_attr(x)} y1="0" x2={_attr(x)} y2={_attr(width)}/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox={_attr(f"-150 -150 {length + 300} {width + 700}")}>'
        '<style>.cut{fill:none;stroke:#111;stroke-width:20}.score{stroke:#268;stroke-width:14;stroke-dasharray:80 50}text{font:180px sans-serif}</style>'
        f'<rect class="cut" width={_attr(length)} height={_attr(width)}/>'
        f'<rect x="-125" y="-125" width={_attr(length + 250)} height={_attr(width + 250)} fill="none" stroke="#d55" stroke-dasharray="50 40"/>'
        + ''.join(scores + texts)
        + f'<text x="0" y={_attr(width + 300)}>Blank: {_text(length)} × {_text(width)} mil</text>'
        + f'<text x="0" y={_attr(width + 550)}>{_text("Illustrative template — the manufacturer's production dieline governs.")}</text></svg>'
    )
