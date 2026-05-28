# ใช้ Python image พื้นฐาน
FROM python:3.12-slim

# ติดตั้ง dependencies ที่จำเป็น
# qpdf: ใช้ compress PDF lossless หลัง add_signature (ลด orphan objects 10-70×)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice \
        fonts-thai-tlwg \
        fontconfig \
        qpdf \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# เพิ่มฟอนต์ราชการไทย (TH Sarabun New)
COPY fonts/*.ttf /usr/share/fonts/truetype/
RUN fc-cache -fv

# กำหนด working directory
WORKDIR /app

# คัดลอก requirements.txt และติดตั้ง Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# คัดลอกโค้ดทั้งหมดเข้า image
COPY . .

# เปิด port 5000 (default ของ Flask)
EXPOSE 5000

# รัน Flask app
CMD ["python", "main.py"]