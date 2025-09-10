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
            "date",
            "subject",
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

        # จัดรูปแบบ proposal ให้มี indent สำหรับเครื่องหมาย -
        if 'proposal' in data and data['proposal']:
            import re
            proposal_text = data['proposal']
            # หาตำแหน่งของ - และแทนที่ทีละตัว
            lines = []
            current_line = ""
            i = 0
            while i < len(proposal_text):
                if proposal_text[i] == '-' and i > 0:
                    # เจอ - ที่ไม่ใช่ตัวแรก
                    if current_line.strip():
                        lines.append(current_line.rstrip())
                    current_line = "          - "
                    i += 1
                    # ข้าม space หลัง - ถ้ามี
                    while i < len(proposal_text) and proposal_text[i] == ' ':
                        i += 1
                    continue
                else:
                    current_line += proposal_text[i]
                    i += 1
            
            if current_line.strip():
                lines.append(current_line.rstrip())
            
            # รวมผลลัพธ์
            if lines:
                if lines[0].startswith('- '):
                    lines[0] = '          ' + lines[0]
                data['proposal'] = '\n'.join(lines)
            else:
                data['proposal'] = proposal_text

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


@app.route('/add_signature_v2', methods=['POST'])
def add_signature_v2():
    # --- ฟังก์ชันวาดข้อความเป็นภาพ (v2) ---
    def draw_text_image_v2(text, font_path, font_size=20, color=(2, 53, 139), scale=1, font_weight="regular"):
        from PIL import ImageFont, ImageDraw, Image
        # เลือก font ตาม font_weight
        if font_weight == "bold":
            font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew Bold.ttf")
        big_font_size = font_size * scale
        font = ImageFont.truetype(font_path, big_font_size)
        padding = 4 * scale
        lines = text.split('\n')
        # ใช้ dummy image สำหรับวัด textbbox
        dummy_img = Image.new("RGBA", (10, 10), (255, 255, 255, 0))
        dummy_draw = ImageDraw.Draw(dummy_img)
        line_sizes = []
        for line in lines:
            bbox = dummy_draw.textbbox((0, 0), line, font=font)
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            line_sizes.append((width, height, bbox))
        max_width = max([w for w, h, _ in line_sizes]) + 2 * padding
        total_height = sum([h for w, h, _ in line_sizes]) + 2 * padding + (len(lines)-1)*2*scale
        img = Image.new("RGBA", (max_width, total_height), (255, 255, 255, 0))
        draw = ImageDraw.Draw(img)
        y = padding
        for i, line in enumerate(lines):
            w, h, bbox = line_sizes[i]
            # ใช้การจัดข้อความแบบเดียวกับ draw_text_image (ไม่จัดกึ่งกลาง)
            draw.text((padding, y - bbox[1]), line, font=font, fill=color)
            y += h + 2*scale
        return img
    try:
        DEFAULT_SIGNATURE_HEIGHT = 50
        DEFAULT_COMMENT_FONT_SIZE = 18
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

        from collections import defaultdict
        sig_dict = defaultdict(list)
        for sig in signatures:
            page_number = int(sig.get('page', 0))
            x = int(sig['x'])
            y = int(sig['y'])
            # รองรับ width/height สำหรับ center positioning
            width = sig.get('width', 0)
            height = sig.get('height', 0)
            
            # ถ้าไม่มี width/height ให้ใช้ค่า default สำหรับ center positioning
            if width == 0 and height == 0:
                width = 120  # default width
                height = 60  # default height
                print(f"DEBUG: Using default dimensions {width}x{height} for signature at ({x}, {y})")
            
            sig_dict[(page_number, x, y, width, height)].append(sig)

        for (page_number, x, y, width, height), sigs in sig_dict.items():
            page = pdf[page_number]
            
            # Debug: แสดงข้อมูล page และพิกัด
            page_rect = page.rect
            print(f"DEBUG: Page {page_number} - Size: {page_rect.width}x{page_rect.height}")
            print(f"DEBUG: Original coordinates: ({x}, {y})")
            print(f"DEBUG: Signature dimensions: {width}x{height}")
            print(f"DEBUG: Page bounds: x(0-{page_rect.width}), y(0-{page_rect.height})")
            
            # ถ้ามี width/height แสดงว่าเป็น center positioning
            is_center_positioning = width > 0 and height > 0
            
            # Logic สำหรับปรับพิกัด Y ให้กลับกัน (บนเป็นล่าง ล่างเป็นบน)
            # คำนวณ: new_y = (page_height - original_y - signature_height) + height_offset
            if is_center_positioning:
                # สำหรับ center positioning ใช้ height ที่กำหนดมา
                adjusted_y = page_rect.height - y - height
                # เลื่อนลงแนวดิ่งเท่ากับ height (60)
                adjusted_y += height+30
                print(f"DEBUG: Y-axis flip with center positioning: {y} -> {adjusted_y} (with +{height} offset)")
                center_x = x
                center_y = adjusted_y
                print(f"DEBUG: Using center positioning - adjusted coordinates")
                print(f"DEBUG: Center point: ({center_x}, {center_y})")
                print(f"DEBUG: Bounding box: {width}x{height}")
            else:
                # สำหรับ top-left positioning ใช้ default signature height
                signature_box_height = 60  # default height สำหรับการคำนวณ
                adjusted_y = page_rect.height - y - signature_box_height
                # เลื่อนลงแนวดิ่งเท่ากับ 60
                adjusted_y += 60
                print(f"DEBUG: Y-axis flip with top-left positioning: {y} -> {adjusted_y} (with +60 offset)")
                center_x = x
                center_y = adjusted_y
                print(f"DEBUG: Using top-left positioning - adjusted coordinates")
            
            current_y = center_y  # ใช้ค่า Y ที่ปรับแล้ว
            # Check if any signature has 'lines' field
            has_lines = any('lines' in sig for sig in sigs)
            if has_lines:
                # For each sig with lines, draw lines in order
                for sig in sigs:
                    lines = sig.get('lines')
                    if not lines:
                        # fallback to old logic for this sig
                        if sig['type'] == 'text':
                            text = to_thai_digits(sig.get('text', ''))
                            # ถ้า type == "comment" ให้ font_size=20, font_weight="bold"
                            font_size = 20 if sig.get('type') == 'comment' else DEFAULT_COMMENT_FONT_SIZE
                            font_weight = "bold" if sig.get('type') == 'comment' else "regular"
                            orig_color = sig.get('color', (2, 53, 139))
                            if isinstance(orig_color, (list, tuple)):
                                r = min(int(orig_color[0]*0.8), 255)
                                g = min(int(orig_color[1]*0.8), 255)
                                b = min(int(orig_color[2]*0.8), 255)
                                color = (r, g, b)
                            else:
                                color = (2, 53, 139)
                            img = draw_text_image_v2(text, font_path, font_size=font_size, color=color, scale=1, font_weight=font_weight)
                            img_byte_arr = io.BytesIO()
                            img.save(img_byte_arr, format='PNG')
                            # ใช้ center positioning ถ้ามี width/height
                            if is_center_positioning:
                                left_x = center_x - img.width // 2
                                top_y = center_y - img.height // 2
                            else:
                                left_x = x
                                top_y = current_y
                            rect = fitz.Rect(left_x, top_y, left_x + img.width, top_y + img.height)
                            print(f"DEBUG: Text '{text}' placed at rect: {rect} (center_pos: {is_center_positioning})")
                            page.insert_image(rect, stream=img_byte_arr.getvalue())
                            if not is_center_positioning:
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
                            # ใช้ center positioning ถ้ามี width/height
                            if is_center_positioning:
                                left_x = center_x - new_width // 2
                                top_y = center_y - fixed_height // 2
                            else:
                                left_x = x
                                top_y = current_y
                            rect = fitz.Rect(left_x, top_y, left_x + new_width, top_y + fixed_height)
                            print(f"DEBUG: Image placed at rect: {rect} (center_pos: {is_center_positioning})")
                            page.insert_image(rect, stream=img_byte_arr.getvalue())
                            if not is_center_positioning:
                                current_y += fixed_height
                    else:
                        # draw lines in order - รองรับ center positioning
                        if is_center_positioning:
                            # สำหรับ center positioning ให้เริ่มจากด้านบนของ bounding box
                            current_y = center_y - height // 2
                        
                        for line in lines:
                            line_type = line.get('type')
                            if line_type == 'image':
                                file_key = line.get('file_key')
                                if file_key and file_key in request.files:
                                    signature_file = request.files[file_key]
                                    img = Image.open(signature_file)
                                    fixed_height = DEFAULT_SIGNATURE_HEIGHT
                                    ratio = fixed_height / img.height
                                    new_width = int(img.width * ratio)
                                    img = img.resize((new_width, fixed_height), resample=Image.LANCZOS)
                                    img_byte_arr = io.BytesIO()
                                    img.save(img_byte_arr, format='PNG')
                                    
                                    if is_center_positioning:
                                        left_x = center_x - new_width // 2  # คืนค่าเดิม แต่เพิ่ม debug
                                        top_y = current_y
                                        print(f"DEBUG: Image center positioning - center_x:{center_x}, new_width:{new_width}, left_x:{left_x}")
                                        print(f"DEBUG: Expected position - should place image at left edge: {left_x}")
                                    else:
                                        left_x = x
                                        top_y = current_y
                                    
                                    rect = fitz.Rect(left_x, top_y, left_x + new_width, top_y + fixed_height)
                                    print(f"DEBUG: Image rect: {rect}")
                                    page.insert_image(rect, stream=img_byte_arr.getvalue())
                                    current_y += fixed_height
                            else:
                                # For text types: 'comment', 'name', 'position', 'academic_rank', 'org_structure_role', 'timestamp'
                                text_value = line.get('text') or line.get('value') or line.get('comment') or ''
                                text = to_thai_digits(text_value)
                                # ถ้า type == "comment" ให้ font_size=20, font_weight="bold"
                                font_size = 20 if line_type == 'comment' else DEFAULT_COMMENT_FONT_SIZE
                                font_weight = "bold" if line_type == 'comment' else "regular"
                                orig_color = line.get('color', (2, 53, 139))
                                if isinstance(orig_color, (list, tuple)):
                                    r = min(int(orig_color[0]*0.8), 255)
                                    g = min(int(orig_color[1]*0.8), 255)
                                    b = min(int(orig_color[2]*0.8), 255)
                                    color = (r, g, b)
                                else:
                                    color = (2, 53, 139)
                                img = draw_text_image_v2(text, font_path, font_size=font_size, color=color, scale=1, font_weight=font_weight)
                                img_byte_arr = io.BytesIO()
                                img.save(img_byte_arr, format='PNG')
                                
                                if is_center_positioning:
                                    left_x = center_x - img.width // 2  # คืนค่าเดิม
                                    top_y = current_y
                                    print(f"DEBUG: Text center positioning - center_x:{center_x}, img.width:{img.width}, left_x:{left_x}")
                                    print(f"DEBUG: Expected position - should place text at left edge: {left_x}")
                                else:
                                    left_x = x
                                    top_y = current_y
                                
                                rect = fitz.Rect(left_x, top_y, left_x + img.width, top_y + img.height)
                                print(f"DEBUG: Text '{text}' rect: {rect}")
                                page.insert_image(rect, stream=img_byte_arr.getvalue())
                                current_y += img.height
            else:
                # fallback to old logic
                sigs_sorted = sorted(sigs, key=lambda s: 0 if s['type'] == 'text' else 1)
                for sig in sigs_sorted:
                    if sig['type'] == 'text':
                        text = to_thai_digits(sig.get('text', ''))
                        # ถ้า type == "comment" ให้ font_size=20, font_weight="bold"
                        font_size = 20 if sig.get('type') == 'comment' else DEFAULT_COMMENT_FONT_SIZE
                        font_weight = "bold" if sig.get('type') == 'comment' else "regular"
                        orig_color = sig.get('color', (2, 53, 139))
                        if isinstance(orig_color, (list, tuple)):
                            r = min(int(orig_color[0]*0.8), 255)
                            g = min(int(orig_color[1]*0.8), 255)
                            b = min(int(orig_color[2]*0.8), 255)
                            color = (r, g, b)
                        else:
                            color = (2, 53, 139)
                        img = draw_text_image_v2(text, font_path, font_size=font_size, color=color, scale=1, font_weight=font_weight)
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



@app.route('/2in1memo', methods=['POST'])
def generate_2in1_memo():
    """รวมการทำงานของ /pdf และ /add_signature_v2 ในครั้งเดียว"""
    try:
        # ตรวจสอบข้อมูลที่ส่งมา
        if not request.form and not request.json:
            return jsonify({'error': 'No data provided'}), 400
        
        # ข้อมูลสำหรับสร้าง PDF จาก form หรือ json
        if request.form:
            # กรณีส่งมาแบบ multipart/form-data
            data = {}
            for key in request.form:
                if key != 'signatures':  # signatures จะจัดการแยก
                    data[key] = request.form[key]
        else:
            # กรณีส่งมาแบบ JSON
            data = request.json or {}

        # ===== ส่วนที่ 1: สร้าง PDF (จาก /pdf) =====
        required_fields = [
            "doc_number",
            "date",
            "subject",
            "introduction", 
            "author_name",
            "author_position",
            "fact",
            "proposal"
        ]
        
        missing = [f for f in required_fields if not data.get(f)]
        if missing:
            return jsonify({'error': f"Missing fields: {', '.join(missing)}"}), 400

        # จัดรูปแบบ proposal ให้มี indent สำหรับเครื่องหมาย -
        if 'proposal' in data and data['proposal']:
            proposal_text = data['proposal']
            lines = []
            current_line = ""
            i = 0
            while i < len(proposal_text):
                if proposal_text[i] == '-' and i > 0:
                    if current_line.strip():
                        lines.append(current_line.rstrip())
                    current_line = "          - "
                    i += 1
                    while i < len(proposal_text) and proposal_text[i] == ' ':
                        i += 1
                    continue
                else:
                    current_line += proposal_text[i]
                    i += 1
            
            if current_line.strip():
                lines.append(current_line.rstrip())
            
            if lines:
                if lines[0].startswith('- '):
                    lines[0] = '          ' + lines[0]
                data['proposal'] = '\n'.join(lines)
            else:
                data['proposal'] = proposal_text

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

        # ===== ส่วนที่ 2: เพิ่มลายเซ็น (จาก /add_signature_v2) =====
        
        # ตรวจสอบว่ามี signatures หรือไม่
        if 'signatures' not in request.form:
            # ถ้าไม่มี signatures ให้ return PDF ธรรมดา
            return send_file(tmp_pdf, mimetype="application/pdf", as_attachment=True, download_name="memo.pdf")
        
        signatures = json.loads(request.form['signatures'])
        
        # ตรวจสอบว่ามีไฟล์เอกสารแนบที่ต้องการแปะลายเซ็นหรือไม่
        attachment_pdf = None
        if 'attachment_pdf' in request.files:
            attachment_file = request.files['attachment_pdf']
            attachment_pdf_bytes = attachment_file.read()
            attachment_pdf = fitz.open(stream=attachment_pdf_bytes, filetype="pdf")
        
        # ฟังก์ชันวาดข้อความเป็นภาพ (v2)
        def draw_text_image_v2(text, font_path, font_size=20, color=(2, 53, 139), scale=1, font_weight="regular"):
            from PIL import ImageFont, ImageDraw, Image
            if font_weight == "bold":
                font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew Bold.ttf")
            big_font_size = font_size * scale
            font = ImageFont.truetype(font_path, big_font_size)
            padding = 4 * scale
            lines = text.split('\n')
            dummy_img = Image.new("RGBA", (10, 10), (255, 255, 255, 0))
            dummy_draw = ImageDraw.Draw(dummy_img)
            line_sizes = []
            for line in lines:
                bbox = dummy_draw.textbbox((0, 0), line, font=font)
                width = bbox[2] - bbox[0]
                height = bbox[3] - bbox[1]
                line_sizes.append((width, height, bbox))
            max_width = max([w for w, h, _ in line_sizes]) + 2 * padding
            total_height = sum([h for w, h, _ in line_sizes]) + 2 * padding + (len(lines)-1)*2*scale
            img = Image.new("RGBA", (max_width, total_height), (255, 255, 255, 0))
            draw = ImageDraw.Draw(img)
            y = padding
            for i, line in enumerate(lines):
                w, h, bbox = line_sizes[i]
                offset_x = (max_width - w) // 2
                draw.text((offset_x, y - bbox[1]), line, font=font, fill=color)
                y += h + 2*scale
            return img

        DEFAULT_SIGNATURE_HEIGHT = 50
        DEFAULT_COMMENT_FONT_SIZE = 18
        font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew.ttf")
        
        if not os.path.isfile(font_path):
            return jsonify({'error': f"Font file not found: {font_path}"}), 500

        # เปิด PDF ที่เพิ่งสร้าง
        main_pdf = fitz.open(tmp_pdf)

        from collections import defaultdict
        sig_dict = defaultdict(list)
        for sig in signatures:
            page_number = int(sig.get('page', 0))
            x = int(sig['x'])
            y = int(sig['y'])
            # แยกประเภท PDF ที่จะแปะลายเซ็น
            pdf_type = sig.get('pdf_type', 'main')  # 'main' หรือ 'attachment'
            sig_dict[(pdf_type, page_number, x, y)].append(sig)

        # ฟังก์ชันสำหรับวาดลายเซ็น
        def process_signatures_on_pdf(pdf, sig_dict_filtered):
            for (page_number, x, y), sigs in sig_dict_filtered.items():
                if page_number >= len(pdf):
                    continue  # ข้ามถ้าหน้าไม่มี
                page = pdf[page_number]
                current_y = y
                has_lines = any('lines' in sig for sig in sigs)
                
                if has_lines:
                    for sig in sigs:
                        lines = sig.get('lines')
                        if not lines:
                            # fallback to old logic
                            if sig['type'] == 'text':
                                text = to_thai_digits(sig.get('text', ''))
                                font_size = 20 if sig.get('type') == 'comment' else DEFAULT_COMMENT_FONT_SIZE
                                font_weight = "bold" if sig.get('type') == 'comment' else "regular"
                                orig_color = sig.get('color', (2, 53, 139))
                                if isinstance(orig_color, (list, tuple)):
                                    r = min(int(orig_color[0]*0.8), 255)
                                    g = min(int(orig_color[1]*0.8), 255)
                                    b = min(int(orig_color[2]*0.8), 255)
                                    color = (r, g, b)
                                else:
                                    color = (2, 53, 139)
                                img = draw_text_image_v2(text, font_path, font_size=font_size, color=color, scale=1, font_weight=font_weight)
                                img_byte_arr = io.BytesIO()
                                img.save(img_byte_arr, format='PNG')
                                left_x = x - img.width // 2
                                rect = fitz.Rect(left_x, current_y, left_x + img.width, current_y + img.height)
                                page.insert_image(rect, stream=img_byte_arr.getvalue())
                                current_y += img.height
                            elif sig['type'] == 'image':
                                file_key = sig['file_key']
                                if file_key in request.files:
                                    signature_file = request.files[file_key]
                                    img = Image.open(signature_file)
                                    fixed_height = DEFAULT_SIGNATURE_HEIGHT
                                    ratio = fixed_height / img.height
                                    new_width = int(img.width * ratio)
                                    img = img.resize((new_width, fixed_height), resample=Image.LANCZOS)
                                    img_byte_arr = io.BytesIO()
                                    img.save(img_byte_arr, format='PNG')
                                    left_x = x - new_width // 2
                                    rect = fitz.Rect(left_x, current_y, left_x + new_width, current_y + fixed_height)
                                    page.insert_image(rect, stream=img_byte_arr.getvalue())
                                    current_y += fixed_height
                        else:
                            # draw lines in order
                            for line in lines:
                                line_type = line.get('type')
                                if line_type == 'image':
                                    file_key = line.get('file_key')
                                    if file_key and file_key in request.files:
                                        signature_file = request.files[file_key]
                                        img = Image.open(signature_file)
                                        fixed_height = DEFAULT_SIGNATURE_HEIGHT
                                        ratio = fixed_height / img.height
                                        new_width = int(img.width * ratio)
                                        img = img.resize((new_width, fixed_height), resample=Image.LANCZOS)
                                        img_byte_arr = io.BytesIO()
                                        img.save(img_byte_arr, format='PNG')
                                        left_x = x - new_width // 2
                                        rect = fitz.Rect(left_x, current_y, left_x + new_width, current_y + fixed_height)
                                        page.insert_image(rect, stream=img_byte_arr.getvalue())
                                        current_y += fixed_height
                                else:
                                    text_value = line.get('text') or line.get('value') or line.get('comment') or ''
                                    text = to_thai_digits(text_value)
                                    font_size = 20 if line_type == 'comment' else DEFAULT_COMMENT_FONT_SIZE
                                    font_weight = "bold" if line_type == 'comment' else "regular"
                                    orig_color = line.get('color', (2, 53, 139))
                                    if isinstance(orig_color, (list, tuple)):
                                        r = min(int(orig_color[0]*0.8), 255)
                                        g = min(int(orig_color[1]*0.8), 255)
                                        b = min(int(orig_color[2]*0.8), 255)
                                        color = (r, g, b)
                                    else:
                                        color = (2, 53, 139)
                                    img = draw_text_image_v2(text, font_path, font_size=font_size, color=color, scale=1, font_weight=font_weight)
                                    img_byte_arr = io.BytesIO()
                                    img.save(img_byte_arr, format='PNG')
                                    left_x = x - img.width // 2
                                    rect = fitz.Rect(left_x, current_y, left_x + img.width, current_y + img.height)
                                    page.insert_image(rect, stream=img_byte_arr.getvalue())
                                    current_y += img.height
                else:
                    # fallback to old logic
                    sigs_sorted = sorted(sigs, key=lambda s: 0 if s['type'] == 'text' else 1)
                    for sig in sigs_sorted:
                        if sig['type'] == 'text':
                            text = to_thai_digits(sig.get('text', ''))
                            font_size = 20 if sig.get('type') == 'comment' else DEFAULT_COMMENT_FONT_SIZE
                            font_weight = "bold" if sig.get('type') == 'comment' else "regular"
                            orig_color = sig.get('color', (2, 53, 139))
                            if isinstance(orig_color, (list, tuple)):
                                r = min(int(orig_color[0]*0.8), 255)
                                g = min(int(orig_color[1]*0.8), 255)
                                b = min(int(orig_color[2]*0.8), 255)
                                color = (r, g, b)
                            else:
                                color = (2, 53, 139)
                            img = draw_text_image_v2(text, font_path, font_size=font_size, color=color, scale=1, font_weight=font_weight)
                            img_byte_arr = io.BytesIO()
                            img.save(img_byte_arr, format='PNG')
                            left_x = x - img.width // 2
                            rect = fitz.Rect(left_x, current_y, left_x + img.width, current_y + img.height)
                            page.insert_image(rect, stream=img_byte_arr.getvalue())
                            current_y += img.height
                        elif sig['type'] == 'image':
                            file_key = sig['file_key']
                            if file_key in request.files:
                                signature_file = request.files[file_key]
                                img = Image.open(signature_file)
                                fixed_height = DEFAULT_SIGNATURE_HEIGHT
                                ratio = fixed_height / img.height
                                new_width = int(img.width * ratio)
                                img = img.resize((new_width, fixed_height), resample=Image.LANCZOS)
                                img_byte_arr = io.BytesIO()
                                img.save(img_byte_arr, format='PNG')
                                left_x = x - new_width // 2
                                rect = fitz.Rect(left_x, current_y, left_x + new_width, current_y + fixed_height)
                                page.insert_image(rect, stream=img_byte_arr.getvalue())
                                current_y += fixed_height

        # แยกลายเซ็นตามประเภท PDF
        main_sigs = {(page, x, y): sigs for (pdf_type, page, x, y), sigs in sig_dict.items() if pdf_type == 'main'}
        attachment_sigs = {(page, x, y): sigs for (pdf_type, page, x, y), sigs in sig_dict.items() if pdf_type == 'attachment'}

        # วาดลายเซ็นลง PDF หลัก
        if main_sigs:
            process_signatures_on_pdf(main_pdf, main_sigs)

        # วาดลายเซ็นลงเอกสารแนบ (ถ้ามี)
        if attachment_pdf and attachment_sigs:
            process_signatures_on_pdf(attachment_pdf, attachment_sigs)

        # รวม PDF ทั้งหมดเป็นไฟล์เดียว
        final_pdf = fitz.open()
        
        # เพิ่มหน้าจาก main PDF
        final_pdf.insert_pdf(main_pdf)
        
        # เพิ่มหน้าจาก attachment PDF (ถ้ามี)
        if attachment_pdf:
            final_pdf.insert_pdf(attachment_pdf)

        # บันทึก PDF ที่มีลายเซ็นแล้ว
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as final_pdf_file:
            final_pdf.save(final_pdf_file.name)
        
        # ปิด PDF ทั้งหมด
        main_pdf.close()
        if attachment_pdf:
            attachment_pdf.close()
        final_pdf.close()
        
        # ลบไฟล์ชั่วคราว
        os.unlink(tmp_pdf)
        
        return send_file(final_pdf_file.name, mimetype="application/pdf", as_attachment=True, download_name="signed_memo.pdf")
        
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@app.route('/PDFmerge', methods=['POST'])
def merge_pdfs():
    """รวมไฟล์ PDF 2 ไฟล์เป็นไฟล์เดียว"""
    try:
        # ตรวจสอบไฟล์ที่ส่งมา
        if 'pdf1' not in request.files:
            return jsonify({'error': 'No pdf1 file uploaded'}), 400
        if 'pdf2' not in request.files:
            return jsonify({'error': 'No pdf2 file uploaded'}), 400
        
        pdf1_file = request.files['pdf1']
        pdf2_file = request.files['pdf2']
        
        # อ่านไฟล์ PDF เป็น bytes (blob)
        pdf1_bytes = pdf1_file.read()
        pdf2_bytes = pdf2_file.read()
        
        # เปิดไฟล์ PDF จาก bytes
        pdf1 = fitz.open(stream=pdf1_bytes, filetype="pdf")
        pdf2 = fitz.open(stream=pdf2_bytes, filetype="pdf")
        
        # สร้าง PDF ใหม่สำหรับรวมไฟล์
        merged_pdf = fitz.open()
        
        # เพิ่มหน้าจาก PDF แรก
        merged_pdf.insert_pdf(pdf1)
        
        # เพิ่มหน้าจาก PDF ที่สอง
        merged_pdf.insert_pdf(pdf2)
        
        # ปิดไฟล์ต้นฉบับ
        pdf1.close()
        pdf2.close()
        
        # บันทึกไฟล์ที่รวมแล้ว
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as merged_file:
            merged_pdf.save(merged_file.name)
        
        merged_pdf.close()
        
        # ส่งไฟล์กลับ
        return send_file(merged_file.name, mimetype="application/pdf", as_attachment=True, download_name="merged.pdf")
        
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/receive_num', methods=['POST'])
def receive_num():
    """
    multipart/form-data:
      - pdf: ไฟล์ PDF  
      - payload: JSON string:
        {
          "page": 0,
          "color": [2,53,139],
          "register_no": "2567/506",
          "date": "20 ก.ย. 67",
          "time": "10.30 น.",
          "receiver": "ดวงดี"
        }
    หมายเหตุ: ตรายางจะวาดที่มุมขวาบนแบบ fix ไม่ต้องส่ง x,y
    """
    print("[DEBUG] /receive_num API called")
    try:
        if 'pdf' not in request.files:
            return jsonify({'error': 'No PDF file uploaded'}), 400
        if 'payload' not in request.form:
            return jsonify({'error': 'No payload'}), 400

        p = json.loads(request.form['payload'])
        print(f"[DEBUG] Payload received: {p}")
        page_no = int(p.get('page', 0))
        color = tuple(p.get('color', [2,53,139]))
        print(f"[DEBUG] Page: {page_no}, color: {color}")

        # เปิด PDF
        pdf_bytes = request.files['pdf'].read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if page_no >= len(doc):
            return jsonify({'error': 'Page out of range'}), 400
        page = doc[page_no]

        # ฟอนต์
        font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew.ttf")
        bold_font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew Bold.ttf")
        print(f"[DEBUG] Font paths: {font_path}, {bold_font_path}")
        print(f"[DEBUG] Font exists: regular={os.path.isfile(font_path)}, bold={os.path.isfile(bold_font_path)}")
        if not os.path.isfile(font_path) or not os.path.isfile(bold_font_path):
            return jsonify({'error': 'THSarabunNew fonts not found'}), 500


        # Fix ตำแหน่งที่มุมขวาบนของกระดาษ
        page_w = page.rect.width
        page_h = page.rect.height
        
        # คำนวณตำแหน่งมุมขวาบน (เว้นขอบเล็กน้อย)
        margin = 20  # เว้นขอบ 20 pixel
        frame_width = 200
        frame_height = 80
        
        # ตำแหน่งกึ่งกลางตรายาง = มุมขวาบน - ขอบ - ครึ่งตรายาง
        center_x = page_w - margin - frame_width//2
        center_y = margin + frame_height//2
        
        print(f"[DEBUG] Page size: {page_w}x{page_h}")
        print(f"[DEBUG] Fixed stamp position: center_x={center_x}, center_y={center_y}")
        
        # วาดกรอบสี่เหลี่ยมสีน้ำเงิน (เหมือนตรายาง)
        box_left = center_x - frame_width//2
        box_top = center_y - frame_height//2
        box_right = center_x + frame_width//2
        box_bottom = center_y + frame_height//2
        
        box_rect = fitz.Rect(box_left, box_top, box_right, box_bottom)
        # ใช้สีเดียวกับฟอนต์ (สีน้ำเงินอ่อนลง)
        box_color = (color[0]/255, color[1]/255, color[2]/255)  # แปลง RGB เป็น 0-1
        page.draw_rect(box_rect, color=box_color, width=2)  # กรอบสีเดียวกับฟอนต์ หนา 1px
        print(f"[DEBUG] Drew blue frame at {box_rect} with color {box_color}")

        # *** ลบ test text ออก และใช้ตัวอย่างง่ายๆ ***
        
        # เส้นหัวข้อกรอบตรา 4 บรรทัด (หนา) - commented out for testing
        """
        header_lines = [
            "ศูนย์การศึกษาพิเศษ เขตการศึกษา ๖ จ.ลพบุรี",
            "เลขทะเบียนรับที่ ..........",
            "วันที่ ........../............/............ เวลา ..........น.",
            "ผู้รับ ........................"
        ]
        gap = int(bh/4)  # ระยะห่างแต่ละบรรทัดในกรอบ

        start_y = center_y - ( (len(header_lines)-1) * gap // 2 )
        print(f"[DEBUG] start_y: {start_y}, gap: {gap}")
        for i, text in enumerate(header_lines):
            print(f"[DEBUG] Drawing header line {i}: {text}")
            img = draw_text_img(text, size=16, bold=True)
            y_pos = start_y + i*gap
            print(f"[DEBUG] Position: center_x={center_x}, y={y_pos}")
            paste_center(img, center_x, y_pos)
        """

        # วาดข้อมูลตรา - ใช้ PyMuPDF text แทน PIL
        register_no = p.get('register_no','')
        date_text = p.get('date','')
        time_text = p.get('time','')
        receiver_text = p.get('receiver','')
        
        print(f"[DEBUG] Data to insert: register_no='{register_no}', date='{date_text}', time='{time_text}', receiver='{receiver_text}'")
        
        # ใช้วิธีเดียวกับ endpoint อื่น - draw_text_image + insert_image
        def draw_text_img(text, size=18, bold=False):
            fp = bold_font_path if bold else font_path
            print(f"[DEBUG] Creating text image: '{text}', size={size}, bold={bold}, font={fp}")
            img = draw_text_image(to_thai_digits(text), fp, size, color, scale=1)
            print(f"[DEBUG] Text image created: {img.width}x{img.height}")
            return img

        def paste_center(img, center_x, center_y):
            left = center_x - img.width//2
            top  = center_y - img.height//2
            rect = fitz.Rect(left, top, left+img.width, top+img.height)
            print(f"[DEBUG] Pasting image at rect: {rect}, image size: {img.width}x{img.height}")
            bio = io.BytesIO(); img.save(bio, format='PNG')
            page.insert_image(rect, stream=bio.getvalue())
            print(f"[DEBUG] Image inserted successfully")
        
        # เส้นหัวข้อกรอบตรา 4 บรรทัด (หนา)
        header_lines = [
            "ศูนย์การศึกษาพิเศษ เขตการศึกษา ๖ จ.ลพบุรี",
            f"เลขทะเบียนรับที่ {register_no}",
            f"วันที่ {date_text} เวลา {time_text}",
            f"ผู้รับ {receiver_text}"
        ]
        gap = 16  # ระยะห่างแต่ละบรรทัดใหม่ (แทนที่ bh/4 ที่มากเกินไป)

        start_y = center_y - ( (len(header_lines)-1) * gap // 2 )
        print(f"[DEBUG] start_y: {start_y}, gap: {gap}")
        for i, text in enumerate(header_lines):
            print(f"[DEBUG] Drawing header line {i}: {text}")
            img = draw_text_img(text, size=16, bold=True)
            y_pos = start_y + i*gap
            print(f"[DEBUG] Position: center_x={center_x}, y={y_pos}")
            paste_center(img, center_x, y_pos)

        # ส่งไฟล์กลับ
        print("[DEBUG] Saving final PDF...")
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as outpdf:
            doc.save(outpdf.name)
        doc.close()
        print(f"[DEBUG] PDF saved, sending response...")
        
        # เพิ่ม debug response header
        response = send_file(outpdf.name, mimetype="application/pdf", as_attachment=True, download_name="receive_num.pdf")
        response.headers['X-Debug'] = 'receive_num_processed'
        return response

    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/stamp_summary', methods=['POST'])
def stamp_summary():
    """
    multipart/form-data:
      - pdf: ไฟล์ PDF ต้นฉบับ
      - sign_png: ไฟล์ลายเซ็นธุรการ (PNG โปร่งใส)
      - payload: JSON string:
        {
          "summary": "เรื่อง การขออนุมัติโครงการ",
          "group_name": "กลุ่มวิชาการ", 
          "receiver_name": "นายสมชาย รับผิดชอบ",
          "date": "25 ก.ย. 67"
        }
    
    ตราจะวาดที่มุมซ้ายล่างของกระดาษ
    """
    print("[DEBUG] /stamp_summary API called")
    try:
        # ตรวจสอบไฟล์และข้อมูลที่ส่งมา
        if 'pdf' not in request.files:
            return jsonify({'error': 'No PDF file uploaded'}), 400
        if 'sign_png' not in request.files:
            return jsonify({'error': 'No signature PNG file uploaded'}), 400
        if 'payload' not in request.form:
            return jsonify({'error': 'No payload'}), 400

        # อ่านข้อมูล
        pdf_file = request.files['pdf']
        sign_file = request.files['sign_png']
        
        p = json.loads(request.form['payload'])
        summary = p.get('summary', '')
        group_name = p.get('group_name', '') 
        receiver_name = p.get('receiver_name', '')
        date = p.get('date', '')
        
        print(f"[DEBUG] Data: summary={summary}, group={group_name}, receiver={receiver_name}, date={date}")

        # เปิด PDF
        pdf_bytes = pdf_file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]  # ใช้หน้าแรก

        # ฟอนต์ไทย
        font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew.ttf")
        bold_font_path = os.path.join(os.path.dirname(__file__), "fonts", "THSarabunNew Bold.ttf")
        if not os.path.isfile(font_path) or not os.path.isfile(bold_font_path):
            return jsonify({'error': 'THSarabunNew fonts not found'}), 500

        # คำนวณตำแหน่งมุมซ้ายล่าง
        page_w = page.rect.width
        page_h = page.rect.height
        margin = 30
        stamp_width = 200
        stamp_height = 140  # เพิ่มความสูงจาก 120 เป็น 140
        
        # ตำแหน่งกึ่งกลางตรา = มุมซ้ายล่าง + ขอบ + ครึ่งตรา
        center_x = margin + stamp_width//2
        center_y = page_h - margin - stamp_height//2
        
        print(f"[DEBUG] Page size: {page_w}x{page_h}")
        print(f"[DEBUG] Stamp position: center_x={center_x}, center_y={center_y}")

        # วาดกรอบตรา
        box_left = center_x - stamp_width//2
        box_top = center_y - stamp_height//2  
        box_right = center_x + stamp_width//2
        box_bottom = center_y + stamp_height//2
        
        box_rect = fitz.Rect(box_left, box_top, box_right, box_bottom)
        box_color = (2/255, 53/255, 139/255)  # สีน้ำเงิน
        page.draw_rect(box_rect, color=box_color, width=2)

        # สร้างฟังก์ชันวาดข้อความ
        def draw_text_img(text, size=16, bold=False):
            fp = bold_font_path if bold else font_path
            color_rgb = (2, 53, 139)  # สีน้ำเงิน
            img = draw_text_image(to_thai_digits(text), fp, size, color_rgb, scale=1)
            return img

        def paste_at_position(img, x, y):
            rect = fitz.Rect(x, y, x+img.width, y+img.height)
            bio = io.BytesIO(); img.save(bio, format='PNG')
            page.insert_image(rect, stream=bio.getvalue())

        # วาดข้อความในตรา
        font_size = 16
        first_line_spacing = font_size  # บรรทัดแรก = ขนาดฟอนต์ (16)
        other_line_spacing = font_size - 4  # บรรทัดอื่น = ขนาดฟอนต์ - 4 (12)
        current_y = box_top + 8  # เริ่มใกล้ขอบบนของกรอบมากขึ้น
        
        # บรรทัดที่ 1: เรียน ผอ. ศกศ.เขต ๖ จ.ลพบุรี
        text1 = "เรียน ผอ. ศกศ.เขต ๖ จ.ลพบุรี"
        img1 = draw_text_img(text1, size=font_size, bold=True)
        paste_at_position(img1, box_left + 10, current_y)
        current_y += first_line_spacing  # ใช้ระยะห่างบรรทัดแรก
        
        # บรรทัดที่ 2-3: summary (อาจมี 1-2 บรรทัด)
        summary_lines = summary.split('\n')
        for line in summary_lines:
            if line.strip():  # ถ้าไม่ใช่บรรทัดว่าง
                img_summary = draw_text_img(line, size=font_size, bold=False)
                paste_at_position(img_summary, box_left + 10, current_y)
            current_y += other_line_spacing  # ใช้ระยะห่างบรรทัดอื่น
        
        current_y += 2  # เว้นบรรทัดเล็กน้อย
        
        # บรรทัดมอบหมาย
        assign_text = f"เห็นควรมอบ {group_name}"
        img_assign = draw_text_img(assign_text, size=font_size, bold=False)
        paste_at_position(img_assign, box_left + 10, current_y)
        current_y += other_line_spacing + 5  # ลดระยะห่างก่อนลายเซ็น
        
        # ลายเซ็น
        sign_img = Image.open(sign_file)
        # ปรับขนาดลายเซ็น
        sign_height = 30  # กลับเป็น 30
        ratio = sign_height / sign_img.height
        sign_width = int(sign_img.width * ratio)
        sign_img = sign_img.resize((sign_width, sign_height), Image.LANCZOS)
        
        # คำนวณตำแหน่งกึ่งกลางของกรอบ
        center_x_frame = box_left + stamp_width//2
        
        # ข้อความลงชื่อ และลายเซ็น (แบบต่อเนื่องกัน)
        sign_text = "ลงชื่อ"
        img_sign_text = draw_text_img(sign_text, size=font_size, bold=False)
        
        # คำนวณตำแหน่งเริ่มต้นให้อยู่กึ่งกลาง
        total_width = img_sign_text.width + 5 + sign_width  # ความกว้างรวม (ข้อความ + gap + ลายเซ็น)
        start_x = center_x_frame - total_width//2
        
        sign_y = current_y
        # วาง "ลงชื่อ" ก่อน
        paste_at_position(img_sign_text, start_x, sign_y)
        # วางลายเซ็นติดข้าง
        paste_at_position(sign_img, start_x + img_sign_text.width + 5, sign_y)
        
        current_y += 15  # ลดระยะห่างหลังลายเซ็น
        
        # ผู้รับ (กึ่งกลาง)
        receiver_text = f"ผู้รับ  {receiver_name}"
        img_receiver = draw_text_img(receiver_text, size=font_size, bold=False)
        paste_at_position(img_receiver, center_x_frame - img_receiver.width//2, current_y)
        current_y += other_line_spacing
        
        # วันที่ (กึ่งกลาง)
        date_text = f"วันที่ {date}"
        img_date = draw_text_img(date_text, size=font_size, bold=False)
        paste_at_position(img_date, center_x_frame - img_date.width//2, current_y)

        # ส่งไฟล์กลับ
        print("[DEBUG] Saving final PDF...")
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as outpdf:
            doc.save(outpdf.name)
            doc.close()
            print(f"[DEBUG] PDF saved, sending response...")
            
            response = send_file(outpdf.name, mimetype="application/pdf", as_attachment=True, download_name="summary_stamped.pdf")
            response.headers['X-Debug'] = 'stamp_summary_processed'
            return response

    except Exception as e:
        print(f"[ERROR] {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

if __name__ == "__main__":
    # สำหรับ Railway ต้องฟังที่ 0.0.0.0
    app.run(debug=True, host="0.0.0.0", port=5000)