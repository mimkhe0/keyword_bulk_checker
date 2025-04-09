# -*- coding: utf-8 -*-
import os
import re
import uuid
import logging
import magic
import datetime # برای context processor
from typing import List, Dict, Tuple, Optional, Set, Union
import pandas as pd
from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename

# --- Selenium Imports (Standard) ---
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, WebDriverException, NoSuchElementException,
    ElementNotVisibleException, SessionNotCreatedException # SessionNotCreatedException اضافه شد
)
# ------------------------------------

from email_validator import validate_email, EmailNotValidError
from dotenv import load_dotenv # برای خواندن .env

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_FOLDER = os.path.join(BASE_DIR, 'instance')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')
ALLOWED_EXTENSIONS: Set[str] = {'.xlsx'}
SELENIUM_TIMEOUT = 30
MAX_FILE_SIZE = 10 * 1024 * 1024 # 10 MB
SNIPPET_CONTEXT_LENGTH = 50
# مسیر پیش‌فرض برای درایور کروم که دستی نصب شده
CHROME_DRIVER_PATH = "/usr/local/bin/chromedriver"

# --- Stop Words ---
# (لیست‌های Stop Words مثل قبل اینجا قرار دارند - برای اختصار حذف شد)
STOP_WORDS_ENGLISH: Set[str] = { ... } # لیست کلمات انگلیسی
STOP_WORDS_PERSIAN: Set[str] = { ... } # لیست کلمات فارسی
STOP_WORDS_ARABIC: Set[str] = { ... } # لیست کلمات عربی
ALL_STOP_WORDS: Set[str] = STOP_WORDS_ENGLISH.union(STOP_WORDS_PERSIAN).union(STOP_WORDS_ARABIC)

# --- Folder Setup ---
for folder in [UPLOAD_FOLDER, RESULTS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- Flask App ---
app = Flask(__name__, instance_path=INSTANCE_FOLDER)
app.config.update(
    UPLOAD_FOLDER=UPLOAD_FOLDER,
    RESULTS_FOLDER=RESULTS_FOLDER,
    MAX_CONTENT_LENGTH=MAX_FILE_SIZE,
    # خواندن کلید مخفی از متغیر محیطی که توسط .env فایل تنظیم شده
    SECRET_KEY=os.environ.get('FLASK_SECRET_KEY', 'a_very_weak_default_secret_key_change_me')
)

# --- Logging ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(process)d - %(thread)d - %(message)s'
)
logging.getLogger('selenium').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# --- Jinja Context Processor ---
@app.context_processor
def inject_now():
    """Injects current time (UTC) into templates."""
    return {'now': datetime.datetime.utcnow()}

# --- Helper Functions ---

def allowed_file(filename: str) -> bool:
    """Checks if the file extension is allowed."""
    return '.' in filename and os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def is_valid_excel(filepath: str) -> bool:
    """Validates if the file is an actual Excel file using MIME type."""
    try:
        mime = magic.Magic(mime=True)
        mime_type = mime.from_file(filepath)
        return mime_type in [
            'application/vnd.ms-excel',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        ]
    except Exception as e:
        logging.error(f"Magic library error checking file {filepath}: {e}")
        return os.path.splitext(filepath)[1].lower() in ALLOWED_EXTENSIONS

def validate_url(url: str) -> str:
    """Validates the URL and ensures it starts with http:// or https://"""
    url = url.strip()
    if not re.match(r'^(http|https)://', url):
        if '.' in url.split('/')[-1]:
             return 'https://' + url
        else:
             return 'https://' + url # Or handle error better
    return url

def validate_email_address(email: str) -> bool:
    """Validates the email format."""
    try:
        validate_email(email, check_deliverability=False)
        return True
    except EmailNotValidError as e:
        logging.warning(f"Invalid email format: {email} - {e}")
        return False

def fetch_page_text(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetches and cleans text content from a URL using Standard Selenium.
    Returns (page_text, error_message)
    """
    driver = None
    logging.info(f"Attempting to fetch URL using standard Selenium: {url}")
    try:
        chrome_options = Options()
        # --- ضروری‌ترین آپشن‌ها ---
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        # --- آپشن‌های مفید دیگر ---
        chrome_options.add_argument('--disable-dev-shm-usage') # مهم در محیط‌های داکر/لینوکس
        chrome_options.add_argument('--disable-gpu') # معمولا برای headless لازم است
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36") # User agent استاندارد
        chrome_options.add_argument('--blink-settings=imagesEnabled=false') # غیرفعال کردن تصاویر برای سرعت

        # --- تعریف سرویس با مسیر درایور دستی ---
        # مطمئن شوید CHROME_DRIVER_PATH مسیر صحیح درایور روی سرور است
        service = ChromeService(executable_path=CHROME_DRIVER_PATH)

        # --- مقداردهی اولیه درایور استاندارد Chrome ---
        logging.info(f"Initializing Chrome driver with service path: {CHROME_DRIVER_PATH}")
        driver = Chrome(service=service, options=chrome_options)
        logging.info("Chrome driver initialized successfully.")

        driver.set_page_load_timeout(SELENIUM_TIMEOUT)
        driver.set_script_timeout(SELENIUM_TIMEOUT)

        driver.get(url)

        WebDriverWait(driver, SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        html = driver.page_source

        if not html:
            logging.warning(f"No HTML content retrieved from {url}")
            return None, "محتوایی از صفحه دریافت نشد."

        # --- Text Cleaning ---
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'', ' ', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip().lower()

        logging.info(f"Successfully fetched and cleaned text from {url}. Length: {len(text)}")
        return text, None

    # --- مدیریت خطاهای Selenium ---
    except TimeoutException as e:
        logging.error(f"Timeout error fetching {url}: {e}", exc_info=True)
        return None, f"خطای وقفه زمانی (Timeout) هنگام بارگذاری صفحه: {url}"
    except SessionNotCreatedException as e:
         logging.error(f"SessionNotCreatedException fetching {url}: {e}", exc_info=True)
         # This often means incompatibility between ChromeDriver and Chrome Browser version
         return None, f"خطا در ایجاد نشست مرورگر: احتمالاً نسخه ChromeDriver با نسخه مرورگر Chrome سازگار نیست یا درایور قابل اجرا نیست. (مسیر: {CHROME_DRIVER_PATH})"
    except WebDriverException as e:
        logging.error(f"WebDriver error fetching {url}: {e}", exc_info=True)
        if "net::ERR_NAME_NOT_RESOLVED" in str(e) or "dns error" in str(e).lower():
             return None, f"آدرس وب‌سایت نامعتبر یا در دسترس نیست: {url}"
        if "unable to connect" in str(e).lower():
             return None, f"امکان برقراری ارتباط با وب‌سایت وجود ندارد: {url}"
        # Check if chromedriver failed to start
        if "failed to start" in str(e).lower() or "terminated unexpectedly" in str(e).lower():
             return None, f"فایل ChromeDriver قابل اجرا نیست یا در مسیر مشخص شده یافت نشد. (مسیر: {CHROME_DRIVER_PATH})"
        return None, f"خطای مرورگر (WebDriver) هنگام دسترسی به صفحه: {e}"
    except (NoSuchElementException, ElementNotVisibleException) as e:
         logging.error(f"Element error on {url}: {e}", exc_info=True)
         return None, f"خطا در یافتن المان‌های ضروری صفحه: {e}"
    except Exception as e:
        # Catch unexpected errors, including potential permission errors if driver isn't executable
        logging.error(f"Unexpected error fetching {url}: {e}", exc_info=True)
        if isinstance(e, PermissionError):
             return None, f"خطای دسترسی: فایل ChromeDriver اجازه اجرا ندارد. (مسیر: {CHROME_DRIVER_PATH})"
        return None, f"خطای پیش‌بینی نشده در دریافت اطلاعات صفحه: {e}"
    finally:
        if driver:
            try:
                driver.quit()
                logging.info(f"Selenium driver quit for {url}")
            except Exception as e:
                logging.error(f"Error quitting driver for {url}: {e}")

def get_phrases_from_file(filepath: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Reads phrases (one per row) from the first column of an Excel file."""
    try:
        df = pd.read_excel(filepath, header=None, engine='openpyxl')
        if df.empty or df.shape[1] == 0:
            return None, "فایل اکسل خالی است یا ستون اول وجود ندارد."
        phrases = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
        valid_phrases = [p for p in phrases if p]
        if not valid_phrases:
             return None, "هیچ عبارت معتبری در ستون اول فایل اکسل یافت نشد."
        logging.info(f"Read {len(valid_phrases)} phrases from {filepath}")
        return valid_phrases, None
    except FileNotFoundError:
        logging.error(f"Excel file not found at path: {filepath}")
        return None, f"فایل اکسل یافت نشد: {filepath}"
    except pd.errors.EmptyDataError:
        logging.warning(f"Excel file is empty: {filepath}")
        return None, "فایل اکسل خالی است."
    except Exception as e:
        logging.error(f"Error reading Excel file {filepath}: {e}", exc_info=True)
        return None, f"خطا در خواندن یا پردازش فایل اکسل: {e}"

def get_context_snippet(text: str, phrase: str, context_len: int = SNIPPET_CONTEXT_LENGTH) -> Optional[str]:
    """Extracts a snippet of text around the first occurrence of the phrase."""
    try:
        matches = list(re.finditer(re.escape(phrase), text, re.IGNORECASE))
        if not matches:
            return None
        match = matches[0]
        start, end = match.span()
        snippet_start = max(0, start - context_len)
        snippet_end = min(len(text), end + context_len)
        prefix = "..." if snippet_start > 0 else ""
        suffix = "..." if snippet_end < len(text) else ""
        snippet = text[snippet_start:snippet_end]
        # Simple highlight - replace first occurrence case-insensitively within snippet
        # Use regex for case-insensitive replace within the snippet only
        try:
            highlighted_snippet = re.sub(re.escape(phrase), r"**\g**", snippet, 1, re.IGNORECASE)
        except re.error:
             highlighted_snippet = snippet # Fallback if regex fails

        return f"{prefix}{highlighted_snippet}{suffix}"

    except Exception as e:
        logging.error(f"Error generating snippet for phrase '{phrase}': {e}", exc_info=True)
        return "[خطا در ایجاد پیش‌نمایش]"

def analyze_phrases_in_text(
    phrases: List[str],
    page_text: str,
    url: str
) -> List[Dict[str, Union[str, int, bool, Dict, List[str], None]]]:
    """Analyzes phrases against page text (exact phrase matching)."""
    results = []
    if not page_text:
        logging.warning(f"Page text is empty for URL {url}. Skipping analysis.")
        for phrase in phrases:
             words_in_phrase = re.findall(r'\b\w+\b', phrase.lower().strip())
             important_terms = [word for word in words_in_phrase if word not in ALL_STOP_WORDS]
             results.append({
                'original_phrase': phrase,
                'phrase_to_find': phrase.lower().strip(),
                'found_phrase_count': 0,
                'important_terms': important_terms,
                'total_score': 0,
                'found_phrase': False,
                'analysis_notes': "محتوای صفحه وب قابل دریافت یا خالی بود.",
                'url': url,
                'context_snippet': None
            })
        return results

    logging.info(f"Analyzing {len(phrases)} phrases against text of length {len(page_text)} from {url}")

    for phrase in phrases:
        if not phrase or not phrase.strip():
            logging.warning("Skipping empty phrase.")
            continue

        original_phrase = phrase
        phrase_lower = phrase.strip().lower()

        words_in_phrase = re.findall(r'\b\w+\b', phrase_lower)
        important_terms = [word for word in words_in_phrase if word not in ALL_STOP_WORDS]

        try:
            escaped_phrase = re.escape(phrase_lower)
            matches = list(re.finditer(escaped_phrase, page_text, re.IGNORECASE))
            phrase_count = len(matches)
        except re.error as e:
             logging.error(f"Regex error searching for phrase '{phrase_lower}': {e}")
             phrase_count = 0
             matches = []

        total_score = phrase_count
        context_snippet = None
        if phrase_count > 0 and matches:
            context_snippet = get_context_snippet(page_text, phrase_lower, SNIPPET_CONTEXT_LENGTH)

        found_phrase = phrase_count > 0
        analysis_notes = None if found_phrase else "عبارت مورد نظر در متن صفحه یافت نشد."

        results.append({
            'original_phrase': original_phrase,
            'phrase_to_find': phrase_lower,
            'found_phrase_count': phrase_count,
            'important_terms': important_terms,
            'total_score': total_score,
            'found_phrase': found_phrase,
            'analysis_notes': analysis_notes,
            'url': url,
            'context_snippet': context_snippet
        })

    logging.info(f"Analysis complete for {url}. Found matches for {sum(1 for r in results if r['found_phrase'])} out of {len(phrases)} phrases.")
    return results

def generate_excel_report(results: List[Dict], url_checked: str) -> Optional[str]:
    """Generates an Excel report from results and saves it."""
    if not results:
        logging.warning("No results to generate report.")
        return None

    report_data = []
    for res in results:
        important_terms_str = ', '.join(res.get('important_terms', [])) if res.get('important_terms') else "-"
        analysis_notes = res.get('analysis_notes', "-") if res.get('analysis_notes') else "-"

        report_data.append({
            'Original Phrase': res.get('original_phrase', 'N/A'),
            'Phrase Searched (Lowercase)': res.get('phrase_to_find', 'N/A'),
            'Important Terms (Non-StopWords)': important_terms_str,
            'Times Found': res.get('found_phrase_count', 0),
            'Score (Phrase Count)': res.get('total_score', 0),
            'Phrase Found?': 'Yes' if res.get('found_phrase', False) else 'No',
            'Analysis Notes': analysis_notes,
            'Context Snippet': res.get('context_snippet', 'N/A'),
        })

    df = pd.DataFrame(report_data)
    output_name = f"analysis_report_{uuid.uuid4().hex}.xlsx"
    output_path = os.path.join(app.config['RESULTS_FOLDER'], output_name)

    try:
        df.to_excel(output_path, index=False, engine='openpyxl')
        logging.info(f"Generated Excel report: {output_path} for URL: {url_checked}")
        return output_name
    except Exception as e:
        logging.error(f"Failed to generate Excel report {output_path}: {e}", exc_info=True)
        return None

# --- Flask Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    """Handles file upload, website input, processing, and displaying results."""
    results_summary: Optional[List[Dict]] = None
    error: Optional[str] = None
    download_filename: Optional[str] = None
    processing_done: bool = False
    email: str = request.form.get('email', '').strip()
    website: str = request.form.get('website', '').strip()

    if request.method == 'POST':
        logging.info(f"POST request received. Email: {'Provided' if email else 'Missing'}, Website: {'Provided' if website else 'Missing'}")
        if 'file' not in request.files or not request.files['file'].filename:
            error = "لطفا فایل اکسل حاوی عبارات کلیدی را انتخاب کنید."
            logging.warning("File not provided in POST request.")
        elif not email:
            error = "لطفا ایمیل خود را وارد کنید."
            logging.warning("Email not provided in POST request.")
        elif not validate_email_address(email):
             error = "فرمت ایمیل وارد شده صحیح نیست."
        elif not website:
            error = "لطفا آدرس وب‌سایت را وارد کنید."
            logging.warning("Website not provided in POST request.")
        else:
            file = request.files['file']
            if not allowed_file(file.filename):
                error = "فرمت فایل مجاز نیست. لطفا فایل اکسل با پسوند .xlsx آپلود کنید."
                logging.warning(f"Disallowed file extension: {file.filename}")

        if not error:
            website = validate_url(website)
            logging.info(f"Validated inputs. Processing website: {website}")
            filepath = None
            unique_upload_name = None
            try:
                filename = secure_filename(file.filename)
                unique_upload_name = f"{uuid.uuid4().hex}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_upload_name)
                file.save(filepath)
                logging.info(f"File saved temporarily to {filepath}")

                if not is_valid_excel(filepath):
                    raise ValueError("فایل آپلود شده یک فایل اکسل معتبر نیست.")

                phrases, phrases_error = get_phrases_from_file(filepath)
                if phrases_error:
                    raise ValueError(f"خطا در پردازش فایل عبارات: {phrases_error}")

                logging.info(f"Fetching text content for {website}...")
                page_text, fetch_error = fetch_page_text(website)
                if fetch_error:
                    # Raise error with specific message from fetch_page_text
                    raise ConnectionError(f"{fetch_error}")

                logging.info(f"Analyzing {len(phrases)} phrases...")
                analysis_results = analyze_phrases_in_text(phrases, page_text, website)

                if analysis_results:
                    logging.info("Generating Excel report...")
                    download_filename = generate_excel_report(analysis_results, website)
                    if not download_filename:
                         raise RuntimeError("خطا در تولید فایل گزارش اکسل.")
                    results_summary = analysis_results
                else:
                     error = "تحلیلی انجام نشد یا نتیجه‌ای حاصل نشد."
                     logging.warning("Analysis returned no results.")

                processing_done = True
                logging.info(f"Processing finished successfully for {website}.")

            except (ValueError, ConnectionError, RuntimeError, PermissionError) as e: # Added PermissionError
                error = str(e)
                logging.error(f"Processing error: {error}", exc_info=False)
            except Exception as e:
                error = f"یک خطای پیش‌بینی نشده رخ داد: {e}"
                logging.error("Unexpected error during processing:", exc_info=True)
            finally:
                if filepath and os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        logging.info(f"Cleaned up uploaded file: {filepath}")
                    except OSError as e:
                        logging.error(f"Error removing uploaded file {filepath}: {e}")

    return render_template(
        "index.html",
        results=results_summary,
        error=error,
        download_filename=download_filename,
        processing_done=processing_done,
        email=email,
        website=website
    )

@app.route('/download/<filename>')
def download(filename: str):
    """Provides the generated analysis Excel file for download."""
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
         logging.warning(f"Download attempt with potentially unsafe filename: {filename}")
         return "نام فایل نامعتبر است.", 400

    path = os.path.join(app.config['RESULTS_FOLDER'], safe_name)
    logging.info(f"Download request for: {path}")

    if not os.path.isfile(path):
        logging.error(f"Download failed: File not found at {path}")
        return "فایل مورد نظر یافت نشد.", 404

    try:
        return send_file(path, download_name=safe_name, as_attachment=True)
    except Exception as e:
         logging.error(f"Error sending file {path} for download: {e}", exc_info=True)
         return "خطا در ارسال فایل.", 500

# --- Main Execution Guard ---
if __name__ == '__main__':
    # For development only:
    # app.run(debug=True, host='0.0.0.0', port=5000)

    # For production using Waitress (recommended with Systemd):
    # Make sure waitress is installed: pip install waitress
    # The Systemd service file should execute waitress, e.g.:
    # ExecStart=/path/to/venv/bin/waitress-serve --host 127.0.0.1 --port 5000 app:app
    # DO NOT run with app.run(debug=True) in production!
    pass # In production, Systemd/Waitress will run the app, not this block.
