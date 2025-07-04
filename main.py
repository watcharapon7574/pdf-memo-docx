from flask import Flask, request, send_file, jsonify
from docxtpl import DocxTemplate
import os
import tempfile
import fitz  # PyMuPDF
from PIL import Image
import io
import json

def to_thai_digits(text):
    thai_digits = '๐๑๒๓๔๕๖๗๘๙'
    def convert_char(c):
        if '0' <= c <= '9':
            return thai_digits[ord(c) - ord('0')]
        return c
    if not isinstance(text, str):
        return text
    return ''.join(convert_char(c) for c in text)

app = Flask(__name__)

def draw_text_image(text, font_path, font_size=20, color=(2, 53, 139), scale=1):
    from PIL import Image, ImageDraw, ImageFont

    big_font_size = font_size * scale
    font = ImageFont.truetype(font_path, big_font_size)
    padding = 4 * scale
    lines = text.split('\n')
    width = max([font.getbbox(line)[2] for line in lines]) + 2 * padding
    height = sum([font.getbbox(line)[3] - font.getbbox(line)[1] for line in lines]) + 2 * padding + (len(lines)-1)*2*scale
    img = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    y = padding
    for line in lines:
        draw.text((padding, y), line, font=font, fill=color)
        y += font.getbbox(line)[3] - font.getbbox(line)[1] + 2*scale
    return img

@app.route('/pdf', methods=['POST'])
def generate_pdf():
    try:
        data = request.json or {}
        # convert all string fields in data to Thai digits
        def convert_dict(d):
            if isinstance(d, dict):
                return {k: convert_dict(v) for k, v in d.items()}
            elif isinstance(d, list):
                return [convert_dict(i) for i in d]
            elif isinstance(d, str):
                return to_thai_digits(d)
            else:
                return d
        data = convert_dict(data)
        template_path = os.path.join(os.path.dirname(__file__), "templates", "memo-template2.docx")
        doc = DocxTemplate(template_path)
        doc.render(data)
        tmp_docx = tempfile.NamedTemporaryFile(delete=False, suffix='.docx')
        doc.save(tmp_docx.name)
        from docx2pdf import convert
        tmp_pdf = tmp_docx.name.replace('.docx', '.pdf')
        convert(tmp_docx.name, tmp_pdf)
        return send_file(tmp_pdf, mimetype="application/pdf", as_attachment=True, download_name="memo.pdf")
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/add_signature', methods=['POST'])
def add_signature():
    try:
        DEFAULT_SIGNATURE_HEIGHT = 70   # ควบคุมความสูงลายเซ็น
        DEFAULT_COMMENT_FONT_SIZE = 20  # ฟิกซ์ขนาดฟอนต์ไว้เลย

        font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew.ttf")
        if not os.path.isfile(font_path):
            return jsonify({'error': f"Font file not found: {font_path}"}), 500

        pdf_file = request.files['pdf']
        signatures = json.loads(request.form['signatures'])
        pdf_bytes = pdf_file.read()
        pdf = fitz.open(stream=pdf_bytes, filetype="pdf")

        from collections import defaultdict
        sig_dict = defaultdict(list)
        for sig in signatures:
            page_number = int(sig.get('page', 0))
            x = int(sig['x'])
            y = int(sig['y'])
            sig_dict[(page_number, x, y)].append(sig)

        for (page_number, x, y), sigs in sig_dict.items():
            page = pdf[page_number]
            sigs_sorted = sorted(sigs, key=lambda s: 0 if s['type'] == 'text' else 1)
            current_y = y
            for sig in sigs_sorted:
                if sig['type'] == 'text':
                    text = to_thai_digits(sig.get('text', ''))
                    font_size = DEFAULT_COMMENT_FONT_SIZE  # ฟิกซ์ขนาดฟอนต์ไว้เลย
                    orig_color = sig.get('color', (2, 53, 139))
                    if isinstance(orig_color, (list, tuple)):
                        r = min(int(orig_color[0]*0.8), 255)
                        g = min(int(orig_color[1]*0.8), 255)
                        b = min(int(orig_color[2]*0.8), 255)
                        color = (r, g, b)
                    else:
                        color = (2, 53, 139)
                    img = draw_text_image(text, font_path, font_size=font_size, color=color, scale=1)
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    img_byte_arr = img_byte_arr.getvalue()
                    rect = fitz.Rect(x, current_y, x + img.width, current_y + img.height)
                    page.insert_image(rect, stream=img_byte_arr)
                    current_y += img.height
                elif sig['type'] == 'image':
                    file_key = sig['file_key']
                    signature_file = request.files[file_key]
                    img = Image.open(signature_file)
                    fixed_height = DEFAULT_SIGNATURE_HEIGHT
                    ratio = fixed_height / img.height
                    new_width = int(img.width * ratio)
                    img = img.resize((new_width, fixed_height), resample=Image.LANCZOS)
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    img_byte_arr = img_byte_arr.getvalue()
                    rect = fitz.Rect(x, current_y, x + new_width, current_y + fixed_height)
                    page.insert_image(rect, stream=img_byte_arr)
                    current_y += fixed_height

        tmp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        pdf.save(tmp_pdf.name)
        pdf.close()
        return send_file(tmp_pdf.name, mimetype="application/pdf", as_attachment=True, download_name="signed.pdf")
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/generate_signed_pdf', methods=['POST'])
def generate_signed_pdf():
    try:
        # -------- 1) Render Word → PDF --------
        data = json.loads(request.form['template_data'])
        # convert all string fields in data to Thai digits
        def convert_dict(d):
            if isinstance(d, dict):
                return {k: convert_dict(v) for k, v in d.items()}
            elif isinstance(d, list):
                return [convert_dict(i) for i in d]
            elif isinstance(d, str):
                return to_thai_digits(d)
            else:
                return d
        data = convert_dict(data)
        template_path = os.path.join(os.path.dirname(__file__), "templates", "memo-template2.docx")
        doc = DocxTemplate(template_path)
        doc.render(data)
        tmp_docx_path = os.path.join(os.path.dirname(__file__), "tmp_memo.docx")
        doc.save(tmp_docx_path)

        from docx2pdf import convert
        tmp_pdf_path = tmp_docx_path.replace('.docx', '.pdf')
        convert(tmp_docx_path, tmp_pdf_path)

        pdf = fitz.open(tmp_pdf_path)

        # -------- 2) Add Signatures & Comments --------
        signatures = json.loads(request.form['signatures'])
        from collections import defaultdict
        sig_dict = defaultdict(list)
        for sig in signatures:
            page_number = int(sig.get('page', 0))
            x = int(sig['x'])
            y = int(sig['y'])
            sig_dict[(page_number, x, y)].append(sig)

        DEFAULT_SIGNATURE_HEIGHT = 50
        DEFAULT_COMMENT_FONT_SIZE = 20
        font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew.ttf")

        for (page_number, x, y), sigs in sig_dict.items():
            page = pdf[page_number]
            sigs_sorted = sorted(sigs, key=lambda s: 0 if s['type'] == 'text' else 1)
            current_y = y
            for sig in sigs_sorted:
                if sig['type'] == 'text':
                    text = to_thai_digits(sig.get('text', ''))
                    font_size = DEFAULT_COMMENT_FONT_SIZE
                    orig_color = sig.get('color', (2, 53, 139))
                    if isinstance(orig_color, (list, tuple)):
                        r = min(int(orig_color[0]*0.8), 255)
                        g = min(int(orig_color[1]*0.8), 255)
                        b = min(int(orig_color[2]*0.8), 255)
                        color = (r, g, b)
                    else:
                        color = (2, 53, 139)
                    img = draw_text_image(text, font_path, font_size=font_size, color=color, scale=4)
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    img_byte_arr = img_byte_arr.getvalue()
                    img = Image.open(io.BytesIO(img_byte_arr))
                    img = img.resize((round(img.width/4), round(img.height/4)), resample=Image.LANCZOS)
                    rect = fitz.Rect(x, current_y, x + img.width, current_y + img.height)
                    page.insert_image(rect, stream=img_byte_arr)
                    current_y += img.height
                elif sig['type'] == 'image':
                    file_key = sig['file_key']
                    signature_file = request.files[file_key]
                    img = Image.open(signature_file)
                    fixed_height = DEFAULT_SIGNATURE_HEIGHT
                    ratio = fixed_height / img.height
                    new_width = int(img.width * ratio)
                    img = img.resize((new_width, fixed_height), resample=Image.LANCZOS)
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    img_byte_arr = img_byte_arr.getvalue()
                    rect = fitz.Rect(x, current_y, x + new_width, current_y + fixed_height)
                    page.insert_image(rect, stream=img_byte_arr)
                    current_y += fixed_height

        tmp_signed_pdf = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        pdf.save(tmp_signed_pdf.name)
        pdf.close()
        return send_file(tmp_signed_pdf.name, mimetype="application/pdf", as_attachment=False)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)