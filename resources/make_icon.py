# Рисует иконку SHKWIZI Remote: AppIcon-1024.png (iOS), icon.png, icon.ico (Windows)
import os
from PIL import Image, ImageDraw, ImageFilter
D = os.path.dirname(os.path.abspath(__file__))
S = 1024
bg = Image.new('RGB', (S, S), '#0b0614'); bd = ImageDraw.Draw(bg)
bd.ellipse((-250, -250, 650, 650), fill='#7b2cff')
bd.ellipse((450, 400, 1250, 1200), fill='#ff3f9a')
img = bg.filter(ImageFilter.GaussianBlur(170))
d = ImageDraw.Draw(img, 'RGBA')
d.rounded_rectangle((150, 150, 874, 874), radius=160, fill=(255, 255, 255, 36), outline=(255, 255, 255, 90), width=6)
# монитор
d.rounded_rectangle((250, 300, 700, 600), radius=30, outline='white', width=28)
d.rectangle((440, 600, 510, 665), fill='white'); d.rounded_rectangle((370, 655, 580, 690), radius=14, fill='white')
# телефон
d.rounded_rectangle((610, 470, 780, 780), radius=34, fill=(25, 12, 45, 255), outline='white', width=24)
# курсор-стрелка на мониторе
d.polygon([(400, 360), (400, 520), (440, 482), (470, 545), (497, 532), (468, 470), (520, 470)], fill='white')
img.save(os.path.join(D, 'AppIcon-1024.png'))
img.resize((512, 512)).save(os.path.join(D, 'icon.png'))
img.resize((256, 256)).save(os.path.join(D, 'icon.ico'), sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print('icons ok')
