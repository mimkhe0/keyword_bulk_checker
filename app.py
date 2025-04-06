import flask
from flask import Flask, render_template, request, send_file, abort, g
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import uuid
from werkzeug.utils import secure_filename
import validators
import re
from collections import Counter
from datetime import datetime, timedelta
import logging
import sqlite3 # جایگزین اکسل برای ذخیره اطلاعات کاربر
import time # برای اندازه‌گیری زمان

# --- Configuration ---
# بهتر است این مقادیر در یک فایل کانفیگ یا متغیرهای محیطی باشند
INSTANCE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
DATABASE = os.path.join(INSTANCE_FOLDER, 'database.db')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')

TIMEOUT_PER_URL = 8 # افزایش جزئی تایم‌اوت
MAX_URLS_TO_FETCH = 30 # افزایش تعداد URL های قابل بررسی
MAX_URLS_FOR_TEXT_EXTRACTION = 10 # محدود کردن تعداد URL برای استخراج متن اولیه (برای تعیین زبان و کلمات ایستا)
MAX_WORKERS_FETCH = 15 # تعداد ترد برای دریافت URL ها
MAX_WORKERS_CHECK = 10 # تعداد ترد برای بررسی کلمات کلیدی
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'.xlsx'}
CLEANUP_DAYS = 1 # پاک کردن فایل‌های قدیمی‌تر از ۱ روز

# --- Flask App Setup ---
app = Flask(__name__)
app.config['INSTANCE_FOLDER'] = INSTANCE_FOLDER
app.config['DATABASE'] = DATABASE
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['RESULTS_FOLDER'] = RESULTS_FOLDER

# ایجاد پوشه‌های لازم
os.makedirs(INSTANCE_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESULTS_FOLDER, exist_ok=True)

# Setup logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(process)d - %(thread)d - %(message)s'
)
logging.getLogger("requests").setLevel(logging.WARNING) # کاهش لاگ‌های requests
logging.getLogger("urllib3").setLevel(logging.WARNING) # کاهش لاگ‌های urllib3

# --- Database Functions ---
def get_db():
    """اتصال به دیتابیس یا استفاده از اتصال موجود در کانتکست g."""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(
            app.config['DATABASE'],
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        db.row_factory = sqlite3.Row # دسترسی به نتایج با نام ستون
    return db

@app.teardown_appcontext
def close_connection(exception):
    """بستن اتصال دیتابیس در پایان درخواست."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    """ایجاد جدول کاربران اگر وجود نداشته باشد."""
    try:
        with app.app_context():
            db = get_db()
            with db: # استفاده از context manager برای commit یا rollback خودکار
                db.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT NOT NULL,
                        website TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
            logging.info("Database initialized successfully.")
    except sqlite3.Error as e:
        logging.error(f"Database initialization failed: {e}", exc_info=True)
        # در محیط عملیاتی شاید بهتر باشد برنامه متوقف شود
        raise # Re-raise the exception to make it clear initialization failed

def save_user_data(email, website):
    """ذخیره اطلاعات کاربر در دیتابیس SQLite."""
    sql = 'INSERT INTO users (email, website) VALUES (?, ?)'
    try:
        db = get_db()
        with db:
            db.execute(sql, (email, website))
        logging.info(f"User data saved: {email}, {website}")
        return True
    except sqlite3.IntegrityError:
        logging.warning(f"Attempt to save duplicate or invalid user data: {email}, {website}")
        return False # یا مدیریت خطای یکتایی به نحو دیگر
    except sqlite3.Error as e:
        logging.error(f"Failed to save user data for {email}: {e}", exc_info=True)
        return False

# --- Helper Functions ---
def allowed_file(filename):
    """بررسی پسوند فایل مجاز."""
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

# *** بازنگری شده: استخراج متن از یک URL ***
def extract_text_from_url(url, session):
    """محتوای متنی یک URL را با استفاده از session دریافت و استخراج می‌کند."""
    try:
        # استفاده از session برای کارایی بهتر (اتصالات مجدد)
        with session.get(url, timeout=TIMEOUT_PER_URL, allow_redirects=True) as response:
            response.raise_for_status() # بررسی خطاهای HTTP (4xx, 5xx)
            # بررسی نوع محتوا برای جلوگیری از پردازش فایل‌های غیر HTML
            content_type = response.headers.get('content-type', '').lower()
            if 'html' not in content_type:
                logging.warning(f"Skipping non-HTML content at {url} (Content-Type: {content_type})")
                return url, None # برگرداندن None برای متن

            # استفاده از lxml برای سرعت بیشتر در پارس کردن
            soup = BeautifulSoup(response.content, 'lxml')

            # حذف تگ‌های نامرتبط (قابل بهبود)
            for element in soup(['script', 'style', 'footer', 'nav', 'form', 'head', 'header', 'aside', 'noscript']):
                element.decompose()

            # استخراج متن با جداساز فاصله و تبدیل به حروف کوچک
            text = soup.get_text(separator=' ', strip=True).lower()
            # حذف فاصله‌های اضافی و خطوط خالی
            text = re.sub(r'\s+', ' ', text).strip()
            return url, text
    except requests.Timeout:
        logging.warning(f"Timeout fetching {url}")
        return url, None
    except requests.RequestException as e:
        # لاگ کردن خطای خاص‌تر
        logging.warning(f"Failed to fetch/process {url}: {type(e).__name__} - {str(e)}")
        return url, None
    except Exception as e:
        logging.error(f"Unexpected error processing {url}: {e}", exc_info=True)
        return url, None

# *** جدید: دریافت و استخراج متن از چندین URL به صورت موازی ***
def fetch_and_extract_texts_parallel(urls):
    """
    محتوای متنی چندین URL را به صورت موازی دریافت و استخراج می‌کند.
    Returns:
        dict: دیکشنری از {url: extracted_text} که text می‌تواند None باشد در صورت خطا.
    """
    url_texts = {}
    # استفاده از session برای بهبود کارایی درخواست‌های HTTP
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 KeywordCheckerBot/1.0'}
    with requests.Session() as session:
        session.headers.update(headers)
        # استفاده از ThreadPoolExecutor برای انجام موازی درخواست‌ها
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_FETCH) as executor:
            # ارسال وظایف به executor
            future_to_url = {executor.submit(extract_text_from_url, url, session): url for url in urls}
            # جمع‌آوری نتایج به محض آماده شدن
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    res_url, text = future.result()
                    if text: # فقط متون استخراج شده موفق را اضافه کن
                        url_texts[res_url] = text
                except Exception as e:
                    # خطاهای غیرمنتظره در اجرای ترد
                    logging.error(f"Error in future result for {url}: {e}", exc_info=True)

    logging.info(f"Successfully extracted text from {len(url_texts)} out of {len(urls)} URLs.")
    return url_texts

# *** بازنگری شده: دریافت URL های داخلی از یک وب‌سایت ***
def get_internal_urls(base_url, session):
    """
    URL های داخلی یک وب‌سایت را تا سقف MAX_URLS_TO_FETCH پیدا می‌کند.
    Returns:
        list: لیستی از URL های معتبر داخلی یا None در صورت خطای اولیه.
    """
    urls = set()
    # اطمینان از وجود scheme در base_url
    if not base_url.startswith(('http://', 'https://')):
        base_url = 'http://' + base_url # فرض بر http

    # نرمال‌سازی base_url (حذف اسلش انتهایی)
    normalized_base_url = base_url.rstrip('/')
    urls.add(normalized_base_url) # اضافه کردن خود صفحه اصلی

    try:
        # اولین درخواست برای صفحه اصلی
        with session.get(normalized_base_url, timeout=10) as response:
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').lower()
            if 'html' not in content_type:
                logging.error(f"Base URL {normalized_base_url} is not HTML. Cannot crawl.")
                return list(urls) # فقط خود URL اصلی را برگردان

            soup = BeautifulSoup(response.content, 'lxml')

            # پیدا کردن تمام لینک‌ها
            for a_tag in soup.select('a[href]'):
                href = a_tag.get('href')
                if not href:
                    continue

                # ساخت URL کامل و حذف fragment (#)
                full_url = urljoin(normalized_base_url, href).split('#')[0].rstrip('/')

                # بررسی اینکه آیا لینک داخلی است و معتبر است
                if full_url.startswith(normalized_base_url) and validators.url(full_url):
                    urls.add(full_url)
                    if len(urls) >= MAX_URLS_TO_FETCH:
                        break # رسیدن به سقف تعداد URL

        logging.info(f"Found {len(urls)} internal URLs starting from {normalized_base_url}")
        return list(urls)

    except requests.Timeout:
        logging.error(f"Timeout fetching base URL {normalized_base_url}")
        return None # نشان‌دهنده خطای اساسی
    except requests.RequestException as e:
        logging.error(f"Failed to get base URL {normalized_base_url}: {e}", exc_info=True)
        return None # نشان‌دهنده خطای اساسی
    except Exception as e:
        logging.error(f"Unexpected error getting URLs from {normalized_base_url}: {e}", exc_info=True)
        return None


# Detect language (same basic function)
def detect_language(text_sample):
    """تشخیص ساده زبان بر اساس وجود حروف فارسی/عربی."""
    if not text_sample:
        return 'en' # پیش‌فرض انگلیسی اگر متنی نباشد
    # جستجوی کاراکترهای رنج یونیکد فارسی و عربی
    if re.search(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', text_sample):
        return 'fa_ar'
    return 'en'

# Generate dynamic stop words (same basic function, maybe increase top_n slightly)
def get_dynamic_stop_words(text, lang, top_n=50): # افزایش top_n
    """تولید کلمات ایستا شامل کلمات رایج و پرتکرارترین کلمات در متن."""
    # استفاده از \w+ برای گرفتن کلمات شامل اعداد (اگر لازم باشد)
    # استفاده از {3,} برای حذف کلمات خیلی کوتاه (مثل "a", "i", "و")
    words = re.findall(r'\b\w{3,}\b', text.lower())
    if not words:
        return set()

    word_counts = Counter(words)
    most_common_words = {word for word, count in word_counts.most_common(top_n)}

    # لیست پایه بزرگتر و دقیق‌تر برای کلمات ایستا
    general_stop_words = {
        'en': {
            'the', 'a', 'an', 'and', 'in', 'of', 'for', 'with', 'to', 'from', 'by', 'is', 'are', 'on', 'at',
            'it', 'this', 'that', 'or', 'as', 'was', 'were', 'be', 'been', 'have', 'has', 'had', 'but', 'not',
            'you', 'your', 'we', 'our', 'us', 'he', 'she', 'they', 'them', 'their', 'will', 'can', 'could',
            'would', 'should', 'also', 'more', 'most', 'all', 'any', 'some', 'other', 'about', 'out', 'up',
            'down', 'into', 'through', 'over', 'under', 'again', 'further', 'then', 'once', 'here', 'there',
            'when', 'where', 'why', 'how', 'which', 'who', 'whom', 'whose', 'its', 'etc', 'http', 'https',
            'www', 'com', 'org', 'net', 'gov', 'edu', 'html' # اضافه کردن موارد مرتبط با وب
        },
        'fa_ar': {
            'و', 'در', 'به', 'از', 'که', 'این', 'آن', 'است', 'را', 'با', 'برای', 'تا', 'یا', 'هم', 'نیز', 'شد',
            'شده', 'بود', 'کرد', 'کند', 'کنند', 'کنند', 'های', 'هایی', 'ای', 'یک', 'دو', 'سه', 'بر', 'هر',
            'همه', 'وی', 'او', 'ما', 'شما', 'ایشان', 'آنها', 'خود', 'دیگر', 'ولی', 'اما', 'اگر', 'پس', 'چون',
            'حتی', 'فقط', 'باید', 'نباید', 'باشد', 'نباشد', 'آیا', 'کدام', 'چه', 'چگونه', 'کجا', 'کی', 'ضمن',
            'بین', 'روی', 'زیر', 'بالای', 'کنار', 'مقابل', 'طبق', 'مانند', 'مثل', 'حدود', 'طی', 'علیه', 'الا',
            'فی', 'کل', 'غیر', 'ذلک', 'من', 'الی', 'عن', 'حتیٰ', 'اذا', 'لم', 'لن', 'کان', 'لیس', 'قد', 'ما',
             'ها', 'تر', 'ترین', # پسوندهای رایج
            'میلادی', 'شمسی', 'هجری', 'سال', 'ماه', 'روز', # کلمات مرتبط با تاریخ
            'شرکت', 'سازمان', 'اداره', 'موسسه', # کلمات عمومی سازمانی
            'صفحه', 'اصلی', 'تماس', 'درباره', 'ما', 'محصولات', 'خدمات' # منوهای رایج
        }
    }
    # اطمینان از وجود کلید زبان
    base_stops = general_stop_words.get(lang, set())
    dynamic_stops = most_common_words - base_stops # کلمات پرتکرار که جزو لیست پایه نیستند
    # لاگ کردن کلمات ایستا برای بررسی
    logging.debug(f"Base stop words ({lang}): {len(base_stops)}")
    logging.debug(f"Dynamic stop words ({lang}, top {top_n}): {dynamic_stops}")

    # ترکیب لیست پایه با کلمات پرتکرار
    return base_stops.union(most_common_words)

# *** بازنگری شده: بررسی وجود کلمه کلیدی در متون از پیش استخراج شده ***
def check_keyword_in_texts(phrase, url_texts_dict):
    """
    جستجوی یک عبارت (phrase) در دیکشنری متون استخراج شده.
    Returns:
        dict: نتیجه جستجو شامل 'found', 'url', 'score', 'preview'.
    """
    best_match = {"found": False, "url": "-", "score": 0, "preview": ""}
    phrase_lower = phrase.lower() # جستجو به صورت case-insensitive

    for url, text in url_texts_dict.items():
        if text is None: # اگر استخراج متن برای این URL ناموفق بود
            continue

        try:
            # استفاده از find به جای in برای گرفتن اندیس و ایجاد پیش‌نمایش
            index = text.find(phrase_lower)
            if index != -1:
                # شمارش تعداد تکرار برای امتیازدهی (روش ساده)
                # برای دقت بیشتر می‌توان از الگوریتم‌های وزن‌دهی (مثل TF-IDF) استفاده کرد
                score = text.count(phrase_lower)

                # ساخت پیش‌نمایش با کمی متن اطراف
                start = max(0, index - 60) # افزایش طول پیش‌نمایش
                end = min(len(text), index + len(phrase_lower) + 60)
                preview_text = text[start:end].strip()
                # برجسته کردن کلمه کلیدی در پیش‌نمایش (اختیاری)
                # preview_text = preview_text.replace(phrase_lower, f"<b>{phrase_lower}</b>")

                # اگر امتیاز بالاتر بود یا اولین مورد یافت شده بود
                if score > best_match["score"]:
                    best_match["found"] = True
                    best_match["url"] = url
                    best_match["score"] = score
                    best_match["preview"] = f"...{preview_text}..."

                # اگر فقط پیدا شدن مهم است و نه بهترین امتیاز، می‌توان break کرد
                # break

        except Exception as e:
            logging.warning(f"Error searching for '{phrase}' in text from {url}: {e}")
            continue # ادامه جستجو در URL های بعدی

    return best_match


# Cleanup old files
def cleanup_old_files():
    """پاک کردن فایل‌های قدیمی آپلود و نتایج."""
    now = time.time()
    cutoff = now - (CLEANUP_DAYS * 24 * 60 * 60)
    folders_to_clean = [app.config['UPLOAD_FOLDER'], app.config['RESULTS_FOLDER']]

    cleaned_count = 0
    for folder in folders_to_clean:
        try:
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path):
                    file_mtime = os.path.getmtime(file_path)
                    if file_mtime < cutoff:
                        os.remove(file_path)
                        cleaned_count += 1
                        logging.info(f"Cleaned up old file: {file_path}")
        except FileNotFoundError:
             logging.warning(f"Cleanup folder not found: {folder}")
        except Exception as e:
            logging.error(f"Error during cleanup in {folder}: {e}", exc_info=True)

    if cleaned_count > 0:
        logging.info(f"Cleanup finished. Removed {cleaned_count} old files.")


# --- Flask Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    start_time = time.time()
    results_data = []
    download_filename = None
    error_message = None
    website_url = request.form.get('website', '').strip() # دریافت در ابتدای تابع

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        file = request.files.get('file')

        # --- 1. Input Validation ---
        validation_errors = []
        if not email or not validators.email(email):
            validation_errors.append("آدرس ایمیل معتبر نیست.")
        if not website_url or not validators.url(website_url):
             # تلاش برای اصلاح URL بدون http
            if website_url and not website_url.startswith(('http://', 'https://')):
                website_url = 'http://' + website_url
                if not validators.url(website_url):
                    validation_errors.append(f"آدرس وب‌سایت معتبر نیست: {request.form.get('website', '')}")
            else:
                 validation_errors.append(f"آدرس وب‌سایت معتبر نیست: {request.form.get('website', '')}")

        if not file or not file.filename:
            validation_errors.append("فایل اکسل انتخاب نشده است.")
        elif not allowed_file(file.filename):
            validation_errors.append(f"فرمت فایل مجاز نیست. فقط {ALLOWED_EXTENSIONS} مجاز است.")

        if validation_errors:
            error_message = " | ".join(validation_errors)
            # نمایش دوباره فرم با پیام خطا و مقادیر قبلی (اگر ممکن است)
            return render_template("index.html", error=error_message, website=website_url, email=email)

        # --- 2. Save User Data ---
        if not save_user_data(email, website_url):
            # ادامه کار حتی اگر ذخیره ناموفق بود، اما لاگ شد
            logging.warning(f"Proceeding despite failed user data save for {email}")
            # error_message = "خطا در ذخیره اطلاعات کاربر." # یا نمایش خطا و توقف

        # --- 3. Handle File Upload ---
        # استفاده از نام امن و UUID برای جلوگیری از تداخل و مشکلات امنیتی
        safe_filename = secure_filename(file.filename)
        temp_filename = f"{uuid.uuid4()}_{safe_filename}"
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
        try:
            file.save(temp_path)
            logging.info(f"File uploaded successfully to {temp_path}")

            # --- 4. Read Keywords from Excel ---
            df = pd.read_excel(temp_path)
            if df.empty or len(df.columns) == 0:
                 raise ValueError("فایل اکسل خالی است یا ستون اول وجود ندارد.")
            # استفاده از iloc برای اطمینان از خواندن ستون اول، حذف NaN و تبدیل به رشته
            keywords = df.iloc[:, 0].dropna().astype(str).unique().tolist()
            if not keywords:
                raise ValueError("هیچ کلمه کلیدی معتبری در ستون اول فایل اکسل یافت نشد.")
            logging.info(f"Read {len(keywords)} unique keywords from {safe_filename}")

        except ValueError as e:
             error_message = f"خطا در خواندن فایل اکسل: {e}"
             if os.path.exists(temp_path): os.remove(temp_path)
             return render_template("index.html", error=error_message, website=website_url, email=email)
        except Exception as e:
            logging.error(f"Error processing uploaded file {safe_filename}: {e}", exc_info=True)
            error_message = f"خطای ناشناخته در پردازش فایل: {e}"
            if os.path.exists(temp_path): os.remove(temp_path)
            return render_template("index.html", error=error_message, website=website_url, email=email)


        # --- 5. Crawl and Fetch Website Content ---
        urls_to_check = None
        url_texts = {}
        try:
            headers = {'User-Agent': 'Mozilla/5.0 KeywordCheckerBot/1.0'}
            with requests.Session() as session:
                session.headers.update(headers)
                # دریافت URL های داخلی
                crawl_start_time = time.time()
                urls_to_check = get_internal_urls(website_url, session)
                logging.info(f"Crawling took {time.time() - crawl_start_time:.2f} seconds.")

                if not urls_to_check:
                    raise ConnectionError(f"امکان دسترسی به وب‌سایت اصلی ({website_url}) یا یافتن لینک‌های داخلی وجود ندارد.")

                logging.info(f"Fetching content for {len(urls_to_check)} URLs...")
                fetch_start_time = time.time()
                # دریافت و استخراج متن به صورت موازی
                url_texts = fetch_and_extract_texts_parallel(urls_to_check)
                logging.info(f"Fetching and extraction took {time.time() - fetch_start_time:.2f} seconds.")

                if not url_texts:
                    raise ValueError("محتوایی برای بررسی یافت نشد. ممکن است URL ها قابل دسترس نباشند یا محتوای متنی نداشته باشند.")

        except (ConnectionError, ValueError) as e:
            error_message = str(e)
            if os.path.exists(temp_path): os.remove(temp_path)
            return render_template("index.html", error=error_message, website=website_url, email=email)
        except Exception as e:
            logging.error(f"Error during crawling/fetching for {website_url}: {e}", exc_info=True)
            error_message = f"خطای غیرمنتظره در دریافت اطلاعات وب‌سایت: {e}"
            if os.path.exists(temp_path): os.remove(temp_path)
            return render_template("index.html", error=error_message, website=website_url, email=email)

        # --- 6. Language Detection and Stop Words ---
        # ترکیب بخشی از متون برای تشخیص زبان و کلمات ایستا
        # sample_text = ' '.join(list(url_texts.values())[:MAX_URLS_FOR_TEXT_EXTRACTION]) # ترکیب متن چند URL اول
        sample_text = ' '.join(url_texts.values()) # یا ترکیب تمام متون
        lang = detect_language(sample_text)
        dynamic_stop_words = get_dynamic_stop_words(sample_text, lang)
        logging.info(f"Detected language: {lang}. Generated {len(dynamic_stop_words)} stop words.")

        # --- 7. Prepare Phrases to Check ---
        phrases_to_check = set() # استفاده از set برای جلوگیری از بررسی موارد تکراری
        for kw in keywords:
            kw_lower = kw.lower()
            phrases_to_check.add(kw_lower) # اضافه کردن کلمه کلیدی اصلی
            # اضافه کردن کلمات مهم (غیر ایستا) از کلمه کلیدی
            # important_words = [w for w in re.findall(r'\b\w{3,}\b', kw_lower) if w not in dynamic_stop_words]
            # phrases_to_check.update(important_words) # فعال کردن این خط در صورت نیاز به بررسی تک کلمات

        logging.info(f"Total unique phrases/keywords to check: {len(phrases_to_check)}")

        # --- 8. Check Keywords in Parallel ---
        check_start_time = time.time()
        temp_results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_CHECK) as executor:
            # ارسال وظایف جستجو برای هر عبارت
            future_to_phrase = {
                executor.submit(check_keyword_in_texts, phrase, url_texts): phrase
                for phrase in phrases_to_check
            }
            # جمع‌آوری نتایج
            for future in as_completed(future_to_phrase):
                phrase = future_to_phrase[future]
                try:
                    result = future.result()
                    # اضافه کردن خود کلمه کلیدی به نتیجه برای نمایش بهتر
                    result['keyword'] = phrase
                    temp_results.append(result)
                except Exception as e:
                    logging.error(f"Error checking keyword '{phrase}': {e}", exc_info=True)
                    # اضافه کردن نتیجه ناموفق برای پیگیری
                    temp_results.append({"keyword": phrase, "found": False, "url": "Error", "score": 0, "preview": str(e)})

        # مرتب‌سازی نتایج بر اساس کلمه کلیدی (اختیاری)
        results_data = sorted(temp_results, key=lambda x: x['keyword'])
        logging.info(f"Keyword checking took {time.time() - check_start_time:.2f} seconds.")

        # --- 9. Generate Output Excel ---
        if results_data:
            try:
                output_filename = f"results_{uuid.uuid4()}.xlsx"
                output_path = os.path.join(app.config['RESULTS_FOLDER'], output_filename)
                # انتخاب ستون‌های مورد نظر و ترتیب آن‌ها
                output_df = pd.DataFrame(results_data, columns=['keyword', 'found', 'url', 'score', 'preview'])
                output_df.to_excel(output_path, index=False, engine='openpyxl') # استفاده از openpyxl
                download_filename = output_filename
                logging.info(f"Results saved to {output_path}")
            except Exception as e:
                logging.error(f"Failed to generate results Excel file: {e}", exc_info=True)
                error_message = "خطا در تولید فایل اکسل نتایج."
        else:
            error_message = "هیچ نتیجه‌ای برای نمایش وجود ندارد."


        # --- 10. Cleanup Uploaded File ---
        finally:
            if 'temp_path' in locals() and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                    logging.info(f"Cleaned up temporary upload file: {temp_path}")
                except OSError as e:
                     logging.error(f"Error removing temporary upload file {temp_path}: {e}")


    # پایان پردازش یا درخواست GET
    total_time = time.time() - start_time
    logging.info(f"Request processing time: {total_time:.2f} seconds.")

    return render_template(
        "index.html",
        results=results_data, # ارسال نتایج به تمپلیت
        download_filename=download_filename,
        error=error_message,
        website=website_url # ارسال مجدد وبسایت برای نمایش در فرم
    )

@app.route('/download/<filename>')
def download(filename):
    """ارسال فایل نتیجه برای دانلود."""
    # اعتبارسنجی و پاکسازی نام فایل ورودی
    safe_filename = secure_filename(filename)
    if not safe_filename:
        logging.warning(f"Attempted download with invalid filename: {filename}")
        abort(400) # Bad Request

    file_path = os.path.join(app.config['RESULTS_FOLDER'], safe_filename)

    # بررسی وجود فایل قبل از ارسال
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        logging.warning(f"Download request for non-existent file: {file_path}")
        abort(404) # Not Found

    try:
        # ارسال فایل به عنوان ضمیمه (attachment)
        # توجه: فایل پس از دانلود توسط cleanup_old_files پاک می‌شود.
        # اگر نیاز به حذف فوری است، می‌توان از after_this_request استفاده کرد،
        # اما ریسک حذف قبل از اتمام دانلود وجود دارد.
        return send_file(file_path, as_attachment=True)
    except Exception as e:
        logging.error(f"Error sending file {file_path}: {e}", exc_info=True)
        abort(500) # Internal Server Error


# --- Main Execution ---
if __name__ == '__main__':
    init_db()  # اطمینان از وجود دیتابیس و جدول در زمان اجرا
    cleanup_old_files()  # پاکسازی فایل‌های قدیمی در زمان شروع
    # اجرای برنامه در حالت دیباگ (برای توسعه)
    # در محیط عملیاتی از Gunicorn یا uWSGI استفاده کنید
    app.run(debug=True, host='0.0.0.0', port=5000) # debug=False برای پروداکشن
    # app.run() # حالت پیش‌فرض
