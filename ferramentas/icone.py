"""Icone do app do ponto: cronometro com o arco da semana. PNG na unha
(zlib + struct) porque nem o Mac nem o servidor tem PIL."""
import math, struct, zlib

BG = (7, 11, 18); TRILHO = (27, 36, 51); BRANCO = (233, 237, 244)
A1 = (59, 130, 246); A2 = (34, 211, 238)
CX, CY, R, W = 0.5, 0.54, 0.285, 0.085
SPAN = math.radians(250)

def mistura(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))

def cor(u, v):
    dx, dy = u - CX, v - CY
    r = math.hypot(dx, dy)
    c = BG
    # coroa do cronometro
    if abs(u - CX) < 0.055 and CY - R - W / 2 - 0.075 < v < CY - R - W / 2 + 0.01:
        c = TRILHO
    if abs(u - CX) < 0.085 and CY - R - W / 2 - 0.1 < v < CY - R - W / 2 - 0.06:
        c = BRANCO
    if abs(r - R) < W / 2:
        c = TRILHO
        ang = math.atan2(dx, -dy) % (2 * math.pi)
        if ang <= SPAN:
            c = mistura(A1, A2, ang / SPAN)
    # pontas redondas do arco
    for ang, cc in ((0.0, A1), (SPAN, A2)):
        px, py = CX + R * math.sin(ang), CY - R * math.cos(ang)
        if math.hypot(u - px, v - py) < W / 2:
            c = cc
    # ponteiro ate o fim do arco + miolo
    hx, hy = math.sin(SPAN), -math.cos(SPAN)
    t = dx * hx + dy * hy
    if 0 <= t <= R * 0.62 and abs(dx * hy - dy * hx) < 0.022:
        c = BRANCO
    if r < 0.042:
        c = BRANCO
    return c

def png(tam, ss, nome):
    linhas = bytearray()
    for y in range(tam):
        linhas.append(0)
        for x in range(tam):
            acc = [0.0, 0.0, 0.0]
            for j in range(ss):
                for i in range(ss):
                    c = cor((x + (i + .5) / ss) / tam, (y + (j + .5) / ss) / tam)
                    acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]
            n = ss * ss
            linhas += bytes(int(round(a / n)) for a in acc)
    def bloco(tipo, dados):
        return (struct.pack(">I", len(dados)) + tipo + dados +
                struct.pack(">I", zlib.crc32(tipo + dados) & 0xffffffff))
    with open(nome, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(bloco(b"IHDR", struct.pack(">IIBBBBB", tam, tam, 8, 2, 0, 0, 0)))
        f.write(bloco(b"IDAT", zlib.compress(bytes(linhas), 9)))
        f.write(bloco(b"IEND", b""))

for tam, ss in ((180, 4), (192, 4), (512, 3)):
    png(tam, ss, "web/icone-%d.png" % tam)
    print("ok", tam)
