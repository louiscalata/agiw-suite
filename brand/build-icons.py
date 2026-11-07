#!/usr/bin/env python3
"""Builds every AGIW icon from one master: the letters in Inter SemiBold, black and white only.

    python3 brand/build-icons.py [--font Inter-SemiBold.ttf]

--font re-reads the outlines into brand/glyphs.json (needs swift); without it the stored outlines
are used, so the font does not have to be installed. Needs rsvg-convert and iconutil.
Every output is regenerated; nothing else is touched except the marked block in agiw-monitor.ps1.
"""
import argparse
import base64
import json
import pathlib
import shutil
import struct
import subprocess
import tempfile
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAND = ROOT / 'brand'
GLYPHS = BRAND / 'glyphs.json'
TRACKING_EM = 0.02  # docs/jaryo-identity.md: the font's own kerning plus 0.02em
INK, PAPER = '#000000', '#ffffff'


def outlines(font):
    tool = BRAND / 'tools' / 'outline-text.swift'
    data = {'font': 'Inter SemiBold 4.1 (SIL Open Font License 1.1)', 'size': 1000, 'trackingEm': TRACKING_EM}
    for text in ('AG', 'AGIW'):
        out = subprocess.run(['swift', str(tool), str(font), text, '1000', str(TRACKING_EM)],
                             check=True, capture_output=True, text=True).stdout.splitlines()
        width, height = (float(v) for v in out[0].split())
        data[text] = {'width': width, 'height': height, 'd': out[1]}
    GLYPHS.write_text(json.dumps(data, indent=1) + '\n')


def letters(glyph, cx, cy, width, fill):
    """The glyph scaled to WIDTH and centred on (cx, cy)."""
    s = width / glyph['width']
    x, y = cx - width / 2, cy - glyph['height'] * s / 2
    return f'<path transform="translate({x:.3f} {y:.3f}) scale({s:.6f})" fill="{fill}" d="{glyph["d"]}"/>'


def svg(size, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size[0]} {size[1]}" role="img" '
            f'aria-label="{title}">\n<title>{title}</title>\n{body}\n</svg>\n')


def square(g, size, ratio, tile=INK, letter=PAPER, frame=None):
    body = f'<rect width="{size}" height="{size}" fill="{tile}"/>'
    if frame == 'solid':
        w = size / 16
        body += f'<rect x="{w / 2}" y="{w / 2}" width="{size - w}" height="{size - w}" fill="none" stroke="{letter}" stroke-width="{w:.3f}"/>'
    elif frame == 'dashed':
        w = size / 16
        body += (f'<rect x="{w / 2}" y="{w / 2}" width="{size - w}" height="{size - w}" fill="none" stroke="{letter}" '
                 f'stroke-width="{w:.3f}" stroke-dasharray="{2 * w:.3f} {2 * w:.3f}"/>')
    elif frame == 'edge':
        w = size / 16
        body += f'<rect x="{w / 2}" y="{w / 2}" width="{size - w}" height="{size - w}" fill="none" stroke="{letter}" stroke-width="{w:.3f}"/>'
    return svg((size, size), body + letters(g, size / 2, size / 2, size * ratio, letter), 'AGIW')


def render(source, out, w, h=None):
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['rsvg-convert', '-w', str(w), '-h', str(h or w), '-o', str(out), str(source)], check=True)


def rgba(png):
    """Width, height and RGBA rows of an 8-bit RGB or RGBA, non-interlaced PNG (what rsvg-convert writes)."""
    data = png.read_bytes()
    pos, idat, width = 8, b'', 0
    while pos < len(data):
        length, kind = struct.unpack('>I4s', data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b'IHDR':
            width, height, depth, colour, _, _, interlace = struct.unpack('>IIBBBBB', chunk)
            if depth != 8 or colour not in (2, 6) or interlace:
                raise SystemExit(f'{png}: expected 8-bit RGB or RGBA, not interlaced')
            channels = 4 if colour == 6 else 3
        elif kind == b'IDAT':
            idat += chunk
        pos += 12 + length
    raw, stride, rows = zlib.decompress(idat), width * channels, []
    prior = bytearray(stride)
    for y in range(height):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up, corner = prior[i], prior[i - channels] if i >= channels else 0
            if kind == 1: line[i] = (line[i] + left) & 255
            elif kind == 2: line[i] = (line[i] + up) & 255
            elif kind == 3: line[i] = (line[i] + (left + up) // 2) & 255
            elif kind == 4:
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 255
        rows.append(bytes(line) if channels == 4 else b''.join(bytes(line[x:x + 3]) + b'\xff' for x in range(0, stride, 3)))
        prior = line
    return width, height, rows


def ico(pngs, out):
    """A classic ICO: 32-bit BGRA DIB entries. .NET Framework's System.Drawing.Icon (the tray) cannot
    read PNG-compressed entries at 16 and 32 px."""
    blobs = []
    for png in pngs:
        w, h, rows = rgba(png)
        pixels = b''.join(bytes(b for x in range(w) for b in (r[x * 4 + 2], r[x * 4 + 1], r[x * 4], r[x * 4 + 3])) for r in reversed(rows))
        mask = b'\0' * (((w + 31) // 32) * 4 * h)
        header = struct.pack('<IiiHHIIiiII', 40, w, h * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
        blobs.append((w, h, header + pixels + mask))
    offset = 6 + 16 * len(blobs)
    entries = b''
    for w, h, blob in blobs:
        entries += struct.pack('<BBBBHHII', w % 256, h % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(struct.pack('<HHH', 0, 1, len(blobs)) + entries + b''.join(blob for _, _, blob in blobs))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--font', type=pathlib.Path)
    args = parser.parse_args()
    if args.font:
        outlines(args.font)
    glyphs = json.loads(GLYPHS.read_text())
    ag, agiw = glyphs['AG'], glyphs['AGIW']
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        # Masters.
        (BRAND / 'agiw-mark.svg').write_text(square(ag, 1024, 0.6))
        for name, fg, bg in (('agiw-wordmark-on-light', INK, PAPER), ('agiw-wordmark-on-dark', PAPER, INK)):
            w, h = 1200, 400
            (BRAND / f'{name}.svg').write_text(svg((w, h), f'<rect width="{w}" height="{h}" fill="{bg}"/>' + letters(agiw, w / 2, h / 2, w * 0.8, fg), 'AGIW'))
        macos = svg((1024, 1024), f'<rect x="100" y="100" width="824" height="824" rx="185" fill="{INK}"/>' + letters(ag, 512, 512, 470, PAPER), 'AGIW')
        (BRAND / 'agiw-app-icon-macos.svg').write_text(macos)
        # Favicon (served; tests expect both colour literals).
        (ROOT / 'jaryo' / 'marks' / 'favicon.svg').write_text(square(ag, 32, 0.72))

        # macOS: the full monitor's .icns and the App Store wrapper's appiconset.
        master = BRAND / 'agiw-app-icon-macos.svg'
        iconset = tmp / 'AppIcon.iconset'
        appiconset = ROOT / 'wrapper-patch' / 'app-icon' / 'AppIcon.appiconset'
        if appiconset.exists():
            shutil.rmtree(appiconset)
        images = []
        for base in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f'icon_{base}x{base}{"@2x" if scale == 2 else ""}.png'
                render(master, iconset / name, base * scale)
                render(master, appiconset / name, base * scale)
                images.append({'size': f'{base}x{base}', 'idiom': 'mac', 'filename': name, 'scale': f'{scale}x'})
        (appiconset / 'Contents.json').write_text(json.dumps({'images': images, 'info': {'version': 1, 'author': 'xcode'}}, indent=2) + '\n')
        (BRAND / 'macos').mkdir(exist_ok=True)
        subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(BRAND / 'macos' / 'AppIcon.icns')], check=True)
        # Menu bar: a template image, letters only (macOS tints it).
        bar = svg((22, 16), letters(ag, 11, 8, 18, INK), 'AGIW')
        (tmp / 'bar.svg').write_text(bar)
        render(tmp / 'bar.svg', BRAND / 'macos' / 'MenuBarMark.png', 22, 16)
        render(tmp / 'bar.svg', BRAND / 'macos' / 'MenuBarMark@2x.png', 44, 32)

        # Android: legacy square launcher and adaptive foreground (108dp, letters inside the 66dp safe zone).
        res = ROOT / 'android' / 'app' / 'src' / 'main' / 'res'
        (tmp / 'launcher.svg').write_text(square(ag, 1024, 0.6))
        (tmp / 'foreground.svg').write_text(svg((1024, 1024), letters(ag, 512, 512, 1024 * 0.42, PAPER), 'AGIW'))
        for bucket, scale in (('mdpi', 1), ('hdpi', 1.5), ('xhdpi', 2), ('xxhdpi', 3), ('xxxhdpi', 4)):
            render(tmp / 'launcher.svg', res / f'mipmap-{bucket}' / 'ic_launcher.png', round(48 * scale))
            render(tmp / 'foreground.svg', res / f'mipmap-{bucket}' / 'ic_launcher_foreground.png', round(108 * scale))
        adaptive = ('<?xml version="1.0" encoding="utf-8"?>\n'
                    '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
                    '    <background android:drawable="@android:color/black"/>\n'
                    '    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>\n'
                    '    <monochrome android:drawable="@mipmap/ic_launcher_foreground"/>\n'
                    '</adaptive-icon>\n')
        (res / 'mipmap-anydpi-v26').mkdir(parents=True, exist_ok=True)
        (res / 'mipmap-anydpi-v26' / 'ic_launcher.xml').write_text(adaptive)

        # Windows tray: one icon per state. State is a shape, never a colour; the tooltip says it in words.
        states = {
            'ok': dict(),                                        # plain: lanes up and idle
            'live': dict(tile=PAPER, letter=INK, frame='edge'),  # inverted: a lane is working
            'warn': dict(frame='solid'),                         # framed: something needs attention
            'muted': dict(frame='dashed'),                       # dashed: starting or stale
        }
        block = ['# BEGIN generated tray icons (brand/build-icons.py; do not edit by hand)', '$TrayIconData = @{']
        for state, style in states.items():
            pngs = []
            for px in (16, 32):
                src = tmp / f'tray-{state}-{px}.svg'
                src.write_text(square(ag, px, 0.66, **style))
                render(src, tmp / f'tray-{state}-{px}.png', px)
                pngs.append(tmp / f'tray-{state}-{px}.png')
            ico(pngs, BRAND / 'windows' / f'tray-{state}.ico')
            block.append(f"    {state} = '{base64.b64encode((BRAND / 'windows' / f'tray-{state}.ico').read_bytes()).decode()}'")
        block += ['}', '# END generated tray icons']
        ps1 = ROOT / 'windows' / 'agiw-monitor.ps1'
        with ps1.open(newline='') as handle:
            text = handle.read()
        start, end = text.find('# BEGIN generated tray icons'), text.find('# END generated tray icons')
        if start < 0 or end < 0:
            raise SystemExit('agiw-monitor.ps1 has no generated tray icon block')
        end += len('# END generated tray icons')
        with ps1.open('w', newline='') as handle:  # the script is CRLF
            handle.write(text[:start] + '\r\n'.join(block) + text[end:])
        # The tray is the Windows app icon too.
        ico([tmp / 'tray-ok-16.png', tmp / 'tray-ok-32.png'], BRAND / 'windows' / 'agiw.ico')
    finally:
        shutil.rmtree(tmp)


if __name__ == '__main__':
    main()
