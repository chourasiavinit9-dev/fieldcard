"""qr_code.py: Pure Python, zero-dependency QR code generator.

Generates QR Code (Model 2, Version 1-3, Byte mode, ECC Level M) as an inline SVG.
100% Python standard library. Works offline, no external services.
"""

from typing import List, Tuple

# Galois Field GF(256) with primitive polynomial 0x11D (285)
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    EXP[_i + 255] = _x
    LOG[_x] = _i
    _x = (_x << 1) ^ (0x11D if (_x & 0x80) else 0)


def _gf_mul(x: int, y: int) -> int:
    return 0 if x == 0 or y == 0 else EXP[LOG[x] + LOG[y]]


def _rs_poly(degree: int) -> List[int]:
    poly = [1]
    for i in range(degree):
        # multiply poly by (x - 2^i)
        root = EXP[i]
        new_poly = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            new_poly[j] ^= _gf_mul(c, root)
            new_poly[j + 1] ^= c
        poly = new_poly
    return poly


def _rs_encode(data: bytes, ecc_len: int) -> bytes:
    gen = _rs_poly(ecc_len)
    msg = list(data) + [0] * ecc_len
    for i in range(len(data)):
        lead = msg[i]
        if lead != 0:
            for j in range(len(gen)):
                msg[i + j] ^= _gf_mul(gen[j], lead)
    return bytes(msg[len(data):])


# Version 1 (21x21, 16 data bytes, 10 ecc bytes, ECC M)
# Version 2 (25x25, 28 data bytes, 16 ecc bytes, ECC M)
# Version 3 (29x29, 44 data bytes, 26 ecc bytes, ECC M)
CONFIGS = [
    (1, 21, 14, 10),
    (2, 25, 26, 16),
    (3, 29, 42, 26),
]


def make_qr_matrix(text: str) -> Tuple[List[List[int]], int]:
    raw = text.encode("utf-8")
    config = None
    for ver, size, max_data, ecc_len in CONFIGS:
        if len(raw) <= max_data:
            config = (ver, size, max_data, ecc_len)
            break
    if not config:
        # Fall back to version 3 by truncating if text is overly long
        config = CONFIGS[-1]
        raw = raw[:config[2]]

    ver, size, max_data, ecc_len = config

    # Bitstream: Mode (0100 for Byte mode) + Count + Data + Terminator
    bits = "0100"
    bits += format(len(raw), f"0{8}b")
    for b in raw:
        bits += format(b, "08b")
    bits += "0000"
    while len(bits) % 8 != 0:
        bits += "0"

    # Convert to bytes and pad with 0xEC, 0x11
    data_bytes = bytearray([int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)])
    pads = [0xEC, 0x11]
    pad_idx = 0
    while len(data_bytes) < max_data:
        data_bytes.append(pads[pad_idx])
        pad_idx = (pad_idx + 1) % 2

    # Compute Reed-Solomon ECC
    ecc_bytes = _rs_encode(bytes(data_bytes), ecc_len)
    all_bytes = bytes(data_bytes) + ecc_bytes

    # Create empty matrix (-1 means unassigned)
    matrix = [[-1 for _ in range(size)] for _ in range(size)]
    reserved = [[False for _ in range(size)] for _ in range(size)]

    def set_finder(r0, c0):
        for r in range(7):
            for c in range(7):
                val = 1 if (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4)) else 0
                matrix[r0 + r][c0 + c] = val
                reserved[r0 + r][c0 + c] = True
        # Separator around finder
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < size and 0 <= cc < size and not reserved[rr][cc]:
                    matrix[rr][cc] = 0
                    reserved[rr][cc] = True

    # 3 Finders
    set_finder(0, 0)
    set_finder(0, size - 7)
    set_finder(size - 7, 0)

    # Alignment pattern for Version 2+ (center at 18 for V2, 22 for V3)
    if ver >= 2:
        center = 18 if ver == 2 else 22
        for r in range(center - 2, center + 3):
            for c in range(center - 2, center + 3):
                val = 1 if (r in (center - 2, center + 2) or c in (center - 2, center + 2) or (r == center and c == center)) else 0
                matrix[r][c] = val
                reserved[r][c] = True

    # Timing patterns
    for i in range(8, size - 8):
        val = 1 if i % 2 == 0 else 0
        if not reserved[6][i]:
            matrix[6][i] = val
            reserved[6][i] = True
        if not reserved[i][6]:
            matrix[i][6] = val
            reserved[i][6] = True

    # Dark module
    matrix[size - 8][8] = 1
    reserved[size - 8][8] = True

    # Reserve Format Info areas (row 8 and col 8)
    for c in range(9):
        if not reserved[8][c]:
            reserved[8][c] = True
    for r in range(9):
        if not reserved[r][8]:
            reserved[r][8] = True
    for c in range(size - 8, size):
        if not reserved[8][c]:
            reserved[8][c] = True
    for r in range(size - 7, size):
        if not reserved[r][8]:
            reserved[r][8] = True

    # Place data bits (zigzag right-to-left, bottom-to-top)
    all_bits = "".join(format(b, "08b") for b in all_bytes)
    bit_idx = 0
    right = size - 1
    up = True
    while right > 0:
        if right == 6:
            right -= 1  # Skip timing column
        for row in (range(size - 1, -1, -1) if up else range(size)):
            for col in (right, right - 1):
                if not reserved[row][col]:
                    bit = int(all_bits[bit_idx]) if bit_idx < len(all_bits) else 0
                    bit_idx += 1
                    # Apply Mask 0: (row + col) % 2 == 0
                    matrix[row][col] = bit ^ (1 if (row + col) % 2 == 0 else 0)
        right -= 2
        up = not up

    # Format info for ECC Level M (00), Mask 0 (000) -> 00000 with BCH -> 101010000010010
    format_bits = [1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0]
    # Top-left format placement
    coords_tl = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                 (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    for i, (r, c) in enumerate(coords_tl):
        matrix[r][c] = format_bits[i]

    # Split format placement around other finders
    coords_split = ([(size - 1 - i, 8) for i in range(7)] +
                    [(8, size - 8 + i) for i in range(8)])
    for i, (r, c) in enumerate(coords_split):
        matrix[r][c] = format_bits[i]

    return matrix, size


def qr_svg(text: str, size_px: int = 140, fg: str = "#1d2b22", bg: str = "#f8f4e8") -> str:
    """Generate an inline SVG QR code."""
    matrix, count = make_qr_matrix(text)
    margin = 2
    total_grid = count + margin * 2
    rects = []
    for r in range(count):
        for c in range(count):
            if matrix[r][c] == 1:
                rects.append(f'<rect x="{c + margin}" y="{r + margin}" width="1" height="1" fill="{fg}"/>')
    content = "".join(rects)
    return (
        f'<svg viewBox="0 0 {total_grid} {total_grid}" width="{size_px}" height="{size_px}" '
        f'xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges">'
        f'<rect width="{total_grid}" height="{total_grid}" fill="{bg}"/>'
        f'{content}</svg>'
    )
