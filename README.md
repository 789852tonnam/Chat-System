# Socket Chat

โครงงาน Computer Network: แอปแชต Python/Tkinter เชื่อม Server ผ่าน TCP Socket มีแชตส่วนตัว กลุ่ม และฟีเจอร์พิเศษสี่รายการ

**สถานะปัจจุบัน:** Server transport/ping ใช้งานได้แล้ว ผ่าน 29 tests รวม TCP loopback ยังไม่มีระบบบัญชี/แชต, Tkinter Client, SQL schema หรือ Docker image

## รัน Server ขั้นแรก

ใช้ Python 3.11 ขึ้นไปและไม่ต้องติดตั้ง package เพิ่ม จากโฟลเดอร์ socket-chat:

```bash
python3 server.py --host 127.0.0.1 --port 5000
```

ดู [Server guide](docs/server.md) สำหรับการลอง ping, ใช้งาน LAN และคำอธิบาย Thread/Queue; กด Ctrl-C เพื่อปิด

```bash
python3 -m unittest discover -s tests -v
```

## เริ่มอ่านที่นี่

| เอกสาร | ใช้ทำอะไร |
| --- | --- |
| [Requirement และเกณฑ์รับงาน](docs/requirements.md) | ตรวจ R1–R11, คะแนน, หลักฐานที่ต้องสาธิตและสิ่งที่ต้องส่ง |
| [ภาพรวมการออกแบบ](docs/design.md) | ดูข้อสรุปล่าสุดและหน้าที่ของแต่ละส่วน |
| [แผนลงมือทำ](docs/implementation-plan.md) | ทำทีละช่วง ตั้งแต่เชื่อมต่อจนสาธิตสองเครื่อง |
| [ประเด็นที่ยังไม่ตกลง](docs/open-decisions.md) | แยกข้อเสนอและรายละเอียดเปิดออกจากการตัดสินใจแล้ว |
| [Server ที่ implement แล้ว](docs/server.md) | รัน ตรวจ ping และอ่าน flow ในโค้ด |
| [คำศัพท์](GLOSSARY.md) | ใช้คำเรียกบัญชี ห้อง สมาชิก และหมุดให้ตรงกัน |

## รายละเอียดและแผนภาพ

| หัวข้อ | เอกสาร | PlantUML |
| --- | --- | --- |
| Thread, Queue และหน้าที่ Client/Server | [Design](docs/design.md) | [Architecture](diagram/architecture.puml) |
| โครงสร้างข้อมูล | [Data model](docs/data-model.md) | [Database](diagram/database.puml) |
| TCP request/response | [Protocol](docs/protocol.md) | — |
| Contract ทุกคำสั่งและตัวอย่าง JSON | [API reference](docs/api-reference.md), [Examples](docs/protocol-examples.json) | — |
| Poll, ประวัติ และ revision | [Synchronization](docs/synchronization.md) | — |
| AI, หัวใจ, หมุด, ข้อความวิ่ง | [Special features](docs/special-features.md) | — |
| OpenRouter และโมเดลฟรี | [AI](docs/ai.md) | — |
| LAN, Docker และข้อมูลถาวร | [Deployment](docs/deployment.md) | [Deployment](diagram/deployment.puml) |
| ไฟล์โจทย์ เทมเพลต และประกาศส่งงาน | [References](docs/references/README.md) | — |

## โครงสร้างปัจจุบัน

```text
socket-chat/
├── README.md
├── GLOSSARY.md
├── server.py                # Runnable transport/ping server
├── common/
│   └── protocol.py          # Shared wire-format helpers
├── tests/                   # Unit + loopback TCP integration
├── diagram/                 # Editable PlantUML sources
└── docs/
    ├── design.md
    ├── server.md
    ├── requirements.md
    ├── implementation-plan.md
    ├── open-decisions.md
    ├── data-model.md
    ├── protocol.md
    ├── api-reference.md
    ├── protocol-examples.json
    ├── synchronization.md
    ├── special-features.md
    ├── ai.md
    ├── deployment.md
    └── references/          # Source documents, unchanged copies
```

Server อยู่ใน server.py ส่วน Client จะเป็นคนละไฟล์ตามโจทย์ โค้ดปัจจุบันตอบจริงเฉพาะ ping และ error ของ transport; คำสั่งแอปที่ยังไม่ทำตอบ ACTION_NOT_IMPLEMENTED ไม่มีการอ้างว่า API v1.0 implement ครบแล้ว

เอกสารใช้สถานะ **ยืนยันแล้ว**, **ข้อเสนอ**, **ยังไม่ตกลง** แยกกัน การยืนยันการออกแบบไม่ใช่หลักฐานว่า implement หรือผ่านการทดสอบแล้ว

Protocol v1.0 และสัญญา action กำหนดโดยผู้ช่วยตามคำขอให้ตัดสินใจและจดเป็นมาตรฐานกลาง รายละเอียดที่เหลือนอก API เช่น database engine, วิธีเก็บรหัสผ่าน และ AI model selector ยังอยู่ใน Open decisions
