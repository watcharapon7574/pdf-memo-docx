import subprocess
from flask import Flask, request, send_file, jsonify
from docxtpl import DocxTemplate
import os
import tempfile
import fitz  # PyMuPDF
from PIL import Image
import io
import json
import traceback
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
# --- ฟังก์ชันแปลง docx → pdf ด้วย LibreOffice ---
def convert_docx_to_pdf(docx_path, output_pdf_path):
    cmd = [
        "libreoffice",
        "--headless",
        "--convert-to", "pdf",
        "--outdir", os.path.dirname(output_pdf_path),
        docx_path
    ]
    subprocess.run(cmd, check=True)

# --- ฟังก์ชันแปลงตัวเลขเป็นเลขไทย ---
def to_thai_digits(text):
    thai_digits = '๐๑๒๓๔๕๖๗๘๙'
    def convert_char(c):
        if '0' <= c <= '9':
            return thai_digits[ord(c) - ord('0')]
        return c
    if not isinstance(text, str):
        return text
    return ''.join(convert_char(c) for c in text)

# --- ฟังก์ชันวาดข้อความเป็นภาพ ---
def draw_text_image(text, font_path, font_size=20, color=(2, 53, 139), scale=1):
    from PIL import ImageFont, ImageDraw
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


# --- สร้าง PDF จาก template docx ---
@app.route('/pdf', methods=['POST'])
def generate_pdf():
    try:
        data = request.json or {}

        # ===== เพิ่มส่วนนี้ =====
        required_fields = [
            "doc_number",
            "date",
            "subject",
            "attachment_title",
            "introduction",
            "author_name",
            "author_position",
            "fact",
            "proposal"
        ]
        # ตรวจสอบฟิลด์ที่ขาด หรือ ฟิลด์ที่เป็น "" (ว่าง)
        missing = [f for f in required_fields if not data.get(f)]
        if missing:
            return jsonify({'error': f"Missing fields: {', '.join(missing)}"}), 400
        # =====================

        # แปลงเลขใน dict เป็นเลขไทย
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
        if not os.path.exists(template_path):
            return jsonify({'error': f'Template file not found: {template_path}'}), 500
        doc = DocxTemplate(template_path)
        doc.render(data)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.docx') as tmp_docx:
            doc.save(tmp_docx.name)
            tmp_pdf = tmp_docx.name.replace('.docx', '.pdf')
            convert_docx_to_pdf(tmp_docx.name, tmp_pdf)
        return send_file(tmp_pdf, mimetype="application/pdf", as_attachment=True, download_name="memo.pdf")
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# --- วางลายเซ็น/ความเห็นลง PDF ที่อัพโหลดมา ---
@app.route('/add_signature', methods=['POST'])
def add_signature():
    try:
        DEFAULT_SIGNATURE_HEIGHT = 70
        DEFAULT_COMMENT_FONT_SIZE = 20
        font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew.ttf")
        if not os.path.isfile(font_path):
            return jsonify({'error': f"Font file not found: {font_path}"}), 500

        if 'pdf' not in request.files:
            return jsonify({'error': 'No PDF file uploaded'}), 400
        pdf_file = request.files['pdf']

        if 'signatures' not in request.form:
            return jsonify({'error': 'No signatures data'}), 400
        signatures = json.loads(request.form['signatures'])

        pdf_bytes = pdf_file.read()
        pdf = fitz.open(stream=pdf_bytes, filetype="pdf")

        # --- กลุ่ม sig ตามตำแหน่ง (page, x, y) ---
        from collections import defaultdict
        sig_dict = defaultdict(list)
        for sig in signatures:
            page_number = int(sig.get('page', 0))
            x = int(sig['x'])
            y = int(sig['y'])
            sig_dict[(page_number, x, y)].append(sig)

        # --- วาดลายเซ็นและความเห็นทีละจุด ---
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
                    img = draw_text_image(text, font_path, font_size=font_size, color=color, scale=1)
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    rect = fitz.Rect(x, current_y, x + img.width, current_y + img.height)
                    page.insert_image(rect, stream=img_byte_arr.getvalue())
                    current_y += img.height
                elif sig['type'] == 'image':
                    file_key = sig['file_key']
                    if file_key not in request.files:
                        continue
                    signature_file = request.files[file_key]
                    img = Image.open(signature_file)
                    fixed_height = DEFAULT_SIGNATURE_HEIGHT
                    ratio = fixed_height / img.height
                    new_width = int(img.width * ratio)
                    img = img.resize((new_width, fixed_height), resample=Image.LANCZOS)
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    rect = fitz.Rect(x, current_y, x + new_width, current_y + fixed_height)
                    page.insert_image(rect, stream=img_byte_arr.getvalue())
                    current_y += fixed_height

        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_pdf:
            pdf.save(tmp_pdf.name)
        pdf.close()
        return send_file(tmp_pdf.name, mimetype="application/pdf", as_attachment=True, download_name="signed.pdf")
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# --- สร้าง PDF และวางลายเซ็นในขั้นตอนเดียว ---
@app.route('/generate_signed_pdf', methods=['POST'])
def generate_signed_pdf():
    try:
        # 1. Render Word → PDF
        if 'template_data' not in request.form or 'signatures' not in request.form:
            return jsonify({'error': 'template_data and signatures are required'}), 400
        data = json.loads(request.form['template_data'])
        # แปลงเลขใน dict เป็นเลขไทย
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
        if not os.path.exists(template_path):
            return jsonify({'error': f'Template file not found: {template_path}'}), 500
        doc = DocxTemplate(template_path)
        doc.render(data)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.docx') as tmp_docx:
            doc.save(tmp_docx.name)
            tmp_pdf_path = tmp_docx.name.replace('.docx', '.pdf')
            convert_docx_to_pdf(tmp_docx.name, tmp_pdf_path)

        pdf = fitz.open(tmp_pdf_path)

        # 2. Add Signatures & Comments
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
                    img = Image.open(io.BytesIO(img_byte_arr.getvalue()))
                    img = img.resize((round(img.width/4), round(img.height/4)), resample=Image.LANCZOS)
                    rect = fitz.Rect(x, current_y, x + img.width, current_y + img.height)
                    page.insert_image(rect, stream=img_byte_arr.getvalue())
                    current_y += img.height
                elif sig['type'] == 'image':
                    file_key = sig['file_key']
                    if file_key not in request.files:
                        continue
                    signature_file = request.files[file_key]
                    img = Image.open(signature_file)
                    fixed_height = DEFAULT_SIGNATURE_HEIGHT
                    ratio = fixed_height / img.height
                    new_width = int(img.width * ratio)
                    img = img.resize((new_width, fixed_height), resample=Image.LANCZOS)
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    rect = fitz.Rect(x, current_y, x + new_width, current_y + fixed_height)
                    page.insert_image(rect, stream=img_byte_arr.getvalue())
                    current_y += fixed_height

        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_signed_pdf:
            pdf.save(tmp_signed_pdf.name)
        pdf.close()
        return send_file(tmp_signed_pdf.name, mimetype="application/pdf", as_attachment=True)
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# --- สร้าง PDF จาก docx หรือใช้ PDF ที่อัพโหลด แล้ววางลายเซ็นและคอมเมนต์ในไฟล์เดียว ---
@app.route('/pdf_with_signature', methods=['POST'])
def pdf_with_signature():
    try:
        # Check signatures field
        if 'signatures' not in request.form:
            return jsonify({'error': 'signatures field is required'}), 400
        signatures = json.loads(request.form['signatures'])

        font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew.ttf")
        if not os.path.isfile(font_path):
            return jsonify({'error': f"Font file not found: {font_path}"}), 500

        # Determine if template_data or pdf file is provided
        pdf = None
        tmp_pdf_path = None
        if 'pdf' in request.files:
            # Use uploaded PDF file
            pdf_file = request.files['pdf']
            pdf_bytes = pdf_file.read()
            pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
        else:
            # Use template_data to generate PDF
            data = {}
            if 'template_data' in request.form:
                data = json.loads(request.form['template_data'])
            # แปลงเลขใน dict เป็นเลขไทย
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
            if not os.path.exists(template_path):
                return jsonify({'error': f'Template file not found: {template_path}'}), 500
            doc = DocxTemplate(template_path)
            doc.render(data)
            with tempfile.NamedTemporaryFile(delete=False, suffix='.docx') as tmp_docx:
                doc.save(tmp_docx.name)
                tmp_pdf_path = tmp_docx.name.replace('.docx', '.pdf')
                convert_docx_to_pdf(tmp_docx.name, tmp_pdf_path)
            pdf = fitz.open(tmp_pdf_path)

        from collections import defaultdict
        sig_dict = defaultdict(list)
        for sig in signatures:
            page_number = int(sig.get('page', 0))
            x = int(sig['x'])
            y = int(sig['y'])
            sig_dict[(page_number, x, y)].append(sig)

        DEFAULT_SIGNATURE_HEIGHT = 50
        DEFAULT_COMMENT_FONT_SIZE = 20

        for (page_number, x, y), sigs in sig_dict.items():
            if page_number >= len(pdf):
                continue
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
                    img = Image.open(io.BytesIO(img_byte_arr.getvalue()))
                    img = img.resize((round(img.width/4), round(img.height/4)), resample=Image.LANCZOS)
                    rect = fitz.Rect(x, current_y, x + img.width, current_y + img.height)
                    page.insert_image(rect, stream=img_byte_arr.getvalue())
                    current_y += img.height
                elif sig['type'] == 'image':
                    file_key = sig['file_key']
                    if file_key not in request.files:
                        continue
                    signature_file = request.files[file_key]
                    img = Image.open(signature_file)
                    fixed_height = DEFAULT_SIGNATURE_HEIGHT
                    ratio = fixed_height / img.height
                    new_width = int(img.width * ratio)
                    img = img.resize((new_width, fixed_height), resample=Image.LANCZOS)
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    rect = fitz.Rect(x, current_y, x + new_width, current_y + fixed_height)
                    page.insert_image(rect, stream=img_byte_arr.getvalue())
                    current_y += fixed_height

        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_signed_pdf:
            pdf.save(tmp_signed_pdf.name)
        pdf.close()
        if tmp_pdf_path and os.path.exists(tmp_pdf_path):
            try:
                os.remove(tmp_pdf_path)
            except:
                pass
        return send_file(tmp_signed_pdf.name, mimetype="application/pdf", as_attachment=True)
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

if __name__ == "__main__":
    # สำหรับ Railway ต้องฟังที่ 0.0.0.0
    app.run(debug=True, host="0.0.0.0", port=5000)