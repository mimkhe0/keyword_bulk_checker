# -*- coding: utf-8 -*-
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
import sqlite3
import time

# --- Configuration ---
# (Dependencies: pip install Flask pandas requests beautifulsoup4 lxml validators openpyxl)
INSTANCE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
DATABASE = os.path.join(INSTANCE_FOLDER, 'database.db')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')

TIMEOUT_PER_URL = 8
MAX_URLS_TO_FETCH = 30
# MAX_URLS_FOR_TEXT_EXTRACTION = 10 # Unused currently, sample_text uses all texts
MAX_WORKERS_FETCH = 15
MAX_WORKERS_CHECK = 10
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'.xlsx'}
CLEANUP_DAYS = 1

# --- Flask App Setup ---
app = Flask(__name__)
app.config['INSTANCE_FOLDER'] = INSTANCE_FOLDER
app.config['DATABASE'] = DATABASE
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['RESULTS_FOLDER'] = RESULTS_FOLDER

os.makedirs(INSTANCE_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESULTS_FOLDER, exist_ok=True)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(process)d:%(threadName)s] - %(module)s - %(message)s'
)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.INFO)

# --- Database Functions ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        try:
            db = g._database = sqlite3.connect(
                app.config['DATABASE'],
                detect_types=sqlite3.PARSE_DECLTYPES
            )
            db.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            logging.critical(f"Failed to connect to database {app.config['DATABASE']}: {e}", exc_info=True)
            abort(500, description="Database connection error.")
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    try:
        with app.app_context():
            db = get_db()
            with db:
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
        logging.critical(f"Database initialization failed: {e}", exc_info=True)
        raise

def save_user_data(email, website):
    sql = 'INSERT INTO users (email, website) VALUES (?, ?)'
    try:
        db = get_db()
        with db:
            db.execute(sql, (email, website))
        logging.info(f"User data saved: {email}, {website}")
        return True
    except sqlite3.IntegrityError:
        logging.warning(f"Attempt to save duplicate or invalid user data: {email}, {website}")
        return True
    except sqlite3.Error as e:
        logging.error(f"Failed to save user data for {email}: {e}", exc_info=True)
        return False

# --- Helper Functions ---
def allowed_file(filename):
    return '.' in filename and \
           os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_url(url, session):
    try:
        with session.get(url, timeout=TIMEOUT_PER_URL, allow_redirects=True) as response:
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').lower()
            if 'html' not in content_type:
                logging.debug(f"Skipping non-HTML content at {url} (Type: {content_type})")
                return url, None
            soup = BeautifulSoup(response.content, 'lxml')
            for element in soup(['script', 'style', 'footer', 'nav', 'form', 'head', 'header', 'aside', 'noscript', 'link', 'meta']):
                element.decompose()
            text = soup.get_text(separator=' ', strip=True).lower()
            text = re.sub(r'\s+', ' ', text).strip()
            return url, text
    except requests.exceptions.Timeout:
        logging.warning(f"Timeout fetching {url}")
        return url, None
    except requests.exceptions.TooManyRedirects:
        logging.warning(f"Too many redirects for {url}")
        return url, None
    except requests.exceptions.HTTPError as e:
        logging.warning(f"HTTP error fetching {url}: {e.response.status_code} {e.response.reason}")
        return url, None
    except requests.exceptions.RequestException as e:
        logging.warning(f"Request failed for {url}: {type(e).__name__}")
        return url, None
    except Exception as e:
        logging.error(f"Unexpected error processing {url}: {e}", exc_info=True)
        return url, None

def fetch_and_extract_texts_parallel(urls):
    url_texts = {}
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; KeywordCheckerBot/1.1; +http://example.com/bot)'}  # TODO: Replace with actual bot info URL
    with requests.Session() as session:
        session.headers.update(headers)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_FETCH, thread_name_prefix='fetch_url') as executor:
            future_to_url = {executor.submit(extract_text_from_url, url, session): url for url in urls}
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    res_url, text = future.result()
                    if text is not None:
                        url_texts[res_url] = text
                except Exception as e:
                    logging.error(f"Future execution or text extraction failed for {url}: {e}", exc_info=False)
    logging.info(f"Successfully extracted text from {len(url_texts)} out of {len(urls)} URLs.")
    return url_texts

def get_internal_urls(base_url, session):
    urls = set()
    if not base_url.startswith(('http://', 'https://')):
        base_url = 'https://' + base_url
    normalized_base_url = base_url.rstrip('/')
    urls.add(normalized_base_url)
    try:
        with session.get(normalized_base_url, timeout=10) as response:
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').lower()
            if 'html' not in content_type:
                logging.warning(f"Base URL {normalized_base_url} is not HTML. Cannot crawl links.")
                return list(urls)
            soup = BeautifulSoup(response.content, 'lxml')
            for a_tag in soup.select('a[href]'):
                href = a_tag.get('href')
                if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
                    continue
                try:
                    full_url = urljoin(response.url, href).split('#')[0].rstrip('/')
                except ValueError:
                    logging.debug(f"Could not parse href '{href}' relative to {response.url}")
                    continue
                if full_url.startswith(normalized_base_url) and validators.url(full_url):
                    urls.add(full_url)
                    if len(urls) >= MAX_URLS_TO_FETCH:
                        logging.info(f"Reached MAX_URLS_TO_FETCH limit ({MAX_URLS_TO_FETCH}).")
                        break
        logging.info(f"Found {len(urls)} internal URLs starting from {normalized_base_url}")
        return list(urls)
    except requests.exceptions.Timeout:
        logging.error(f"Timeout fetching base URL {normalized_base_url}")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to get base URL {normalized_base_url}: {e}", exc_info=False)
        return None
    except Exception as e:
        logging.error(f"Unexpected error crawling {normalized_base_url}: {e}", exc_info=True)
        return None

def detect_language(text_sample):
    if not text_sample:
        return 'en'
    if re.search(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', text_sample):
        return 'fa_ar'
    return 'en'

def get_dynamic_stop_words(text, lang, top_n=50):
    words = re.findall(r'\b\w{3,}\b', text.lower())
    if not words:
        return set()
    word_counts = Counter(words)
    most_common_words = {word for word, count in word_counts.most_common(top_n)}
    general_stop_words = {
        'en': {
            'the', 'a', 'an', 'and', 'in', 'of', 'for', 'with', 'to', 'from', 'by', 'is', 'are', 'on', 'at',
            'it', 'this', 'that', 'or', 'as', 'was', 'were', 'be', 'been', 'have', 'has', 'had', 'but', 'not',
            'you', 'your', 'we', 'our', 'us', 'he', 'she', 'they', 'them', 'their', 'will', 'can', 'could',
            'would', 'should', 'also', 'more', 'most', 'all', 'any', 'some', 'other', 'about', 'out', 'up',
            'down', 'into', 'through', 'over', 'under', 'again', 'further', 'then', 'once', 'here', 'there',
            'when', 'where', 'why', 'how', 'which', 'who', 'whom', 'whose', 'its', 'etc', 'eg', 'ie',
            'http', 'https', 'www', 'com', 'org', 'net', 'gov', 'edu', 'html', 'pdf', 'inc', 'corp', 'ltd', 'co',
            'home', 'contact', 'search', 'menu', 'page', 'click', 'news', 'events', 'about', 'privacy', 'terms',
            'login', 'logout', 'register', 'account', 'profile', 'settings', 'admin', 'dashboard', 'help', 'faq',
            'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'
        },
        'fa_ar': {
            'و', 'در', 'به', 'از', 'که', 'این', 'آن', 'است', 'را', 'با', 'برای', 'تا', 'یا', 'هم', 'نیز', 'شد',
            'شده', 'شود', 'شوند', 'بود', 'کرد', 'کند', 'کنند', 'کنید', 'کردن', 'های', 'هایی', 'ای', 'یک', 'دو', 'سه', 'بر', 'هر',
            'همه', 'وی', 'او', 'ما', 'شما', 'ایشان', 'آنها', 'خود', 'دیگر', 'ولی', 'اما', 'اگر', 'پس', 'چون',
            'حتی', 'فقط', 'باید', 'نباید', 'باشد', 'نباشد', 'آیا', 'کدام', 'چه', 'چگونه', 'کجا', 'کی', 'چیست', 'واقع',
            'ضمن', 'بین', 'روی', 'زیر', 'بالای', 'کنار', 'مقابل', 'طبق', 'مانند', 'مثل', 'حدود', 'طی', 'طریق', 'نسبت',
            'علیه', 'علی', 'الا', 'فی', 'کل', 'غیر', 'ذلک', 'من', 'الی', 'عن', 'حتیٰ', 'اذا', 'لم', 'لن', 'کان', 'لیس', 'قد', 'ما', 'هو', 'هی',
            'ها', 'تر', 'ترین', 'می', 'نمی', 'بی', 'پیش', 'پس',
            'میلادی', 'شمسی', 'هجری', 'سال', 'ماه', 'روز', 'شنبه', 'یکشنبه', 'دوشنبه', 'سهشنبه', 'چهارشنبه', 'پنجشنبه', 'جمعه', 'ساعت', 'دقیقه', 'ثانیه',
            'شرکت', 'سازمان', 'اداره', 'موسسه', 'دانشگاه', 'گروه', 'بخش', 'واحد', 'مرکز', 'انجمن', 'تیم', 'معاونت', 'مدیریت',
            'صفحه', 'اصلی', 'تماس', 'با', 'ما', 'درباره', 'محصولات', 'خدمات', 'اخبار', 'مقالات', 'پروفایل', 'کاربری', 'ورود', 'خروج', 'ثبت', 'نام',
            'جستجو', 'دانلود', 'ارسال', 'نظر', 'نظرات', 'پاسخ', 'گالری', 'تصاویر', 'ویدئو', 'اطلاعات', 'بیشتر', 'ادامه', 'مطلب', 'قبلی', 'بعدی',
            'اول', 'آخر', 'نقشه', 'سایت', 'کپی', 'رایت', 'طراحی', 'توسعه', 'توسط'
        }
    }
    base_stops = general_stop_words.get(lang, set())
    dynamic_only = most_common_words - base_stops
    if dynamic_only:
        logging.debug(f"Dynamic-only stop words ({lang}, top {top_n}): {list(dynamic_only)[:10]}...")
    return base_stops.union(most_common_words)

def check_keyword_in_texts(phrase, url_texts_dict):
    best_match = {"found": False, "url": "-", "score": 0, "preview": "", "keyword": phrase}
    phrase_lower = phrase.lower()
    if not phrase_lower:
        return best_match
    for url, text in url_texts_dict.items():
        if text is None:
            continue
        try:
            index = text.find(phrase_lower)
            if index != -1:
                score = text.count(phrase_lower)
                start = max(0, index - 60)
                end = min(len(text), index + len(phrase_lower) + 60)
                preview_text = re.sub(r'\s+', ' ', text[start:end]).strip()
                if score > best_match["score"]:
                    best_match.update({
                        "found": True,
                        "url": url,
                        "score": score,
                        "preview": f"...{preview_text}..."
                    })
                    # Optional: break here if only first occurrence needed
                    # break
        except Exception as e:
            logging.warning(f"Error searching for '{phrase}' in text from {url}: {e}")
            continue
    return best_match

def cleanup_old_files():
    now = time.time()
    cutoff = now - timedelta(days=CLEANUP_DAYS).total_seconds()
    folders_to_clean = [app.config['UPLOAD_FOLDER'], app.config['RESULTS_FOLDER']]
    cleaned_count = 0
    logging.info(f"Starting cleanup for files older than {CLEANUP_DAYS} day(s)...")
    for folder in folders_to_clean:
        try:
            if not os.path.isdir(folder):
                logging.warning(f"Cleanup folder does not exist, skipping: {folder}")
                continue
            logging.debug(f"Checking folder: {folder}")
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                try:
                    if os.path.isfile(file_path):
                        file_mtime = os.path.getmtime(file_path)
                        if file_mtime < cutoff:
                            os.remove(file_path)
                            cleaned_count += 1
                            logging.info(f"Cleaned up old file: {file_path}")
                except FileNotFoundError:
                    logging.warning(f"File not found during cleanup check: {file_path}")
                except OSError as e:
                    logging.error(f"Error removing file {file_path}: {e}")
                except Exception as e:
                    logging.error(f"Unexpected error processing file {file_path} during cleanup: {e}", exc_info=True)
        except Exception as e:
            logging.error(f"Error listing files during cleanup in {folder}: {e}", exc_info=True)
    if cleaned_count > 0:
        logging.info(f"Cleanup finished. Removed {cleaned_count} old files.")
    else:
        logging.info("Cleanup finished. No old files found to remove.")

# --- Flask Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    start_time = time.time()
    results_data = []
    download_filename = None
    error_message = None
    email = request.form.get('email', '').strip()
    website_url = request.form.get('website', '').strip()  # Use this for rendering back

    if request.method == 'POST':
        file = request.files.get('file')
        temp_path = None

        # --- Improved Validation ---
        validation_errors = []
        if not email or not validators.email(email):
            validation_errors.append("آدرس ایمیل معتبر وارد کنید.")

        submitted_website_url = website_url
        corrected_website_url = website_url
        if not corrected_website_url:
            validation_errors.append("آدرس وب‌سایت را وارد کنید.")
        elif not validators.url(corrected_website_url) and not corrected_website_url.startswith(('http://', 'https://')):
            corrected_website_url = 'https://' + corrected_website_url
            if not validators.url(corrected_website_url):
                validation_errors.append(f"آدرس وب‌سایت نامعتبر: {submitted_website_url}")
        elif not validators.url(corrected_website_url):
            validation_errors.append(f"آدرس وب‌سایت نامعتبر: {submitted_website_url}")

        if not file:
            validation_errors.append("فایل اکسل کلمات کلیدی را انتخاب کنید.")
        elif not file.filename:
            validation_errors.append("فایل اکسل انتخاب نشده است.")
        elif not allowed_file(file.filename):
            validation_errors.append(f"فرمت فایل نامعتبر است. فقط {ALLOWED_EXTENSIONS} مجاز است.")

        if validation_errors:
            error_message = " | ".join(validation_errors)
            logging.warning(f"Validation failed: {error_message}")
            return render_template("index.html", error=error_message, website=submitted_website_url, email=email), 400

        process_url = corrected_website_url

        if not save_user_data(email, process_url):
            logging.warning(f"Continuing process despite failed user data save for {email}")

        try:
            safe_filename = secure_filename(file.filename)
            temp_filename = f"{uuid.uuid4()}{os.path.splitext(safe_filename)[1]}"
            temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
            file.save(temp_path)
            logging.info(f"File uploaded to {temp_path}")

            try:
                df = pd.read_excel(temp_path, engine='openpyxl')
                if df.empty or df.shape[1] == 0:
                    raise ValueError("فایل اکسل خالی است یا ستون اول ندارد.")
                keywords = df.iloc[:, 0].dropna().astype(str).str.strip().unique().tolist()
                keywords = [kw for kw in keywords if kw]
                if not keywords:
                    raise ValueError("هیچ کلمه کلیدی معتبری (غیر خالی) در ستون اول یافت نشد.")
                logging.info(f"Read {len(keywords)} unique, non-empty keywords from {safe_filename}")
            except Exception as e:
                raise ValueError(f"خطا در خواندن یا پردازش فایل اکسل: {e}") from e

            logging.info(f"Starting crawl for: {process_url}")
            crawl_start_time = time.time()
            with requests.Session() as session_crawl:
                urls_to_check = get_internal_urls(process_url, session_crawl)

            if urls_to_check is None:
                raise ConnectionError(f"امکان دسترسی به وب‌سایت اصلی ({process_url}) یا دریافت لینک‌ها وجود ندارد.")
            if not urls_to_check:
                logging.warning(f"No internal URLs found beyond the base for {process_url}. Checking base URL only.")

            logging.info(f"Crawling took {time.time() - crawl_start_time:.2f}s. Found {len(urls_to_check)} URLs.")

            fetch_start_time = time.time()
            url_texts = fetch_and_extract_texts_parallel(urls_to_check)
            logging.info(f"Fetching/extraction took {time.time() - fetch_start_time:.2f}s. Got text from {len(url_texts)} URLs.")

            if not url_texts:
                raise ValueError("هیچ محتوای متنی قابل استخراجی از URL های یافت شده به دست نیامد.")

            combined_text = ' '.join(url_texts.values())
            lang = detect_language(combined_text)
            stop_words = get_dynamic_stop_words(combined_text, lang)
            del combined_text  # Release memory if large
            logging.info(f"Detected language: {lang}. Generated {len(stop_words)} stop words.")

            phrases_to_check = set()
            for kw in keywords:
                kw_lower = kw.lower()
                if kw_lower:
                    phrases_to_check.add(kw_lower)

            # --- Optional: Add individual important words ---
            # important_words_to_check = set()
            # for kw_lower in phrases_to_check.copy():
            #     words_in_kw = re.findall(r'\b\w{3,}\b', kw_lower)
            #     for word in words_in_kw:
            #         if word not in stop_words:
            #             important_words_to_check.add(word)
            # phrases_to_check.update(important_words_to_check)
            # if important_words_to_check:
            #     logging.info(f"Total unique phrases/words to check: {len(phrases_to_check)}")
            # --- End Optional ---

            if not phrases_to_check:
                raise ValueError("پس از پردازش، هیچ کلمه کلیدی معتبری برای بررسی وجود ندارد.")
            logging.info(f"Checking {len(phrases_to_check)} unique phrases/words...")

            check_start_time = time.time()
            temp_results = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS_CHECK, thread_name_prefix='check_kw') as executor:
                future_to_phrase = {
                    executor.submit(check_keyword_in_texts, phrase, url_texts): phrase
                    for phrase in phrases_to_check
                }
                for future in as_completed(future_to_phrase):
                    phrase = future_to_phrase[future]
                    try:
                        result = future.result()
                        temp_results.append(result)
                    except Exception as e:
                        logging.error(f"Error processing result for keyword '{phrase}': {e}", exc_info=True)
                        temp_results.append({"keyword": phrase, "found": False, "url": "Error", "score": 0, "preview": "Processing Error"})

            results_data = sorted(temp_results, key=lambda x: x['keyword'])
            logging.info(f"Keyword checking took {time.time() - check_start_time:.2f} seconds.")

            if results_data:
                try:
                    output_filename = f"results_{uuid.uuid4()}.xlsx"
                    output_path = os.path.join(app.config['RESULTS_FOLDER'], output_filename)
                    output_df = pd.DataFrame(results_data, columns=['keyword', 'found', 'url', 'score', 'preview'])
                    output_df.to_excel(output_path, index=False, engine='openpyxl')
                    download_filename = output_filename
                    logging.info(f"Results file generated: {output_filename}")
                except Exception as e:
                    logging.error(f"Failed to generate results Excel file: {e}", exc_info=True)
                    error_message = (error_message + " | " if error_message else "") + "خطا در تولید فایل نتایج اکسل."
            else:
                logging.warning("Keyword checking yielded no results data.")
                error_message = (error_message + " | " if error_message else "") + "هیچ نتیجه‌ای برای ذخیره در فایل اکسل وجود ندارد."

        except (ValueError, ConnectionError) as e:
            logging.warning(f"Process failed due to input/connection error: {e}")
            error_message = str(e)
        except MemoryError:
            logging.critical("Out of memory during processing!", exc_info=True)
            error_message = "خطای کمبود حافظه رخ داد. ممکن است وب‌سایت یا فایل ورودی بسیار بزرگ باشد."
        except Exception as e:
            logging.error(f"An unexpected error occurred during processing: {e}", exc_info=True)
            error_message = "یک خطای پیش‌بینی نشده در سرور رخ داد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
        finally:
            if temp_path and os.path.isfile(temp_path):
                try:
                    os.remove(temp_path)
                    logging.info(f"Cleaned up temporary upload file: {temp_path}")
                except OSError as e:
                    logging.error(f"Error removing temporary upload file {temp_path}: {e}")

    # --- Render Template ---
    total_time = time.time() - start_time
    if error_message:
        logging.error(f"Request finished with error in {total_time:.2f}s. Error: {error_message}")
    elif request.method == 'POST':  # Log success only for POST requests that didn't error out
        logging.info(f"Request processed successfully in {total_time:.2f}s.")

    # Determine website URL to render back in the form
    render_website_url = website_url  # Use value from GET or top of function by default
    if request.method == 'POST' and 'submitted_website_url' in locals():
        render_website_url = submitted_website_url  # Use original submitted value if POST failed validation

    return render_template("index.html",
                           results=results_data,
                           download_filename=download_filename,
                           error=error_message,
                           website=render_website_url,
                           email=email)

@app.route('/download/<filename>')
def download(filename):
    safe_filename = secure_filename(filename)
    if not safe_filename or safe_filename != filename:
        logging.warning(f"Download attempt with potentially unsafe filename rejected: '{filename}' -> '{safe_filename}'")
        abort(400)
    file_path = os.path.join(app.config['RESULTS_FOLDER'], safe_filename)
    try:
        if not os.path.isfile(file_path):
            logging.warning(f"Download request for non-existent or non-file path: {file_path}")
            abort(404)
        return send_file(file_path, as_attachment=True)
    except Exception as e:
        logging.error(f"Error during file download of {safe_filename}: {e}", exc_info=True)
        abort(500)

# --- Main Execution ---
if __name__ == '__main__':
    try:
        init_db()
    except Exception as e:
        logging.critical(f"CRITICAL: Database could not be initialized. Exiting. Error: {e}", exc_info=True)
        exit(1)

    cleanup_old_files()
    logging.info("Starting Flask development server...")
    # Important: Set debug=False for production environments
    app.run(debug=False, host='0.0.0.0', port=5000)
