import os
import requests
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from jinja2 import Template
from typing import List
import time

# ==========================================
# 1. إعدادات السيرفر (Flask) ليعمل 24 ساعة
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "iLovePDF Bot is Running!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

# ==========================================
# 2. إعدادات iLovePDF (من ملفك)
# ==========================================
ILOVEPDF_PUBLIC_KEY = "project_public_e37dbc55b1cef99987608cd0c7a1a938_CVhdn3750fc8930cf0fbd2488cfd41d2ff309"
ILOVEPDF_SECRET_KEY = "secret_key_5ec8ca22feb71d81cc8189d33d788f27_EZlk7b435659353674fc5132a1246cc334bf0"

# ==========================================
# 3. كلاس iLovePDF (نفس كودك السابق)
# ==========================================
class ILovePdfClient:
    def __init__(self, public_key, secret_key):
        self.base_url = "https://api.ilovepdf.com/v1"
        self.public_key = public_key
        self.secret_key = secret_key
        self.token = None

    def auth(self):
        try:
            url = f"{self.base_url}/auth"
            response = requests.post(url, json={"public_key": self.public_key})
            if response.status_code == 200:
                self.token = response.json()['token']
                return True
            return False
        except: return False

    def convert_html_to_pdf(self, html_content):
        # تم تعديل الدالة لتعيد محتوى الملف (Bytes) بدلاً من حفظه على القرص
        if not self.token and not self.auth(): return None
        headers = {"Authorization": f"Bearer {self.token}"}

        # Start
        start_resp = requests.get(f"{self.base_url}/start/htmlpdf", headers=headers)
        if start_resp.status_code != 200: return None
        task_id = start_resp.json()['task']
        server = start_resp.json()['server']
        
        # Upload
        upload_url = f"https://{server}/v1/upload"
        files = {'file': ('report.html', html_content, 'text/html')}
        data = {'task': task_id}
        upload_resp = requests.post(upload_url, headers=headers, files=files, data=data)
        if upload_resp.status_code != 200: return None
        server_filename = upload_resp.json()['server_filename']

        # Process
        process_url = f"https://{server}/v1/process"
        process_data = {
            "task": task_id,
            "tool": "htmlpdf",
            "files": [{"server_filename": server_filename, "filename": "report.html"}],
            "page_size": "A4",
            "page_orientation": "portrait",
            "page_margin": 20
        }
        requests.post(process_url, headers=headers, json=process_data)

        # Download
        time.sleep(2) # انتظار بسيط
        download_url = f"https://{server}/v1/download/{task_id}"
        download_resp = requests.get(download_url, headers=headers)
        
        if download_resp.status_code == 200:
            return download_resp.content # إرجاع الملف كبيانات
        return None

# ==========================================
# 4. منطق الذكاء الاصطناعي (Gemini)
# ==========================================
class Section(BaseModel):
    title: str = Field(description="عنوان القسم")
    content: str = Field(description="المحتوى")

class AcademicReport(BaseModel):
    title: str = Field(description="عنوان التقرير")
    introduction: str = Field(description="المقدمة")
    sections: List[Section] = Field(description="الأقسام")
    conclusion: str = Field(description="الخاتمة")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
body{font-family:'Arial',sans-serif;padding:40px;direction:rtl;text-align:right}
h1{text-align:center;border-bottom:2px solid #333}
h2{color:#0066cc;margin-top:30px;border-right:4px solid #0066cc;padding-right:10px}
p{text-align:justify;line-height:1.6}
.footer{text-align:center;margin-top:50px;color:#777;font-size:12px}
</style>
</head>
<body>
<h1>{{ title }}</h1>
<div><h2>المقدمة</h2>{{ intro }}</div>
{% for section in sections %}
<div><h2>{{ section.title }}</h2>{{ section.content }}</div>
{% endfor %}
<div><h2>الخاتمة</h2>{{ conc }}</div>
<div class="footer">Created by Telegram Bot</div>
</body>
</html>
"""

def generate_report(topic):
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        llm = ChatGoogleGenerativeAI(model="models/gemini-2.5-flash", temperature=0.3, google_api_key=api_key)
        parser = PydanticOutputParser(pydantic_object=AcademicReport)
        prompt = PromptTemplate(
            input_variables=["topic"],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            template="اكتب تقريرًا أكاديميًا عن: {topic}\n{format_instructions}\nلغة عربية فصحى."
        )
        report = (prompt | llm | parser).invoke({"topic": topic})
        
        # HTML Rendering
        def clean(t): return "".join([f"<p>{p}</p>" for p in t.split('\n') if p.strip()])
        html = Template(HTML_TEMPLATE).render(
            title=report.title,
            intro=clean(report.introduction),
            sections=[{'title': s.title, 'content': clean(s.content)} for s in report.sections],
            conc=clean(report.conclusion)
        )
        
        # PDF Conversion
        client = ILovePdfClient(ILOVEPDF_PUBLIC_KEY, ILOVEPDF_SECRET_KEY)
        pdf_bytes = client.convert_html_to_pdf(html)
        return pdf_bytes, report.title
    except Exception as e:
        print(f"Error: {e}")
        return None, None

# ==========================================
# 5. أوامر تليجرام
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 أهلاً! أرسل لي عنوان التقرير وسأصنعه لك بـ iLovePDF.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text
    msg = await update.message.reply_text(f"⏳ جاري العمل على تقرير: {topic}...")
    
    try:
        pdf_bytes, title = generate_report(topic)
        if pdf_bytes:
            filename = f"{title[:20].replace(' ', '_')}.pdf"
            await update.message.reply_document(document=pdf_bytes, filename=filename, caption="✅ تم!")
        else:
            await update.message.reply_text("❌ حدث خطأ أثناء التحويل.")
    except Exception as e:
        await update.message.reply_text(f"خطأ: {e}")
    finally:
        await msg.delete()

# ==========================================
# 6. التشغيل
# ==========================================
if __name__ == '__main__':
    # تشغيل السيرفر في الخلفية
    threading.Thread(target=run_flask).start()
    
    # تشغيل البوت
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("❌ Error: TELEGRAM_TOKEN missing")
    else:
        app = ApplicationBuilder().token(token).build()
        app.add_handler(CommandHandler('start', start))
        app.add_handler(MessageHandler(filters.TEXT, handle_message))
        print("🤖 Bot Started...")
        app.run_polling()
        