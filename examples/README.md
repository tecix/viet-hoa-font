# Ví dụ sử dụng

## Trường hợp 1: Font đã có đủ dấu phụ

Một số font (ví dụ Roboto, Noto Sans) đã ship đầy đủ ký tự tiếng Việt. Khi đó, công cụ chỉ báo coverage và không cần làm gì:

```bash
python viet_hoa.py Roboto-Regular.ttf --check
# Vietnamese coverage: 134/134
```

## Trường hợp 2: Font có chữ cái cơ bản và dấu phụ, thiếu glyph tổ hợp

Phổ biến với các font hỗ trợ Latin Extended-A/B nhưng không có Latin Extended Additional. Việt hoá trực tiếp:

```bash
python viet_hoa.py YourFont-Regular.ttf -o YourFont-VN.ttf -v
```

## Trường hợp 3: Font hiển thị (display font) thiếu cả dấu phụ

Hầu hết font hiển thị trên Google Fonts (Outfit, Bricolage, Big Shoulders...) thiếu các dấu hỏi (◌̉), dấu nặng (◌̣), dấu sừng (◌̛). Cần thêm font donor:

```bash
python viet_hoa.py Outfit-Regular.ttf \
    -o Outfit-VN.ttf \
    --donor NotoSans-Regular.ttf \
    -v
```

Khi chọn donor, hãy ưu tiên font có:
- Cùng phong cách (sans-serif → sans-serif, serif → serif)
- Cùng độ đậm (regular → regular, bold → bold)
- `unitsPerEm` tương thích (thường 1000 hoặc 2048)

Một số font donor tốt:
- **Noto Sans / Noto Serif** — phủ rất rộng, miễn phí, có anchor data đầy đủ
- **Arsenal** — sans-serif Việt Nam, xử lý tốt cả chữ hoa
- **Be Vietnam Pro** — thiết kế cho tiếng Việt từ đầu

## Pipeline xử lý hàng loạt

Việt hoá tất cả font trong thư mục:

```bash
for f in fonts/*.ttf; do
    python viet_hoa.py "$f" \
        -o "output/$(basename "$f" .ttf)-VN.ttf" \
        --donor donor/NotoSans-Regular.ttf
done
```

## Kiểm thử kết quả

Sau khi Việt hoá, bạn có thể render thử bằng Pillow:

```python
from PIL import Image, ImageDraw, ImageFont

font = ImageFont.truetype("YourFont-VN.ttf", 60)
img = Image.new("RGB", (1000, 200), "white")
draw = ImageDraw.Draw(img)
draw.text((20, 60), "Tiếng Việt: Phở bò Hà Nội — ưỡn ngực ỷ thế", 
          font=font, fill="black")
img.save("test.png")
```

Hoặc cài font vào hệ thống và mở Word, Figma để kiểm tra trực quan.
