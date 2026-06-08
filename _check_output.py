"""Check existing output images"""
from PIL import Image
import os

userDir = os.path.expanduser('~/Documents/MKPSpectrum/Temp/Texture/Preview')
files = sorted([f for f in os.listdir(userDir) if f.endswith('.png')])
print(f"Total files: {len(files)}")

# Check a few files
for f in [files[0], files[1], files[50], files[99]]:
    path = os.path.join(userDir, f)
    img = Image.open(path)
    print(f"\n{f}:")
    print(f"  size={img.size}, mode={img.mode}")
    extrema = img.getextrema()
    print(f"  extrema={extrema}")
    # Get pixel at center
    cx, cy = img.size[0]//2, img.size[1]//2
    print(f"  center pixel={img.getpixel((cx, cy))}")
    # Get unique colors
    if img.mode == 'RGB':
        colors = img.getcolors(maxcolors=50)
        print(f"  num_colors_in_sample={len(colors) if colors else '>50'}")
        if colors:
            for c in colors[:5]:
                print(f"    count={c[0]}, color={c[1]}")
