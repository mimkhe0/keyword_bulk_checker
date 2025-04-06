# -*- coding: utf-8 -*-
# (Ensure encoding is specified, especially with Persian literals)
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
MAX_FILE_SIZE = 10 * 1024 * 1024 # 10MB
ALLOWED_EXTENSIONS = {'.xlsx'}
CLEANUP_DAYS = 1

# --- Flask App Setup ---
app = Flask(__name__)
# Ensure the template folder is correctly configured if not in default location
# app = Flask(__name__, template_folder='path/to/templates')
app.config['INSTANCE_FOLDER'] = INSTANCE_FOLDER
app.config['DATABASE'] = DATABASE
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['RESULTS_FOLDER'] = RESULTS_FOLDER

# Create necessary directories if they don't exist
os.makedirs(INSTANCE_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESULTS_FOLDER, exist_ok=True)

# Setup logging
# Use RotatingFileHandler for production to manage log file size
# from logging.handlers import RotatingFileHandler
# log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(process)d:%(threadName)s] - %(module)s - %(message)s')
# log_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5) # 10MB per file, 5 backups
# log_handler.setFormatter(log_formatter)
# app.logger.addHandler(log_handler) # Use Flask's logger
# app.logger.setLevel(logging.INFO)
# logging.getLogger().handlers.clear() # Remove default basicConfig handler if used elsewhere
# logging.getLogger().addHandler(log_handler) # Also configure root logger if needed

# Simple file logging for now
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    # Using threadName instead of thread ID for better readability
    format='%(asctime)s - %(levelname)s - [%(process)d:%(threadName)s] - %(module)s - %(message)s'
)

# Reduce verbosity of external libraries
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.INFO) # Log Werkzeug requests

# --- Database Functions ---
def get_db():
    """Gets the SQLite database connection for the current context."""
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
             # Depending on requirements, maybe abort or raise
             abort(500, description="Database connection error.")
    return db

@app.teardown_appcontext
def close_connection(exception):
    """Closes the database connection at the end of the request."""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()
    # Log exceptions passed during teardown
    # if exception:
    #     app.logger.error(f"Exception during request teardown: {exception}")


def init_db():
    """Initializes the database and creates the users table if needed."""
    try:
        # Use app_context to ensure Flask context is available
        with app.app_context():
            db = get_db()
            # Use 'with db:' for automatic transaction handling (commit/rollback)
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
        # This is critical, re-raise to potentially stop the app
        raise

def save_user_data(email, website):
    """Saves user email and website to the database."""
    sql = 'INSERT INTO users (email, website) VALUES (?, ?)'
    try:
        db = get_db()
        with db:
            db.execute(sql, (email, website))
        logging.info(f"User data saved: {email}, {website}")
        return True
    except sqlite3.IntegrityError:
        # Handle potential unique constraint violations if added later
        logging.warning(f"Attempt to save duplicate or invalid user data: {email}, {website}")
        # Decide if this should be reported as failure
        return True # Assume logging is sufficient for now
    except sqlite3.Error as e:
        logging.error(f"Failed to save user data for {email}: {e}", exc_info=True)
        return False # Indicate failure


# --- Helper Functions ---
def allowed_file(filename):
    """Checks if the file extension is allowed."""
    return '.' in filename and \
           os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_url(url, session):
    """Extracts text content from a URL, handling common errors."""
    try:
        # Use stream=True for potentially large files, though less common for HTML
        # Consider response.content instead of response.text if encoding issues arise
        with session.get(url, timeout=TIMEOUT_PER_URL, allow_redirects=True) as response:
            response.raise_for_status() # Check for 4xx/5xx errors
            content_type = response.headers.get('content-type', '').lower()
            if 'html' not in content_type:
                logging.debug(f"Skipping non-HTML content at {url} (Type: {content_type})")
                return url, None

            # Use lxml for potentially faster parsing
            soup = BeautifulSoup(response.content, 'lxml') # Use response.content for better encoding handling

            # Remove common irrelevant tags
            for element in soup(['script', 'style', 'footer', 'nav', 'form', 'head', 'header', 'aside', 'noscript', 'link', 'meta']):
                element.decompose()

            # Extract text, normalize whitespace, convert to lower
            text = soup.get_text(separator=' ', strip=True).lower()
            text = re.sub(r'\s+', ' ', text).strip()
            return url, text

    # More specific exception handling for better logging
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
        # Catch other requests-related errors (ConnectionError, etc.)
        logging.warning(f"Request failed for {url}: {type(e).__name__}")
        return url, None
    except Exception as e:
        # Catch-all for unexpected errors during processing (e.g., BeautifulSoup errors)
        logging.error(f"Unexpected error processing {url}: {e}", exc_info=True)
        return url, None


def fetch_and_extract_texts_parallel(urls):
    """Fetches and extracts text from multiple URLs concurrently."""
    url_texts = {}
    # Use a consistent, identifiable User-Agent
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; KeywordCheckerBot/1.1; +http://example.com/bot)'} # TODO: Replace with actual bot info URL

    with requests.Session() as session:
        session.headers.update(headers)
        # Adjust max_workers based on system resources and external service limits
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_FETCH, thread_name_prefix='fetch_url') as executor:
            # Create futures
            future_to_url = {executor.submit(extract_text_from_url, url, session): url for url in urls}

            # Process completed futures
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    # result() will re-raise exceptions caught in extract_text_from_url
                    res_url, text = future.result()
                    if text is not None: # Add even if empty string, but not if None (error)
                        url_texts[res_url] = text
                except Exception as e:
                    # Log errors from the future execution itself OR re-raised from function
                    logging.error(f"Future execution or text extraction failed for {url}: {e}", exc_info=False) # Keep log concise
    logging.info(f"Successfully extracted text from {len(url_texts)} out of {len(urls)} URLs.")
    return url_texts


def get_internal_urls(base_url, session):
    """Finds internal URLs up to MAX_URLS_TO_FETCH."""
    urls = set()
    if not base_url.startswith(('http://', 'https://')):
        base_url = 'https://' + base_url # Default to HTTPS

    normalized_base_url = base_url.rstrip('/')
    # Consider adding both http and https versions if scheme wasn't specified?
    urls.add(normalized_base_url) # Add the base URL itself

    try:
        with session.get(normalized_base_url, timeout=10) as response:
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').lower()
            if 'html' not in content_type:
                logging.warning(f"Base URL {normalized_base_url} is not HTML. Cannot crawl links.")
                return list(urls) # Return just the base URL

            soup = BeautifulSoup(response.content, 'lxml')
            for a_tag in soup.select('a[href]'):
                href = a_tag.get('href')
                # Skip empty, mailto, tel, javascript links
                if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
                    continue

                # Resolve relative URLs, remove fragments, normalize trailing slash
                try:
                    full_url = urljoin(response.url, href).split('#')[0].rstrip('/') # Use response.url for accurate base after redirects
                except ValueError:
                     logging.debug(f"Could not parse href '{href}' relative to {response.url}")
                     continue

                # Check if internal and looks like a valid URL structure
                # Be careful with subdomain checks if needed (e.g., www vs non-www)
                if full_url.startswith(normalized_base_url) and validators.url(full_url):
                    urls.add(full_url)
                    if len(urls) >= MAX_URLS_TO_FETCH:
                        logging.info(f"Reached MAX_URLS_TO_FETCH limit ({MAX_URLS_TO_FETCH}).")
                        break
        logging.info(f"Found {len(urls)} internal URLs starting from {normalized_base_url}")
        return list(urls)

    except requests.exceptions.Timeout:
        logging.error(f"Timeout fetching base URL {normalized_base_url}")
        return None # Indicate critical failure
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to get base URL {normalized_base_url}: {e}", exc_info=False) # Log exception type is often enough
        return None
    except Exception as e: # Catch potential BS4 or other errors
        logging.error(f"Unexpected error crawling {normalized_base_url}: {e}", exc_info=True)
        return None


def detect_language(text_sample):
    """Basic language detection (fa_ar or en)."""
    if not text_sample:
        return 'en'
    # Use a more comprehensive regex range for Arabic/Persian script variants
    if re.search(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', text_sample):
        return 'fa_ar'
    return 'en'


def get_dynamic_stop_words(text, lang, top_n=50):
    """Generates stop words combining a base list and most frequent words."""
    # \b ensures word boundaries, \w+ finds sequences of alphanumeric characters
    # {3,} ensures words are at least 3 chars long - adjust if needed
    words = re.findall(r'\b\w{3,}\b', text.lower())
    if not words:
        return set()

    word_counts = Counter(words)
    most_common_words = {word for word, count in word_counts.most_common(top_n)}

    # *** Using the more comprehensive stop word lists ***
    general_stop_words = {
        'en': {
            'the', 'a', 'an', 'and', 'in', 'of', 'for', 'with', 'to', 'from', 'by', 'is', 'are', 'on', 'at',
            'it', 'this', 'that', 'or', 'as', 'was', 'were', 'be', 'been', 'have', 'has', 'had', 'but', 'not',
            'you', 'your', 'we', 'our', 'us', 'he', 'she', 'they', 'them', 'their', 'will', 'can', 'could',
            'would', 'should', 'also', 'more', 'most', 'all', 'any', 'some', 'other', 'about', 'out', 'up',
            'down', 'into', 'through', 'over', 'under', 'again', 'further', 'then', 'once', 'here', 'there',
            'when', 'where', 'why', 'how', 'which', 'who', 'whom', 'whose', 'its', 'etc', 'eg', 'ie',
            'http', 'https', 'www', 'com', 'org', 'net', 'gov', 'edu', 'html', 'pdf', 'inc', 'corp', 'ltd', 'co',
             # Common website terms
            'home', 'contact', 'search', 'menu', 'page', 'click', 'news', 'events', 'about', 'privacy', 'terms',
            'login', 'logout', 'register', 'account', 'profile', 'settings', 'admin', 'dashboard', 'help', 'faq',
            'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'
        },
        'fa_ar': {
            # Persian common words
            'و', 'در', 'به', 'از', 'که', 'این', 'آن', 'است', 'را', 'با', 'برای', 'تا', 'یا', 'هم', 'نیز', 'شد',
            'شده', 'شود', 'شوند', 'بود', 'کرد', 'کند', 'کنند', 'کنید', 'کردن', 'های', 'هایی', 'ای', 'یک', 'دو', 'سه', 'بر', 'هر',
            'همه', 'وی', 'او', 'ما', 'شما', 'ایشان', 'آنها', 'خود', 'دیگر', 'ولی', 'اما', 'اگر', 'پس', 'چون',
            'حتی', 'فقط', 'باید', 'نباید', 'باشد', 'نباشد', 'آیا', 'کدام', 'چه', 'چگونه', 'کجا', 'کی', 'چیست', 'واقع',
            # Prepositions & Conjunctions
            'ضمن', 'بین', 'روی', 'زیر', 'بالای', 'کنار', 'مقابل', 'طبق', 'مانند', 'مثل', 'حدود', 'طی', 'طریق', 'نسبت',
            # Arabic common words often mixed in Persian text
            'علیه', 'علی', 'الا', 'فی', 'کل', 'غیر', 'ذلک', 'من', 'الی', 'عن', 'حتیٰ', 'اذا', 'لم', 'لن', 'کان', 'لیس', 'قد', 'ما', 'هو', 'هی',
            # Common suffixes/prefixes often found as separate words by regex
             'ها', 'تر', 'ترین', 'می', 'نمی', 'بی', 'پیش', 'پس',
            # Common website/context words
            'میلادی', 'شمسی', 'هجری', 'سال', 'ماه', 'روز', 'شنبه', 'یکشنبه', 'دوشنبه', 'سهشنبه', 'چهارشنبه', 'پنجشنبه', 'جمعه', 'ساعت', 'دقیقه', 'ثانیه',
            'شرکت', 'سازمان', 'اداره', 'موسسه', 'دانشگاه', 'گروه', 'بخش', 'واحد', 'مرکز', 'انجمن', 'تیم', 'معاونت', 'مدیریت',
            'صفحه', 'اصلی', 'تماس', 'با', 'ما', 'درباره', 'محصولات', 'خدمات', 'اخبار', 'مقالات', 'پروفایل', 'کاربری', 'ورود', 'خروج', 'ثبت', 'نام',
            'جستجو', 'دانلود', 'ارسال', 'نظر', 'نظرات', 'پاسخ', 'گالری', 'تصاویر', 'ویدئو', 'اطلاعات', 'بیشتر', 'ادامه', 'مطلب', 'قبلی', 'بعدی',
            'اول', 'آخر', 'نقشه', 'سایت', 'کپی', 'رایت', 'طراحی', 'توسعه', 'توسط'
        }
    }
    base_stops = general_stop_words.get(lang, set())
    # Log dynamic words separately for analysis if needed
    dynamic_only = most_common_words - base_stops
    if dynamic_only:
        logging.debug(f"Dynamic-only stop words ({lang}, top {top_n}): {list(dynamic_only)[:10]}...") # Log sample

    # Combine base list with most frequent words
    return base_stops.union(most_common_words)


def check_keyword_in_texts(phrase, url_texts_dict):
    """Checks for a phrase in the pre-fetched texts, returning the best match."""
    best_match = {"found": False, "url": "-", "score": 0, "preview": "", "keyword": phrase}
    phrase_lower = phrase.lower()
    if not phrase_lower: # Skip empty phrases if they somehow get here
        return best_match

    for url, text in url_texts_dict.items():
        if text is None: # Skip URLs where text extraction failed
            continue
        try:
            # Use find for index, count for score
            # Consider using regex for more complex matching (e.g., word boundaries) if needed:
            # matches = list(re.finditer(r'\b' + re.escape(phrase_lower) + r'\b', text))
            # if matches:
            #     score = len(matches)
            #     index = matches[0].start()
            # else:
            #     continue
            index = text.find(phrase_lower)
            if index != -1:
                score = text.count(phrase_lower)
                # Generate preview snippet
                start = max(0, index - 60)
                end = min(len(text), index + len(phrase_lower) + 60)
                # Clean up preview text slightly (normalize whitespace)
                preview_text = re.sub(r'\s+', ' ', text[start:end]).strip()

                # Update best match if this score is higher
                # Could also prioritize based on URL structure (e.g., shorter paths) or other heuristics
                if score > best_match["score"]:
                    best_match.update({
                        "found": True,
                        "url": url,
                        "score": score,
                        "preview": f"...{preview_text}..." # Add ellipses for context
                    })
                    # Optimization: If only the *first* match found anywhere is needed, uncomment break
                    # break
        except Exception as e:
            # Log errors during the search process itself (e.g., regex error if used)
            logging.warning(f"Error searching for '{phrase}' in text from {url}: {e}")
            continue # Continue with next URL

    return best_match


def cleanup_old_files():
    """Removes old files from upload and results folders."""
    now = time.time()
    # Use timedelta for clearer time calculation
    cutoff = now - timedelta(days=CLEANUP_DAYS).total_seconds()
    folders_to_clean = [app.config['UPLOAD_FOLDER'], app.config['RESULTS_FOLDER']]
    cleaned_count = 0
    logging.info(f"Starting cleanup for files older than {CLEANUP_DAYS} day(s)...")

    for folder in folders_to_clean:
        try:
            # Check if folder exists before listing
            if not os.path.isdir(folder):
                logging.warning(f"Cleanup folder does not exist, skipping: {folder}")
                continue
            logging.debug(f"Checking folder: {folder}")
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                try:
                    # Check if it's a file before getting mtime
                    if os.path.isfile(file_path):
                        file_mtime = os.path.getmtime(file_path)
                        if file_mtime < cutoff:
                            os.remove(file_path)
                            cleaned_count += 1
                            logging.info(f"Cleaned up old file: {file_path}")
                except FileNotFoundError:
                     logging.warning(f"File not found during cleanup check (possibly deleted concurrently): {file_path}")
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
    # Use Flask's logger for consistency within the app context
    # app.logger.info(f"Request received: {request.method} {request.path}") # Example
    start_time = time.time()
    results_data = []
    download_filename = None
    error_message = None
    # Get form data regardless of method for template rendering consistency
    email = request.form.get('email', '').strip()
    website_url = request.form.get('website', '').strip() # Use this for rendering back

    if request.method == 'POST':
        file = request.files.get('file')
        temp_path = None # Initialize temp_path to None

        # --- Improved Validation ---
        validation_errors = []
        if not email or not validators.email(email):
            validation_errors.append("آدرس ایمیل معتبر وارد کنید.")

        # Keep the originally submitted URL for display if validation fails
        submitted_website_url = website_url
        corrected_website_url = website_url # URL potentially modified with scheme
        if not corrected_website_url:
            validation_errors.append("آدرس وب‌سایت را وارد کنید.")
        # Try adding scheme only if validation fails initially *and* no scheme exists
        elif not validators.url(corrected_website_url) and not corrected_website_url.startswith(('http://', 'https://')):
            corrected_website_url = 'https://' + corrected_website_url # Default to https
            # Re-validate after adding scheme
            if not validators.url(corrected_website_url):
                validation_errors.append(f"آدرس وب‌سایت نامعتبر: {submitted_website_url}")
        elif not validators.url(corrected_website_url): # Already had scheme, but still invalid
             validation_errors.append(f"آدرس وب‌سایت نامعتبر: {submitted_website_url}")

        if not file: # Check if file object exists first
             validation_errors.append("فایل اکسل کلمات کلیدی را انتخاب کنید.")
        elif not file.filename: # Check if a file was actually selected
            validation_errors.append("فایل اکسل انتخاب نشده است.")
        elif not allowed_file(file.filename):
            validation_errors.append(f"فرمت فایل نامعتبر است. فقط {ALLOWED_EXTENSIONS} مجاز است.")

        if validation_errors:
            error_message = " | ".join(validation_errors)
            # Return early, passing back ORIGINAL submitted values for form persistence
            logging.warning(f"Validation failed: {error_message}")
            return render_template("index.html", error=error_message, website=submitted_website_url, email=email), 400 # Bad Request
        # --- End Validation ---

        # If validation passed, use the potentially corrected URL
        process_url = corrected_website_url

        # --- Save User Data (handle potential failure) ---
        if not save_user_data(email, process_url):
            # Logged already, decide if we need to notify user or stop
            logging.warning(f"Continuing process despite failed user data save for {email}")
            # Optionally add a non-blocking notice:
            # flash("Notice: Could not save user statistics.", "warning") # Requires session setup & secret key

        try:
            # --- Handle File Upload ---
            safe_filename = secure_filename(file.filename)
            # Use a unique name but keep original extension
            temp_filename = f"{uuid.uuid4()}{os.path.splitext(safe_filename)[1]}"
            temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
            file.save(temp_path)
            logging.info(f"File uploaded to {temp_path}")

            # --- Read Keywords ---
            try:
                df = pd.read_excel(temp_path, engine='openpyxl')
                if df.empty or df.shape[1] == 0:
                     raise ValueError("فایل اکسل خالی است یا ستون اول ندارد.")
                # Use .iloc[:, 0] to get first column, dropna, ensure string, get unique, convert to list
                keywords = df.iloc[:, 0].dropna().astype(str).str.strip().unique().tolist()
                # Filter out empty strings after stripping
                keywords = [kw for kw in keywords if kw]
                if not keywords:
                    raise ValueError("هیچ کلمه کلیدی معتبری (غیر خالی) در ستون اول یافت نشد.")
                logging.info(f"Read {len(keywords)} unique, non-empty keywords from {safe_filename}")
            except Exception as e:
                # Catch pandas/excel reading errors
                raise ValueError(f"خطا در خواندن یا پردازش فایل اکسل: {e}") from e

            # --- Crawl and Fetch Content ---
            logging.info(f"Starting crawl for: {process_url}")
            crawl_start_time = time.time()
            # No need for session context manager here, get_internal_urls uses its own
            with requests.Session() as session_crawl:
                urls_to_check = get_internal_urls(process_url, session_crawl)

            if urls_to_check is None: # Critical error during base URL fetch
                raise ConnectionError(f"امکان دسترسی به وب‌سایت اصلی ({process_url}) یا دریافت لینک‌ها وجود ندارد.")
            if not urls_to_check: # Base URL ok, but no internal links found (only base URL itself)
                 logging.warning(f"No internal URLs found beyond the base for {process_url}. Checking base URL only.")
                 # Continue with just the base URL list

            logging.info(f"Crawling took {time.time() - crawl_start_time:.2f}s. Found {len(urls_to_check)} URLs.")

            fetch_start_time = time.time()
            url_texts = fetch_and_extract_texts_parallel(urls_to_check)
            logging.info(f"Fetching/extraction took {time.time() - fetch_start_time:.2f}s. Got text from {len(url_texts)} URLs.")

            if not url_texts:
                # This can happen if all URLs failed or had non-HTML/empty content
                raise ValueError("هیچ محتوای متنی قابل استخراجی از URL های یافت شده به دست نیامد.")

            # --- Language Detection and Stop Words ---
            # Combine text from fetched URLs for analysis
            combined_text = ' '.join(url_texts.values()) # Might be large, consider sampling if memory becomes an issue
            lang = detect_language(combined_text)
            stop_words = get_dynamic_stop_words(combined_text, lang)
            # Release combined_text if large and no longer needed?
            # del combined_text
            logging.info(f"Detected language: {lang}. Generated {len(stop_words)} stop words.")

            # --- Prepare Phrases to Check ---
            phrases_to_check = set()
            for kw in keywords:
                kw_lower = kw.lower() # Already stripped during reading
                if kw_lower: # Ensure not empty
                    phrases_to_check.add(kw_lower)

            # *** Optional: Add individual important words for checking ***
            # If enabled, consider performance impact if keyword list is very large
            # important_words_to_check = set()
            # for kw_lower in phrases_to_check.copy(): # Iterate over a copy if modifying inside loop
            #     # Find words (3+ chars) in the keyword itself
            #     words_in_kw = re.findall(r'\b\w{3,}\b', kw_lower)
            #     for word in words_in_kw:
            #         # Add word only if it's not a stop word
            #         if word not in stop_words:
            #             important_words_to_check.add(word)
            # phrases_to_check.update(important_words_to_check)
            # if important_words_to_check:
            #      logging.info(f"Added {len(important_words_to_check)} individual important words for checking.")
            #      logging.info(f"Total unique phrases/words to check: {len(phrases_to_check)}")
            # *** End Optional Section ***

            if not phrases_to_check:
                 raise ValueError("پس از پردازش، هیچ کلمه کلیدی معتبری برای بررسی وجود ندارد.")
            logging.info(f"Checking {len(phrases_to_check)} unique phrases/words...")


            # --- Check Keywords Concurrently ---
            check_start_time = time.time()
            temp_results = []
            # Use thread name prefix for easier debugging in logs
            with ThreadPoolExecutor(max_workers=MAX_WORKERS_CHECK, thread_name_prefix='check_kw') as executor:
                future_to_phrase = {
                    executor.submit(check_keyword_in_texts, phrase, url_texts): phrase
                    for phrase in phrases_to_check
                }
                # Process results as they complete
                for future in as_completed(future_to_phrase):
                    phrase = future_to_phrase[future] # Get phrase associated with this future
                    try:
                        # result() will re-raise exceptions from check_keyword_in_texts if any occurred
                        result = future.result()
                        temp_results.append(result)
                    except Exception as e:
                        # Log error encountered during the check_keyword_in_texts call for this phrase
                        logging.error(f"Error processing result for keyword '{phrase}': {e}", exc_info=True)
                        # Append an error entry for this keyword to show in results
                        temp_results.append({"keyword": phrase, "found": False, "url": "Error", "score": 0, "preview": f"Processing Error"})

            # Sort results alphabetically by keyword for consistent output
            results_data = sorted(temp_results, key=lambda x: x['keyword'])
            logging.info(f"Keyword checking took {time.time() - check_start_time:.2f} seconds.")

            # --- Generate Output Excel ---
            if results_data:
                # Use try-except for file generation
                try:
                    output_filename = f"results_{uuid.uuid4()}.xlsx"
                    output_path = os.path.join(app.config['RESULTS_FOLDER'], output_filename)
                    # Define columns explicitly for order and inclusion
                    output_df = pd.DataFrame(results_data, columns=['keyword', 'found', 'url', 'score', 'preview'])
                    output_df.to_excel(output_path, index=False, engine='openpyxl')
                    download_filename = output_filename
                    logging.info(f"Results file generated: {output_filename}")
                except Exception as e:
                     logging.error(f"Failed to generate results Excel file: {e}", exc_info=True)
                     # Don't overwrite previous errors, append or use specific message
                     error_message = (error_message + " | " if error_message else "") + "خطا در تولید فایل نتایج اکسل."

            else:
                # This case might happen if all checks resulted in errors, or no phrases found
                 logging.warning("Keyword checking yielded no results data.")
                 # Provide feedback if no results file generated
                 error_message = (error_message + " | " if error_message else "") + "هیچ نتیجه‌ای برای ذخیره در فایل اکسل وجود ندارد."


        # --- Catch specific errors from processing steps ---
        except (ValueError, ConnectionError) as e:
            # Handle specific errors related to input or connectivity
            logging.warning(f"Process failed due to input/connection error: {e}")
            error_message = str(e)
        except MemoryError:
             # Handle out-of-memory errors, e.g., from loading large text
             logging.critical("Out of memory during processing!", exc_info=True)
             error_message = "خطای کمبود حافظه رخ داد. ممکن است وب‌سایت یا فایل ورودی بسیار بزرگ باشد."
        except Exception as e:
            # Handle unexpected errors during processing
            logging.error(f"An unexpected error occurred during processing: {e}", exc_info=True)
            # Provide a generic error to the user for security
            error_message = "یک خطای پیش‌بینی نشده در سرور رخ داد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
        finally:
            # --- Cleanup Uploaded File ---
            # Ensure temp_path exists and is a file before removing
            if temp_path and os.path.isfile(temp_path): # Check isfile
                try:
                    os.remove(temp_path)
                    logging.info(f"Cleaned up temporary upload file: {temp_path}")
                except OSError as e:
                    # Log error if removal fails (e.g., permissions)
                    logging.error(f"Error removing temporary upload file {temp_path}: {e}")

    # --- Render Template ---
    total_time = time.time() - start_time
    # Log final status
    if error_message:
        logging.error(f"Request finished with error in {total_time:.2f}s. Error: {error_message}")
    else:
        logging.info(f"Request processed successfully in {total_time:.2f}s.")

    # Pass back original input for form fields persistence
    # Make sure website_url here is the one submitted by user if validation failed
    # If validation passed, it doesn't matter as much, but consistency is good.
    # Use submitted_website_url if defined (in POST), else use website_url (from GET or initial form value)
    render_website_url = website_url # Default for GET
    if request.method == 'POST' and 'submitted_website_url' in locals():
        render_website_url = submitted_website_url

    # **** Corrected Indentation Here ****
    return render_template("index.html",
                           results=results_data,
                           download_filename=download_filename,
                           error=error_message,
                           website=render_website_url, # Pass the value user submitted
                           email=email)
# --- End of index() function ---


@app.route('/download/<filename>')
def download(filename):
    """Serves the results file for download."""
    # Secure the filename before using it
    safe_filename = secure_filename(filename)
    # Extra check: ensure secure_filename didn't drastically change the name unexpectedly
    # or return an empty string for malicious inputs.
    if not safe_filename or safe_filename != filename:
        logging.warning(f"Download attempt with potentially unsafe filename rejected: '{filename}' -> '{safe_filename}'")
        abort(400) # Bad request

    file_path = os.path.join(app.config['RESULTS_FOLDER'], safe_filename)

    # Use try-except for file operations & check file existence
    try:
        # Check if the path exists and IS a file (not a directory)
        if not os.path.isfile(file_path):
            logging.warning(f"Download request for non-existent or non-file path: {file_path}")
            abort(404) # Not Found

        # Send the file as attachment
        return send_file(file_path, as_attachment=True)

    except Exception as e:
        # Catch potential errors during send_file (e.g., file deleted between check and send)
        logging.error(f"Error during file download of {safe_filename}: {e}", exc_info=True)
        abort(500) # Internal Server Error


# --- Main Execution ---
if __name__ == '__main__':
    # Initialize DB before running
    try:
         init_db()
    except Exception as e:
         # Log critical error and exit if DB init fails
         logging.critical(f"CRITICAL: Database could not be initialized. Exiting. Error: {e}", exc_info=True)
         exit(1) # Exit with error code

    # Run cleanup on startup (suitable for development)
    # In production, consider a scheduled task (cron, systemd timer, etc.)
    cleanup_old_files()

    # Run the Flask development server
    # Use debug=False and run with a proper WSGI server (like Gunicorn) in production
    # Example: gunicorn --workers 4 --bind 0.0.0.0:5000 --log-level info --access-logfile - --error-logfile - app:app
    logging.info("Starting Flask development server...")
    app.run(debug=False, host='0.0.0.0', port=5000) # Set debug=False for production-like behavior
