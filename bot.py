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
from io import BytesIO
from weasyprint import HTML

# ==========================================
# إعداد Logging
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# Flask Server
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
# AI Models
# ==========================================
class Section(BaseModel):
    title: str = Field(description="عنوان القسم")
    content: str = Field(description="المحتوى")

class AcademicReport(BaseModel):
    title: str = Field(description="عنوان التقرير")
    introduction: str = Field(description="المقدمة")
    sections: List[Section] = Field(description="الأقسام (3-5 أقسام)")
    conclusion: str = Field(description="الخاتمة")

# ==========================================
# HTML Template
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
    @page {
        size: A4;
        margin: 2cm;
    }
    body {
        font-family: 'Arial', 'Traditional Arabic', sans-serif;
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
        font-size: 28px;
    }
    h2 {
        color: #0066cc;
        margin-top: 25px;
        border-right: 5px solid #0066cc;
        padding-right: 15px;
        padding: 10px 15px;
        font-size: 20px;
    }
    p {
        text-align: justify;
        line-height: 1.8;
        margin-bottom: 15px;
        font-size: 14px;
    }
    .intro, .conclusion {
        background-color: #f5f5f5;
        padding: 20px;
        border-radius: 5px;
        margin: 20px 0;
    }
    .footer {
        text-align: center;
        margin-top: 50px;
        padding-top: 20px;
        border-top: 2px solid #ddd;
        color: #999;
        font-size: 11px;
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
<div>
    <h2>{{ section.title }}</h2>
    {{ section.content | safe }}
</div>
{% endfor %}

<div class="conclusion">
    <h2>الخاتمة</h2>
    {{ conc | safe }}
</div>

<div class="footer">تم الإنشاء بواسطة Telegram Bot</div>
</body>
</html>
"""

# ==========================================
# Generate Report Function
# ==========================================
def generate_report(topic):
    try:
        # 1. Check API Key
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            logger.error("❌ GOOGLE_API_KEY not found")
            raise Exception("API Key غير موجود")
        
        logger.info(f"📝 Generating report for: {topic}")
        
        # 2. Initialize LLM - استخدام النموذج الصحيح
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0.4,
            google_api_key=api_key,
            max_retries=3
        )
        
        # 3. Create Parser
        parser = PydanticOutputParser(pydantic_object=AcademicReport)
        
        # 4. Create Prompt
        prompt = PromptTemplate(
            input_variables=["topic"],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            template="""أنت كاتب أكاديمي محترف. اكتب تقريرًا مفصلاً وشاملاً عن:

الموضوع: {topic}

يجب أن يحتوي التقرير على:
- مقدمة شاملة (150-200 كلمة)
- 3-4 أقسام رئيسية (كل قسم 200-250 كلمة)
- خاتمة موجزة (100-150 كلمة)

اكتب بلغة عربية فصحى وأسلوب أكاديمي احترافي.

{format_instructions}"""
        )
        
        # 5. Generate Report
        logger.info("🤖 Calling Gemini API...")
        report = (prompt | llm | parser).invoke({"topic": topic})
        logger.info("✅ Report generated successfully")
        
        # 6. Clean Text
        def clean(text):
            paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
            return "".join([f"<p>{p}</p>" for p in paragraphs])
        
        # 7. Render HTML
        html = Template(HTML_TEMPLATE).render(
            title=report.title,
            intro=clean(report.introduction),
            sections=[{'title': s.title, 'content': clean(s.content)} for s in report.sections],
            conc=clean(report.conclusion)
        )
        
        logger.info("📄 Converting HTML to PDF...")
        
        # 8. Convert to PDF using WeasyPrint
        pdf_bytes = HTML(string=html).write_pdf()
        
        logger.info("✅ PDF created successfully")
        return pdf_bytes, report.title
        
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        return None, str(e)

# ==========================================
# Telegram Handlers
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = """
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
    await update.message.reply_text(welcome, parse_mode='Markdown')
    logger.info(f"✅ User {update.effective_user.id} started the bot")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    
    if len(topic) < 3:
        await update.message.reply_text("❌ الموضوع قصير جداً! أرسل موضوعاً أطول من 3 أحرف.")
        return
    
    if len(topic) > 100:
        await update.message.reply_text("❌ الموضوع طويل جداً! حاول اختصاره لأقل من 100 حرف.")
        return
    
    msg = await update.message.reply_text(
        f"⏳ جاري العمل على تقرير:\n*{topic}*\n\n⏱️ قد يستغرق الأمر 30-60 ثانية...",
        parse_mode='Markdown'
    )
    
    try:
        logger.info(f"📝 User {update.effective_user.id} requested: {topic}")
        
        pdf_bytes, result = generate_report(topic)
        
        if pdf_bytes:
            # إنشاء اسم ملف آمن
            safe_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in result[:30])
            filename = f"{safe_name}.pdf"
            
            await update.message.reply_document(
                document=BytesIO(pdf_bytes),
                filename=filename,
                caption=f"✅ *تم إنشاء التقرير بنجاح!*\n\n📄 *{result}*\n\n🔗 يمكنك تحميله الآن",
                parse_mode='Markdown'
            )
            logger.info(f"✅ PDF sent successfully to user {update.effective_user.id}")
        else:
            error_msg = f"❌ *حدث خطأ أثناء إنشاء التقرير*\n\n📋 التفاصيل:\n`{result}`\n\n💡 *تأكد من:*\n• صحة موضوع التقرير\n• الاتصال بالإنترنت\n\n🔄 حاول مرة أخرى"
            await update.message.reply_text(error_msg, parse_mode='Markdown')
            logger.error(f"❌ Failed for user {update.effective_user.id}: {result}")
            
    except Exception as e:
        logger.error(f"❌ Handler error for user {update.effective_user.id}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ *خطأ غير متوقع*\n\n`{str(e)[:200]}`\n\n🔄 حاول مرة أخرى لاحقاً",
            parse_mode='Markdown'
        )
    
    finally:
        try:
            await msg.delete()
        except:
            pass

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"❌ Update error: {context.error}", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ حدث خطأ في معالجة طلبك. حاول مرة أخرى."
            )
    except:
        pass

# ==========================================
# Main
# ==========================================
if __name__ == '__main__':
    # Start Flask in background
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask server started on port 10000")
    
    # Start Telegram Bot
    token = os.getenv("TELEGRAM_TOKEN")
    
    if not token:
        logger.error("❌ TELEGRAM_TOKEN not found in environment variables")
        print("❌ Error: TELEGRAM_TOKEN is missing!")
        exit(1)
    
    try:
        application = ApplicationBuilder().token(token).build()
        
        # Add Handlers
        application.add_handler(CommandHandler('start', start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        logger.info("🤖 Bot started successfully!")
        print("=" * 50)
        print("✅ Bot is now running...")
        print("=" * 50)
        
        # Run Bot
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}", exc_info=True)
        print(f"❌ Startup Error: {e}")
        exit(1)

