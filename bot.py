import os
import requests
import threading
import logging
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
from io import BytesIO

# ==========================================
# إعداد Logging
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# 1. إعدادات السيرفر (Flask)
# ==========================================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ iLovePDF Bot is Running!"

@flask_app.route('/health')
def health():
    return {"status": "healthy", "bot": "active"}, 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ==========================================
# 2. إعدادات iLovePDF
# ==========================================
ILOVEPDF_PUBLIC_KEY = os.getenv("ILOVEPDF_PUBLIC_KEY", "project_public_e37dbc55b1cef99987608cd0c7a1a938_CVhdn3750fc8930cf0fbd2488cfd41d2ff309")
ILOVEPDF_SECRET_KEY = os.getenv("ILOVEPDF_SECRET_KEY", "secret_key_5ec8ca22feb71d81cc8189d33d788f27_EZlk7b435659353674fc5132a1246cc334bf0")

# ==========================================
# 3. كلاس iLovePDF محسّن
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
            response = requests.post(url, json={"public_key": self.public_key}, timeout=10)
            if response.status_code == 200:
                self.token = response.json().get('token')
                logger.info("✅ iLovePDF Authentication successful")
                return True
            logger.error(f"❌ Auth failed: {response.status_code}")
            return False
        except Exception as e:
            logger.error(f"❌ Auth error: {e}")
            return False

    def convert_html_to_pdf(self, html_content):
        try:
            if not self.token and not self.auth():
                return None
            
            headers = {"Authorization": f"Bearer {self.token}"}

            # Start Task
            start_resp = requests.get(f"{self.base_url}/start/htmlpdf", headers=headers, timeout=10)
            if start_resp.status_code != 200:
                logger.error(f"Start failed: {start_resp.status_code}")
                return None
            
            task_id = start_resp.json()['task']
            server = start_resp.json()['server']
            logger.info(f"Task started: {task_id}")
            
            # Upload HTML
            upload_url = f"https://{server}/v1/upload"
            files = {'file': ('report.html', html_content.encode('utf-8'), 'text/html')}
            data = {'task': task_id}
            upload_resp = requests.post(upload_url, headers=headers, files=files, data=data, timeout=30)
            
            if upload_resp.status_code != 200:
                logger.error(f"Upload failed: {upload_resp.status_code}")
                return None
            
            server_filename = upload_resp.json()['server_filename']
            logger.info("HTML uploaded successfully")

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
            process_resp = requests.post(process_url, headers=headers, json=process_data, timeout=30)
            
            if process_resp.status_code != 200:
                logger.error(f"Process failed: {process_resp.status_code}")
                return None
            
            logger.info("Processing PDF...")

            # Download with retries
            time.sleep(3)
            download_url = f"https://{server}/v1/download/{task_id}"
            
            for attempt in range(3):
                download_resp = requests.get(download_url, headers=headers, timeout=30)
                if download_resp.status_code == 200:
                    logger.info("✅ PDF generated successfully")
                    return download_resp.content
                time.sleep(2)
            
            logger.error("Download failed after retries")
            return None
            
        except Exception as e:
            logger.error(f"❌ PDF conversion error: {e}")
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
    sections: List[Section] = Field(description="الأقسام (3-5 أقسام)")
    conclusion: str = Field(description="الخاتمة")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<style>
    body {
        font-family: 'Arial', 'Traditional Arabic', sans-serif;
        padding: 40px;
        direction: rtl;
        text-align: right;
        line-height: 1.8;
        color: #333;
    }
    h1 {
        text-align: center;
        border-bottom: 3px solid #0066cc;
        padding-bottom: 15px;
        color: #0066cc;
        margin-bottom: 30px;
    }
    h2 {
        color: #0066cc;
        margin-top: 35px;
        border-right: 5px solid #0066cc;
        padding-right: 15px;
        padding-top: 10px;
        padding-bottom: 10px;
    }
    p {
        text-align: justify;
        line-height: 1.8;
        margin-bottom: 15px;
        font-size: 14px;
    }
    .intro, .conclusion {
        background-color: #f9f9f9;
        padding: 20px;
        border-radius: 5px;
        margin: 20px 0;
    }
    .footer {
        text-align: center;
        margin-top: 60px;
        padding-top: 20px;
        border-top: 2px solid #ddd;
        color: #777;
        font-size: 12px;
    }
</style>
</head>
<body>
<h1>{{ title }}</h1>

<div class="intro">
    <h2>المقدمة</h2>
    {{ intro | safe }}
</div>

{% for section in sections %}
<div class="section">
    <h2>{{ section.title }}</h2>
    {{ section.content | safe }}
</div>
{% endfor %}

<div class="conclusion">
    <h2>الخاتمة</h2>
    {{ conc | safe }}
</div>

<div class="footer">
    تم الإنشاء بواسطة Telegram Bot | {{ date }}
</div>
</body>
</html>
"""

def generate_report(topic):
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            logger.error("❌ GOOGLE_API_KEY not found")
            return None, None
        
        logger.info(f"Generating report for: {topic}")
        
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash-exp",
            temperature=0.4,
            google_api_key=api_key
        )
        
        parser = PydanticOutputParser(pydantic_object=AcademicReport)
        
        prompt = PromptTemplate(
            input_variables=["topic"],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            template="""أنت كاتب أكاديمي محترف. اكتب تقريرًا شاملاً ومفصلاً عن الموضوع التالي:

الموضوع: {topic}

يجب أن يحتوي التقرير على:
- مقدمة شاملة (150-200 كلمة)
- 3-5 أقسام رئيسية (كل قسم 200-300 كلمة)
- خاتمة موجزة (100-150 كلمة)

اكتب بلغة عربية فصحى وأسلوب أكاديمي احترافي.

{format_instructions}"""
        )
        
        report = (prompt | llm | parser).invoke({"topic": topic})
        logger.info("✅ Report generated by AI")
        
        # HTML Rendering
        def clean(text):
            paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
            return "".join([f"<p>{p}</p>" for p in paragraphs])
        
        from datetime import datetime
        current_date = datetime.now().strftime("%Y-%m-%d")
        
        html = Template(HTML_TEMPLATE).render(
            title=report.title,
            intro=clean(report.introduction),
            sections=[{'title': s.title, 'content': clean(s.content)} for s in report.sections],
            conc=clean(report.conclusion),
            date=current_date
        )
        
        # PDF Conversion
        client = ILovePdfClient(ILOVEPDF_PUBLIC_KEY, ILOVEPDF_SECRET_KEY)
        pdf_bytes = client.convert_html_to_pdf(html)
        
        return pdf_bytes, report.title
        
    except Exception as e:
        logger.error(f"❌ Report generation error: {e}", exc_info=True)
        return None, None

# ==========================================
# 5. أوامر تليجرام
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = """
🤖 *مرحباً بك في بوت التقارير الأكاديمية!*

📝 *كيف تستخدم البوت؟*
فقط أرسل لي عنوان الموضوع وسأقوم بإنشاء تقرير أكاديمي احترافي بصيغة PDF

✨ *أمثلة:*
- الذكاء الاصطناعي
- التغير المناخي
- الطاقة المتجددة
- الأمن السيبراني

⏱️ *وقت الإنشاء: 30-60 ثانية*
    """
    await update.message.reply_text(welcome_msg, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    
    if len(topic) < 3:
        await update.message.reply_text("❌ الموضوع قصير جداً! أرسل موضوعاً أطول من 3 أحرف.")
        return
    
    msg = await update.message.reply_text(f"⏳ جاري العمل على تقرير:\n*{topic}*\n\nقد يستغرق الأمر 30-60 ثانية...", parse_mode='Markdown')
    
    try:
        pdf_bytes, title = generate_report(topic)
        
        if pdf_bytes:
            # إنشاء اسم ملف صحيح
            safe_filename = "".join(c if c.isalnum() or c in (' ', '_') else '_' for c in title[:30])
            filename = f"{safe_filename}.pdf"
            
            # إرسال الملف
            await update.message.reply_document(
                document=BytesIO(pdf_bytes),
                filename=filename,
                caption=f"✅ *تم إنشاء التقرير بنجاح!*\n\n📄 {title}",
                parse_mode='Markdown'
            )
            logger.info(f"✅ PDF sent to user: {update.effective_user.id}")
        else:
            await update.message.reply_text("❌ عذراً، حدث خطأ أثناء إنشاء التقرير. حاول مرة أخرى.")
            
    except Exception as e:
        logger.error(f"Error in handle_message: {e}", exc_info=True)
        await update.message.reply_text(f"❌ حدث خطأ: {str(e)}\n\nحاول مرة أخرى لاحقاً.")
    finally:
        try:
            await msg.delete()
        except:
            pass

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

# ==========================================
# 6. التشغيل
# ==========================================
if __name__ == '__main__':
    # تشغيل Flask في الخلفية
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask server started")
    
    # تشغيل البوت
    token = os.getenv("TELEGRAM_TOKEN")
    
    if not token:
        logger.error("❌ TELEGRAM_TOKEN not found in environment variables")
        exit(1)
    
    try:
        application = ApplicationBuilder().token(token).build()
        
        # إضافة Handlers
        application.add_handler(CommandHandler('start', start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        logger.info("🤖 Bot started successfully!")
        print("✅ Bot is now running...")
        
        # تشغيل البوت
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}", exc_info=True)
        exit(1)
