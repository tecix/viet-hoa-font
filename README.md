# viet-hoa-font

<p>
  <a href="#english">🇬🇧 English</a> &nbsp;|&nbsp;
  <a href="#tiếng-việt">🇻🇳 Tiếng Việt</a>
</p>

---

## English

A tool for Vietnamizing OTF/TTF fonts — automatically adds missing Vietnamese characters to an existing Latin font by composing glyphs and diacritics already present in the font (or borrowed from a donor font) into composite characters such as `ấ ằ ợ ử Ỡ Ự Đ`.

![Before and after Vietnamization](docs/before-after.png)

### Problem

Many beautiful Latin fonts on Google Fonts, Adobe Fonts, or commercial font collections only partially support Vietnamese. Typically a font ships with the base Latin letters and a few diacritics (acute, grave, tilde), but is missing the combinations unique to Vietnamese, such as:

- Hook-above characters (ả ẻ ỉ ỏ ủ ỷ) — missing the `◌̉` mark
- Dot-below characters (ạ ẹ ị ọ ụ ỵ) — missing the `◌̣` mark
- Horn family (ơ ớ ờ ở ỡ ợ ư ứ ừ ử ữ ự) — missing the `◌̛` mark
- Đ / đ — a unique letter that cannot be built from a combining mark
- Stacked double-diacritic combinations such as `ấ ầ ẩ ẫ ậ ắ ằ ẳ ẵ ặ ế ề ể ễ ệ ố ồ ổ ỗ ộ`

This tool automatically detects missing characters, builds the corresponding composite glyphs, positions them correctly using the font's existing GPOS anchor data, and saves a new font file.

### Features

- **134 full Vietnamese characters** (upper and lower case) in the Latin Extended Additional range (U+1EA0–U+1EF9) plus Ơ, Ư, Đ, Ă, Â, Ê, Ô.
- **TTF and OTF support** — automatically selects the TrueType composite mechanism (glyf composite) or CFF (flatten outlines) as appropriate.
- **Anchor data usage** — when the font already contains GPOS MarkBase data, the tool uses the designer's original attachment points, producing results nearly identical to hand-drawn glyphs.
- **Smart heuristics** when no anchors are present: automatically estimates diacritic placement based on cap-height, x-height, and the bounding box of the base character.
- **Correct Vietnamese stacking** — tone marks above circumflex (e.g. ấ, ề, ổ) are placed following authentic Vietnamese typographic conventions, not pushed to extreme heights.
- **Donor font fallback** — if the source font lacks the required diacritics (◌̉ ◌̛ ◌̣) or Đ, the tool borrows only those small marks from a donor font (e.g. Noto Sans, Arsenal) while preserving the style of the original letters.
- **OS/2 update** — sets the Unicode Range bit for Latin Extended Additional and the Vietnamese codepage (1258) so the OS recognizes the font as Vietnamese-capable.

### Requirements

- Python 3.10 or higher
- [fontTools](https://github.com/fonttools/fonttools) (`pip install fonttools`)

### Installation

```bash
git clone https://github.com/<your-username>/viet-hoa-font.git
cd viet-hoa-font
pip install -r requirements.txt
```

### Usage

#### Check Vietnamese coverage

```bash
python viet_hoa.py font.ttf --check
```

Output lists all missing Vietnamese codepoints, for example:

```
Vietnamese coverage: 32/134
Missing:
  U+0102  Ă  LATIN CAPITAL LETTER A WITH BREVE
  U+0103  ă  LATIN SMALL LETTER A WITH BREVE
  ...
```

#### Vietnamize a font

```bash
python viet_hoa.py font.ttf -o font-VN.ttf
```

Uses glyphs and diacritics already present in the source font to compose the missing Vietnamese characters.

#### Vietnamize with a donor font

The most common scenario — most display fonts do not ship with a full set of Vietnamese diacritics:

```bash
python viet_hoa.py font.ttf -o font-VN.ttf --donor NotoSans-Regular.ttf
```

Only the small diacritics (`◌̉`, `◌̣`, `◌̛`) and Đ/đ are borrowed from the donor; all other composites are built from the original font's own glyphs.

Choose a donor with a matching style (sans-serif → sans-serif, serif → serif, bold → bold) so the diacritics blend naturally.

#### Full argument reference

```
python viet_hoa.py INPUT [-o OUTPUT] [--donor DONOR] [--check] [-v]

Positional:
  INPUT              Input font file (.otf or .ttf)

Options:
  -o, --output       Output font file path
                     (default: appends "-VN" to the source filename)
  --donor            Donor font to borrow glyphs from when needed
  --check            Check coverage only, do not write output
  -v, --verbose      Print detailed processing information
```

### Examples

Full 134-character Vietnamese coverage after Vietnamization:

![Full coverage](docs/full-coverage.png)

### How it works

#### 1. NFD decomposition

Each Vietnamese codepoint is decomposed via Unicode NFD into a base letter and one or more combining marks. For example:

```
ấ (U+1EA5) → a (U+0061) + ◌̂ (U+0302) + ◌́ (U+0301)
ợ (U+1EE3) → o (U+006F) + ◌̛ (U+031B) + ◌̣ (U+0323)
```

#### 2. Longest available prefix

For each character to build, the tool finds the longest prefix already present in the font. If the font has `â` (acircumflex) — with anchor data indicating where to attach a tone mark — it uses `â + ◌́` instead of `a + ◌̂ + ◌́`. This exploits the font designer's original per-glyph decisions.

#### 3. Diacritic placement

Priority order:

1. **GPOS MarkBase anchor** — uses the font's existing attachment points directly.
2. **Type-based heuristics**:
   - **Above marks** (acute, grave, circumflex, breve, hook, tilde): horizontally centered, placed above the glyph top by a `mark_gap` measured from existing composites in the font (e.g. from `á`).
   - **Dot below**: centered beneath the body — for horn letters like ợ ự, uses the bounding box of the bare base (`o`, `u`) rather than ơ/ư.
   - **Horn** (◌̛): attached to the upper-right corner, protruding above the glyph top by ~25% of the horn height.
3. **Effective top tracking** — when placing a second mark (e.g. ớ = ơ + ◌́), references the actual top after the first mark has been placed.

#### 4. Composite glyph creation

- **TrueType (TTF)**: composite glyph with component references, no outline duplication — smaller file size.
- **CFF/PostScript (OTF)**: CFF does not support composites, so outlines are re-drawn with a transform offset (flattened).

#### 5. cmap and metadata update

New codepoints are mapped in all Unicode cmap subtables; the appropriate OS/2 bits are set.

### Limitations

- **Aesthetic depends on donor font**: if the source and donor fonts differ significantly in style (e.g. a heavy display font vs. a thin sans-serif), borrowed diacritics may look visually inconsistent. Choose the donor carefully.
- **No kerning or ligature handling**: only adds glyphs; does not modify GPOS/GSUB tables.
- **No variable font axis support**: for variable fonts, the output will be the default instance.
- **No anchors installed on new glyphs** — meaning if you use the output as input for a second Vietnamization pass, the newly added glyphs will have no anchor data. Since full Vietnamese coverage is achieved in one pass, this is rarely relevant.

### Project structure

```
viet-hoa-font/
├── viet_hoa.py           # Main source (single file)
├── README.md             # This file
├── LICENSE               # MIT License
├── requirements.txt      # Python dependencies
├── .gitignore
├── docs/
│   ├── before-after.png  # Before/after illustration
│   └── full-coverage.png # 134-character Vietnamese chart
└── examples/
    └── README.md         # Example walkthrough
```

### Contributing

Pull requests and issues are welcome. Particularly appreciated:

- Test cases for fonts with unusual structures (variable, color, multi-script)
- Heuristic improvements for cases where donor and source fonts differ in style
- Support for other languages that share the same approach (e.g. Pinyin tones)

### License

MIT License — see [LICENSE](LICENSE).

### Acknowledgements

- The [fontTools](https://github.com/fonttools/fonttools) project — the most versatile Python font processing library available.
- The Vietnamese font design community for sharing expertise on Vietnamese typography through articles and open-source work.

---

## Tiếng Việt

<details>
<summary>Bấm để xem phiên bản tiếng Việt</summary>

<br>

Công cụ Việt hoá font OTF/TTF — tự động bổ sung các ký tự tiếng Việt còn thiếu vào một font chữ Latin có sẵn, bằng cách kết hợp các glyph và dấu phụ mà font đã có (hoặc mượn từ một font donor) thành các ký tự tổ hợp như `ấ ằ ợ ử Ỡ Ự Đ`.

![Trước và sau khi Việt hoá](docs/before-after.png)

### Vấn đề

Rất nhiều font Latin đẹp trên Google Fonts, Adobe Fonts hay các bộ font thương mại chỉ hỗ trợ một phần tiếng Việt. Thường thì font có sẵn các chữ cái cơ bản và một vài dấu phụ (sắc, huyền, ngã), nhưng thiếu các tổ hợp đặc trưng tiếng Việt như:

- Các ký tự mang dấu hỏi (ả ẻ ỉ ỏ ủ ỷ) — thiếu dấu `◌̉`
- Các ký tự mang dấu nặng (ạ ẹ ị ọ ụ ỵ) — thiếu dấu `◌̣`
- Họ chữ có sừng (ơ ớ ờ ở ỡ ợ ư ứ ừ ử ữ ự) — thiếu dấu `◌̛`
- Đ / đ — chữ riêng không thể tổ hợp bằng dấu phụ
- Tổ hợp hai dấu chồng nhau như `ấ ầ ẩ ẫ ậ ắ ằ ẳ ẵ ặ ế ề ể ễ ệ ố ồ ổ ỗ ộ`

Công cụ này tự động phát hiện những ký tự thiếu, sinh ra các glyph tổ hợp tương ứng, đặt chúng vào đúng vị trí dùng dữ liệu anchor (GPOS) có sẵn của font, và lưu thành file font mới.

### Tính năng

- **134 ký tự tiếng Việt đầy đủ** (cả chữ hoa và chữ thường) trong dải Latin Extended Additional (U+1EA0–U+1EF9) cộng với Ơ, Ư, Đ, Ă, Â, Ê, Ô.
- **Hỗ trợ cả TTF và OTF** — tự động chọn cơ chế composite TrueType (glyf composite) hoặc CFF (flatten outlines).
- **Sử dụng anchor data** — nếu font đã có sẵn dữ liệu GPOS MarkBase, công cụ dùng đúng anchor mà nhà thiết kế đã cài, cho kết quả gần như đồng nhất với glyph được vẽ tay.
- **Heuristic thông minh** khi không có anchor: tự động ước lượng vị trí dấu dựa theo cap-height, x-height, và bbox của ký tự gốc.
- **Sắp dấu chồng đúng kiểu Việt Nam** — dấu thanh trên dấu mũ (như ấ, ề, ổ) được đặt theo phong cách typography Việt thật, không bị đẩy lên cao chót vót.
- **Donor font fallback** — nếu font gốc không có sẵn dấu phụ (◌̉ ◌̛ ◌̣) hoặc Đ, công cụ mượn từ font donor (ví dụ Noto Sans, Arsenal) chỉ phần dấu nhỏ, giữ nguyên hình dáng chữ cái của font gốc để bảo toàn phong cách.
- **Cập nhật OS/2** — bật bit Unicode Range cho Latin Extended Additional và codepage Vietnamese (1258) để hệ điều hành nhận diện font hỗ trợ tiếng Việt.

### Yêu cầu

- Python 3.10 trở lên
- [fontTools](https://github.com/fonttools/fonttools) (`pip install fonttools`)

### Cài đặt

```bash
git clone https://github.com/<your-username>/viet-hoa-font.git
cd viet-hoa-font
pip install -r requirements.txt
```

### Sử dụng

#### Kiểm tra độ phủ tiếng Việt của font

```bash
python viet_hoa.py font.ttf --check
```

Kết quả sẽ liệt kê các codepoint tiếng Việt còn thiếu, ví dụ:

```
Vietnamese coverage: 32/134
Missing:
  U+0102  Ă  LATIN CAPITAL LETTER A WITH BREVE
  U+0103  ă  LATIN SMALL LETTER A WITH BREVE
  ...
```

#### Việt hoá font

```bash
python viet_hoa.py font.ttf -o font-VN.ttf
```

Lệnh này dùng các glyph và dấu phụ có sẵn trong font gốc để tổ hợp các ký tự tiếng Việt còn thiếu.

#### Việt hoá với font donor

Đây là trường hợp phổ biến nhất — hầu hết font hiển thị (display fonts) không kèm đủ dấu phụ tiếng Việt:

```bash
python viet_hoa.py font.ttf -o font-VN.ttf --donor NotoSans-Regular.ttf
```

Công cụ chỉ mượn các dấu phụ nhỏ (`◌̉`, `◌̣`, `◌̛`) và Đ/đ từ font donor, các chữ tổ hợp khác đều được dựng từ chữ gốc trong font bạn cung cấp.

Gợi ý chọn font donor có cùng phong cách (sans-serif → sans-serif, serif → serif, đậm → đậm) để dấu phụ hoà hợp với phần còn lại.

#### Tham số đầy đủ

```
python viet_hoa.py INPUT [-o OUTPUT] [--donor DONOR] [--check] [-v]

Vị trí:
  INPUT              File font đầu vào (.otf hoặc .ttf)

Tuỳ chọn:
  -o, --output       Đường dẫn file font đầu ra
                     (mặc định: thêm hậu tố "-VN" vào tên file gốc)
  --donor            Font donor để mượn glyph khi cần
  --check            Chỉ kiểm tra coverage, không xuất file
  -v, --verbose      In thông tin chi tiết quá trình xử lý
```

### Ví dụ

Bao phủ đầy đủ 134 ký tự tiếng Việt sau khi Việt hoá:

![Bao phủ đầy đủ](docs/full-coverage.png)

### Cách hoạt động

#### 1. Phân tích NFD

Mỗi codepoint tiếng Việt được phân rã Unicode NFD thành chữ gốc + một hoặc nhiều dấu kết hợp. Ví dụ:

```
ấ (U+1EA5) → a (U+0061) + ◌̂ (U+0302) + ◌́ (U+0301)
ợ (U+1EE3) → o (U+006F) + ◌̛ (U+031B) + ◌̣ (U+0323)
```

#### 2. Chọn prefix có sẵn dài nhất

Với mỗi ký tự cần dựng, công cụ tìm prefix dài nhất đã có sẵn trong font. Nếu font đã có `â` (acircumflex) — kèm anchor data trỏ vị trí đặt dấu thanh — ta dùng `â + ◌́` thay vì `a + ◌̂ + ◌́`. Cách này tận dụng thiết kế gốc của font cho từng cặp glyph.

#### 3. Đặt vị trí dấu phụ

Theo thứ tự ưu tiên:

1. **Anchor GPOS MarkBase** — dùng dữ liệu attachment point gốc của font.
2. **Heuristic theo loại dấu**:
   - **Dấu trên** (acute, grave, circumflex, breve, hook, tilde): căn giữa ngang, đặt cách đỉnh ký tự một khoảng `mark_gap` đo được từ composite có sẵn của font (ví dụ từ `á`).
   - **Dấu dưới** (dot below): căn giữa dưới phần thân chữ — đặc biệt với họ chữ có sừng như ợ ự, dùng bbox của chữ trần (`o`, `u`) chứ không dùng bbox của ơ/ư.
   - **Sừng** (◌̛): gắn vào góc trên-phải của chữ, đỉnh nhô cao hơn đỉnh chữ ~25% chiều cao sừng.
3. **Theo dõi đỉnh hiệu lực** — khi xếp dấu thứ hai (như ớ = ơ + ◌́), tham chiếu đỉnh thực tế sau khi đã đặt dấu thứ nhất.

#### 4. Tạo glyph tổ hợp

- **TrueType (TTF)**: glyph composite với các component reference, không nhân bản outline → file nhẹ.
- **CFF/PostScript (OTF)**: do CFF không hỗ trợ composite, công cụ phải vẽ lại outline với transform offset (flatten).

#### 5. Cập nhật cmap và metadata

Map codepoint mới vào tất cả các Unicode cmap subtable, bật bit OS/2 phù hợp.

### Hạn chế

- **Mỹ thuật phụ thuộc font donor**: nếu phong cách font gốc và font donor khác nhau (ví dụ font hiển thị đậm vs sans-serif mảnh), các dấu phụ mượn về có thể trông không đồng bộ về độ dày nét. Hãy chọn donor cẩn thận.
- **Không xử lý kerning, ligature**: chỉ thêm glyph, không can thiệp các bảng GPOS/GSUB.
- **Không hỗ trợ variable font axes**: với font biến, output sẽ là instance mặc định.
- **Không cài thêm anchor cho glyph mới** — nghĩa là nếu bạn dùng font output này làm input cho lần Việt hoá tiếp theo, các glyph mới sẽ không có anchor data. Nhưng vì lần đầu đã đủ phủ tiếng Việt, điều này hiếm khi quan trọng.

### Cấu trúc dự án

```
viet-hoa-font/
├── viet_hoa.py           # Mã nguồn chính (single file)
├── README.md             # File bạn đang đọc
├── LICENSE               # Giấy phép MIT
├── requirements.txt      # Phụ thuộc Python
├── .gitignore
├── docs/
│   ├── before-after.png  # Ảnh minh hoạ trước/sau
│   └── full-coverage.png # Bảng 134 ký tự tiếng Việt
└── examples/
    └── README.md         # Hướng dẫn ví dụ
```

### Đóng góp

Pull request và issue đều được hoan nghênh. Đặc biệt mong nhận được:

- Test case với font có cấu trúc lạ (variable, color, multi-script)
- Cải tiến heuristic cho các trường hợp font donor khác phong cách
- Hỗ trợ thêm các ngôn ngữ khác sử dụng cùng cách tiếp cận (ví dụ thổ ngữ Pinyin có dấu)

### Giấy phép

MIT License — xem file [LICENSE](LICENSE).

### Lời cảm ơn

- Dự án [fontTools](https://github.com/fonttools/fonttools) — thư viện xử lý font đa năng nhất hiện có cho Python.
- Cộng đồng nhà thiết kế font Việt Nam đã chia sẻ kinh nghiệm về typography tiếng Việt qua nhiều bài viết và mã nguồn mở.

</details>
