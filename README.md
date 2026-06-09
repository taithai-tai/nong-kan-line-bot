# LINE Official Account Bot: น้องก้าน

โปรเจกต์นี้เป็น LINE Official Account Bot สำหรับบริษัท **น้องก้าน** ใช้ตอบคำถามลูกค้าอัตโนมัติผ่าน LINE ด้วย Python, Flask, LINE Messaging API และ OpenRouter

บอตนี้ถูกออกแบบให้ตอบจากฐานความรู้บริษัทเป็นหลัก ไม่เดาข้อมูล และหากไม่พบคำตอบจะส่งต่อให้เจ้าหน้าที่

## โครงสร้างโปรเจกต์

```text
.
├── app.py
├── requirements.txt
├── Procfile
├── railway.json
├── runtime.txt
├── templates/
├── knowledge_base.json
├── training_history.json
├── customer_chats.json
├── customer_ai_settings.json
├── .env.example
├── .gitignore
└── README.md
```

## สิ่งที่ต้องเตรียม

- LINE Official Account
- LINE Developers Messaging API channel
- OpenRouter API key
- GitHub account
- Railway account

## ตั้งค่า Local

1. สร้าง virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. ติดตั้ง dependencies

```bash
pip install -r requirements.txt
```

3. สร้างไฟล์ `.env`

```bash
cp .env.example .env
```

4. ใส่ค่าใน `.env`

```env
LINE_CHANNEL_SECRET=your_line_channel_secret
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token
OPENROUTER_API_KEY=your_openrouter_api_key
AI_API_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=openrouter/auto
DATABASE_URL=
APP_URL=http://localhost:5000
DATA_DIR=.
SQLITE_DATABASE_PATH=nong_kan.sqlite3
TRAINING_HISTORY_PATH=training_history.json
CUSTOMER_CHATS_PATH=customer_chats.json
CUSTOMER_AI_SETTINGS_PATH=customer_ai_settings.json
RESPONSE_TEMPLATES_PATH=response_templates.json
ADMIN_PASSWORD=change_this_to_a_strong_password
SECRET_KEY=change_this_to_a_long_random_secret
```

5. แก้ `knowledge_base.json`

กรอกข้อมูลจริงของบริษัท เช่น สินค้า บริการ ราคา โปรโมชัน วิธีสั่งซื้อ การจัดส่ง และช่องทางติดต่อเจ้าหน้าที่

สำคัญ: ถ้ายังเป็นข้อความตัวอย่าง บอตจะถือว่ายังไม่มีข้อมูลและจะส่งต่อให้เจ้าหน้าที่

6. รัน local server

```bash
python app.py
```

เช็ก health check:

```bash
curl http://localhost:5000/
```

ควรได้ผลลัพธ์ประมาณนี้:

```json
{
  "bot_name": "น้องก้าน",
  "service": "nong-kan-line-bot",
  "status": "ok"
}
```

เข้าไปแก้ฐานความรู้ผ่านหน้าเว็บได้ที่:

```text
http://localhost:5000/admin
```

ใช้รหัสผ่านจาก `ADMIN_PASSWORD`

## ตั้งค่า LINE Messaging API

1. เข้า LINE Developers Console
2. สร้าง Provider หรือเลือก Provider ที่มีอยู่
3. สร้าง Messaging API channel
4. ไปที่แท็บ Messaging API
5. คัดลอก `Channel secret` ไปใส่ใน `LINE_CHANNEL_SECRET`
6. สร้าง long-lived `Channel access token` แล้วใส่ใน `LINE_CHANNEL_ACCESS_TOKEN`
7. ปิด Auto-reply message ใน LINE Official Account Manager หากไม่ต้องการให้ข้อความอัตโนมัติเดิมชนกับบอต
8. เปิด Use webhook

## Deploy บน Railway

1. สร้าง Git repo

```bash
git init
git add .
git commit -m "Initial LINE OA bot"
```

2. สร้าง repository บน GitHub แล้ว push

```bash
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git branch -M main
git push -u origin main
```

3. เข้า Railway
4. เลือก New Project
5. เลือก Deploy from GitHub repo
6. เลือก repo ของโปรเจกต์นี้
7. ตั้ง Environment Variables ใน Railway:

```env
LINE_CHANNEL_SECRET=your_line_channel_secret
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token
OPENROUTER_API_KEY=your_openrouter_api_key
AI_API_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=openrouter/auto
APP_NAME=nong-kan-line-bot
APP_URL=https://your-railway-domain.up.railway.app
DATABASE_URL=${{Postgres.DATABASE_URL}}
DATA_DIR=/data
ADMIN_PASSWORD=change_this_to_a_strong_password
SECRET_KEY=change_this_to_a_long_random_secret
AI_TIMEOUT_SECONDS=20
LOG_LEVEL=INFO
```

8. สร้าง Railway PostgreSQL แล้วตั้ง `DATABASE_URL` ให้ web service
9. สร้าง Railway Volume แล้ว mount ที่ `/data` เพื่อ migrate ข้อมูล JSON เดิมเข้า database ได้ใน deploy แรก
10. Deploy
11. เปิด public domain ของ Railway
12. ทดสอบ health check ที่:

```text
https://your-railway-domain.up.railway.app/
```

เข้า Admin หลังบ้านได้ที่:

```text
https://your-railway-domain.up.railway.app/admin
```

## หน้า Admin สำหรับปรับแต่งข้อมูล

หน้า `/admin` ใช้สำหรับ:

- แก้ `knowledge_base.json` ผ่านเว็บ
- ตรวจสถานะ LINE keys และ OpenRouter key
- ดูโมเดล AI ที่ใช้อยู่
- ทดสอบถามน้องก้านก่อนให้ลูกค้าใช้งานจริง

ต้องตั้งค่า `ADMIN_PASSWORD` ก่อนเสมอ ไม่อย่างนั้นหน้า Admin จะไม่เปิดใช้งาน

ค่า secrets เช่น `LINE_CHANNEL_SECRET`, `LINE_CHANNEL_ACCESS_TOKEN`, `OPENROUTER_API_KEY`, `AI_MODEL` และ `SECRET_KEY` ให้แก้ใน Railway Environment Variables ไม่ควรแก้ผ่านหน้าเว็บ

หมายเหตุ: เวอร์ชันใหม่ใช้ PostgreSQL เป็นหลักผ่าน `DATABASE_URL` เพื่อกันข้อมูลแชทเขียนทับกันและกันข้อมูลหายตอน deploy ระบบยังอ่าน JSON เดิมใน `/data` เพื่อ migrate ครั้งแรกได้

## ตั้ง Webhook URL ใน LINE

นำ domain จาก Railway ไปตั้งเป็น Webhook URL:

```text
https://your-railway-domain.up.railway.app/callback
```

จากนั้นกด Verify ใน LINE Developers Console

ถ้าผ่าน ให้ลองส่งข้อความหา LINE Official Account

## การเปลี่ยนโมเดล AI

แก้ Environment Variable นี้ใน Railway:

```env
AI_MODEL=openrouter/auto
```

ตัวอย่าง:

```env
AI_MODEL=anthropic/claude-3.5-sonnet
AI_MODEL=openai/gpt-4o-mini
AI_MODEL=google/gemini-flash-1.5
```

ชื่อโมเดลต้องเป็นชื่อที่ OpenRouter รองรับ

## พฤติกรรมของบอต

- ตอบภาษาไทยสุภาพ เป็นกันเอง
- ตอบสั้น กระชับ อ่านง่าย
- ตอบเฉพาะข้อมูลที่มีใน `knowledge_base.json`
- ไม่เดาราคา โปรโมชัน เงื่อนไข หรือข้อมูลบริษัท
- ถ้าไม่มีข้อมูล จะตอบว่า:

```text
ขออภัยค่ะ น้องก้านยังไม่มีข้อมูลเรื่องนี้ในระบบ จะส่งต่อให้เจ้าหน้าที่ติดต่อกลับนะคะ
```

- ถ้า AI API ใช้งานไม่ได้หรือ key ไม่ถูกต้อง จะตอบว่า:

```text
ขอโทษนะคะ ตอนนี้น้องก้านมึนหัวอยู่ น้องก้านจะกลับมาตอบตอนมีสติแล้วนะคะ
```

- หน้า `แชทลูกค้า` มีปุ่มปิด AI ทั้งระบบ ถ้าปิดไว้และลูกค้าทักมา ระบบจะตอบข้อความมึนหัวให้อัตโนมัติ
- หน้า `แชทลูกค้า` ดึงข้อมูลใหม่เองแบบเรียลไทม์ และแอดมินส่งข้อความได้โดยไม่ต้องรีเฟรชหน้า
- แชทที่ปิด AI รายลูกค้าจะถูกปักหมุดไว้บนสุด และในกลุ่มเดียวกันจะเรียงแชทใหม่สุดก่อน
- ช่องค้นหาในหน้า `แชทลูกค้า` ค้นได้ทั้ง ID ลูกค้าและข้อความที่อยู่ในประวัติแชท
- แอดมินตอบลูกค้าสำเร็จแล้ว ระบบจะปิด AI ของแชทนั้นให้อัตโนมัติ
- เพิ่ม/ลบเท็มเพลตตอบลูกค้าได้จากหน้า `แชทลูกค้า`; เมื่อกดใช้ เท็มเพลตจะถูกใส่ในช่องพิมพ์แต่ยังไม่ส่งทันที
- ข้อความอัตโนมัติเวลา AI ออฟไลน์แก้ได้จากหน้า `แชทลูกค้า`
- ข้อมูลแชท ประวัติเทรน เท็มเพลต ฐานความรู้ และการตั้งค่าเก็บใน PostgreSQL เมื่อมี `DATABASE_URL`
- หน้าแชทใช้ WebSocket ผ่าน Socket.IO เพื่ออัปเดตทันทีเมื่อมีข้อความหรือการตั้งค่าเปลี่ยน โดยไม่ใช้ polling loop
- มี Broadcast สำหรับส่งข้อความหาลูกค้าทั้งหมด ตามคำค้นหา หรือระบุ ID เอง
- มีสถิติภาพรวม เช่น ลูกค้าทั้งหมด ผู้ใช้งานวันนี้ ข้อความวันนี้ เวลาเฉลี่ยในการตอบ และอัตราบอตตอบจบ
- หน้าแชทมี React/Tailwind shell สำหรับการ์ดเมนูไอคอน 3D แบบไม่มีข้อความอธิบาย

## การทดสอบสำคัญ

- `GET /` ต้องตอบสถานะ `ok`
- `POST /callback` ที่ signature ไม่ถูกต้องต้องถูกปฏิเสธ
- คำถามที่อยู่ในฐานความรู้ต้องตอบตามข้อมูลบริษัท
- คำถามที่ไม่มีในฐานความรู้ต้องส่งต่อเจ้าหน้าที่
- ถ้า OpenRouter API key ผิด บอตต้องไม่ล่ม และต้องตอบข้อความน้องก้านมึนหัว

## หมายเหตุด้านความปลอดภัย

- ห้าม commit ไฟล์ `.env`
- เก็บ API keys ใน Railway Environment Variables เท่านั้น
- ตั้ง `ADMIN_PASSWORD` และ `SECRET_KEY` เป็นค่าที่เดายากก่อน deploy จริง
- LINE webhook ต้องตรวจ `X-Line-Signature` ทุกครั้งก่อนประมวลผล
- อย่าใส่ข้อมูลส่วนตัวลูกค้าไว้ใน `knowledge_base.json`
