from PIL import Image, ImageDraw, ImageFont
import qrcode, os

PROMO = r"D:/Backup/Desktop/mdl/mcd-meal-brain/docs/promo"
GH = "https://github.com/lqx-shuaige/mcd-meal-brain"
DEMO = "https://lqx-shuaige.github.io/mcd-meal-brain/demo.html"

def load_font(size):
    for p in [r"C:/Windows/Fonts/consola.ttf",
              r"C:/Windows/Fonts/arial.ttf",
              r"C:/Windows/Fonts/msyh.ttc",
              r"C:/Windows/Fonts/simhei.ttf"]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()

def make_qr(url, box=10, border=3):
    qr = qrcode.QRCode(box_size=box, border=border,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url); qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")

qr = make_qr(GH, box=10, border=3)

targets = ["promo-banner-horizontal.png",
           "promo-poster-vertical.png",
           "promo-card-square.png"]

for fn in targets:
    im = Image.open(os.path.join(PROMO, fn)).convert("RGB")
    W, H = im.size
    draw = ImageDraw.Draw(im, "RGBA")

    bar_h = int(H * 0.17)
    pad = int(W * 0.035)
    draw.rectangle([0, H - bar_h, W, H], fill=(0, 0, 0, 155))

    qsize = int(bar_h * 0.82)
    qx = W - qsize - pad
    qy = H - qsize - (bar_h - qsize) // 2
    draw.rectangle([qx - 7, qy - 7, qx + qsize + 7, qy + qsize + 7],
                   fill=(255, 255, 255, 255))
    im.paste(qr.resize((qsize, qsize)), (qx, qy))

    line1 = "GitHub: github.com/lqx-shuaige/mcd-meal-brain"
    line2 = "Demo:  lqx-shuaige.github.io/mcd-meal-brain/demo.html"
    avail = qx - pad - 12

    start = max(20, int(W / 30))
    fs = start
    font = load_font(fs); font2 = load_font(int(fs * 0.82))
    for s in range(start, 13, -2):
        f1 = load_font(s); f2 = load_font(int(s * 0.82))
        w1 = draw.textlength(line1, font=f1)
        w2 = draw.textlength(line2, font=f2)
        if max(w1, w2) <= avail:
            fs, font, font2 = s, f1, f2
            break
    else:
        font = load_font(14); font2 = load_font(12); fs = 14

    y1 = H - bar_h + int(bar_h * 0.20)
    y2 = y1 + int(fs * 1.45)
    draw.text((pad, y1), line1, fill=(255, 255, 255, 255), font=font)
    draw.text((pad, y2), line2, fill=(255, 255, 255, 255), font=font2)

    out = os.path.join(PROMO, fn.replace(".png", "_with_link.png"))
    im.save(out)
    print("saved", out, im.size, "fs=", fs)
