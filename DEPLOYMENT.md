# Deployment & Failover Guide

API service ตัวนี้ deploy ขนานกันบน 2 platform เพื่อความเสถียร  
ถ้า platform หลักล่ม สามารถสลับมาใช้ backup ได้ทันทีโดยไม่ต้อง redeploy

---

## URL ของ API

| Platform | บทบาท | Base URL |
|---|---|---|
| **Railway** | Primary (ใช้ปกติ) | `https://pdf-memo-docx-production-25de.up.railway.app` |
| **Fly.io** | Backup (สำรอง) | `https://pdf-memo-docx-backup.fly.dev` |

ทั้งสอง URL รัน code เดียวกัน endpoints เหมือนกันทุก route  
เปลี่ยนแค่ **base URL** ฝั่ง frontend ก็ใช้งานได้ปกติ

---

## วิธีให้ Frontend ปรับระบบ

### แนวทาง A — ใช้ ENV var (แนะนำ)

ใน frontend project ใส่ `.env`:

```env
# ใช้ตัวนี้เป็น default
VITE_API_BASE_URL=https://pdf-memo-docx-production-25de.up.railway.app

# Backup เผื่อ Railway ล่ม — เปลี่ยน comment กับบรรทัดบนเมื่อต้องสลับ
# VITE_API_BASE_URL=https://pdf-memo-docx-backup.fly.dev
```

ในโค้ดเรียก API ผ่านตัวแปร:

```ts
const API_BASE = import.meta.env.VITE_API_BASE_URL;

await fetch(`${API_BASE}/add_signature_v2`, { ... });
```

**เมื่อ Railway ล่ม:**
1. แก้ `.env` → uncomment บรรทัด backup, comment บรรทัด primary
2. Rebuild + redeploy frontend
3. ใช้งานได้ต่อทันที

(สำหรับ Next.js เปลี่ยน prefix เป็น `NEXT_PUBLIC_API_BASE_URL`,  
สำหรับ Create React App เปลี่ยนเป็น `REACT_APP_API_BASE_URL`)

---

### แนวทาง B — Auto-failover ในโค้ด

ถ้าไม่อยากต้อง redeploy frontend ทุกครั้งที่ Railway ล่ม ทำ failover ในโค้ดได้

```ts
const API_HOSTS = [
  "https://pdf-memo-docx-production-25de.up.railway.app",
  "https://pdf-memo-docx-backup.fly.dev",
];

async function apiFetch(path: string, options?: RequestInit) {
  let lastError: unknown;
  for (const base of API_HOSTS) {
    try {
      const res = await fetch(`${base}${path}`, {
        ...options,
        signal: AbortSignal.timeout(10000), // 10s timeout
      });
      if (res.ok || (res.status >= 400 && res.status < 500)) {
        // 4xx ก็ถือว่า server ตอบแล้ว ไม่ต้อง failover
        return res;
      }
      lastError = new Error(`${base} returned ${res.status}`);
    } catch (err) {
      lastError = err;
      // ลอง host ต่อไป
    }
  }
  throw lastError;
}

// ใช้งาน
await apiFetch("/add_signature_v2", {
  method: "POST",
  body: formData,
});
```

> ⚠️ **ข้อควรระวัง:** วิธีนี้ทำให้ request แรกหลัง Railway ล่มช้าขึ้นนิด (รอ timeout 10s แล้วค่อยลอง Fly)  
> ถ้าอยากให้เร็ว เก็บสถานะ "primary down" ใน memory/localStorage ไว้ 5 นาทีแล้วข้าม Railway เลย

---

## วิธีเช็คว่า platform ไหนยังใช้งานได้

```bash
# Railway
curl -I https://pdf-memo-docx-production-25de.up.railway.app/

# Fly.io
curl -I https://pdf-memo-docx-backup.fly.dev/
```

- **HTTP 200 หรือ 404 (Flask 404):** ✅ ใช้งานได้ปกติ (404 = ไม่มี route ที่ `/` ซึ่ง normal)
- **HTTP 404 + body `"Application not found"`:** ❌ Container ไม่รัน
- **Timeout / connection refused:** ❌ Platform ล่ม

---

## วิธี Deploy

### Auto (เมื่อ push เข้า `main`)

มี GitHub Actions ตั้งไว้แล้วที่ `.github/workflows/deploy.yml`  
ทุกครั้งที่ push เข้า branch `main` มันจะ deploy ขึ้นทั้ง 2 platform พร้อมกัน

**ต้องตั้ง GitHub Secrets 2 ตัว** (ทำครั้งเดียว):

1. ไปที่ `https://github.com/watcharapon7574/pdf-memo-docx/settings/secrets/actions`
2. กด **New repository secret** เพิ่ม 2 secret ตามนี้

   | Secret name | ค่า |
   |---|---|
   | `RAILWAY_TOKEN` | ดูวิธีหาด้านล่าง ↓ |
   | `FLY_API_TOKEN` | ดูวิธีหาด้านล่าง ↓ |

**วิธีหา Railway token:**
- ไปที่ `https://railway.com/project/abefb47a-8090-49b0-9c7e-13191b7549f3/settings/tokens`
- กด **Create Token** → ตั้งชื่อเช่น `github-actions` → เลือก environment `production`
- Copy ค่ามาใส่ใน GitHub Secret ชื่อ `RAILWAY_TOKEN`

**วิธีหา Fly token:**
- รันคำสั่งบนเครื่อง local: `flyctl tokens create deploy -a pdf-memo-docx-backup --name github-actions-deploy`
- Copy ทั้งบรรทัดที่ขึ้นต้นด้วย `FlyV1 ...` มาใส่ใน GitHub Secret ชื่อ `FLY_API_TOKEN`

### Manual (ถ้าไม่อยาก push)

```bash
# Deploy Railway
railway up

# Deploy Fly.io
fly deploy --remote-only
```

---

## ค่าใช้จ่ายโดยประมาณ

| Platform | Plan | ค่าใช้จ่าย/เดือน |
|---|---|---|
| Railway | Hobby | $5 (includes $5 usage credit) |
| Fly.io | Pay-as-you-go | ~$0–2 (scale-to-zero เมื่อ idle) |

Fly.io ตั้ง auto-stop machine ไว้ — ตอนไม่มี request จะ machine จะหยุดเอง  
**Cold start ~1-2 วินาที** ใน request แรกหลัง idle นาน
