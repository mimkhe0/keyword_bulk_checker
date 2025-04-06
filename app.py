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
INSTANCE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
DATABASE = os.path.join(INSTANCE_FOLDER, 'database.db')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')

TIMEOUT_PER_URL = 8
MAX_URLS_TO_FETCH = 30
MAX_URLS_FOR_TEXT_EXTRACTION = 10
MAX_WORKERS_FETCH = 15
MAX_WORKERS_CHECK = 10
MAX_FILE_SIZE = 10 * 1024 * 1024
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
    format='%(asctime)s - %(levelname)s - %(process)d - %(thread)d - %(message)s'
)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# --- Database Functions ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(app.config['DATABASE'], detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row
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
                db.execute('''CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    website TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
            logging.info("Database initialized successfully.")
    except sqlite3.Error as e:
        logging.error(f"Database initialization failed: {e}", exc_info=True)
        raise

def save_user_data(email, website):
    sql = 'INSERT INTO users (email, website) VALUES (?, ?)'
    try:
        db = get_db()
        with db:
            db.execute(sql, (email, website))
        logging.info(f"User data saved: {email}, {website}")
        return True
    except sqlite3.Error as e:
        logging.error(f"Failed to save user data for {email}: {e}", exc_info=True)
        return False

# --- Helper Functions ---
def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_url(url, session):
    try:
        with session.get(url, timeout=TIMEOUT_PER_URL, allow_redirects=True) as response:
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').lower()
            if 'html' not in content_type:
                return url, None
            soup = BeautifulSoup(response.content, 'lxml')
            for element in soup(['script', 'style', 'footer', 'nav', 'form', 'head', 'header', 'aside', 'noscript']):
                element.decompose()
            text = soup.get_text(separator=' ', strip=True).lower()
            text = re.sub(r'\s+', ' ', text).strip()
            return url, text
    except Exception as e:
        logging.warning(f"Failed to fetch {url}: {e}")
        return url, None

def fetch_and_extract_texts_parallel(urls):
    url_texts = {}
    headers = {'User-Agent': 'Mozilla/5.0 KeywordCheckerBot/1.0'}
    with requests.Session() as session:
        session.headers.update(headers)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_FETCH) as executor:
            future_to_url = {executor.submit(extract_text_from_url, url, session): url for url in urls}
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    res_url, text = future.result()
                    if text:
                        url_texts[res_url] = text
                except Exception as e:
                    logging.error(f"Error fetching {url}: {e}")
    return url_texts

def get_internal_urls(base_url, session):
    urls = set()
    if not base_url.startswith(('http://', 'https://')):
        base_url = 'http://' + base_url
    normalized_base_url = base_url.rstrip('/')
    urls.add(normalized_base_url)
    try:
        with session.get(normalized_base_url, timeout=10) as response:
            response.raise_for_status()
            if 'html' not in response.headers.get('content-type', '').lower():
                return list(urls)
            soup = BeautifulSoup(response.content, 'lxml')
            for a_tag in soup.select('a[href]'):
                href = a_tag.get('href')
                if not href:
                    continue
                full_url = urljoin(normalized_base_url, href).split('#')[0].rstrip('/')
                if full_url.startswith(normalized_base_url) and validators.url(full_url):
                    urls.add(full_url)
                    if len(urls) >= MAX_URLS_TO_FETCH:
                        break
        return list(urls)
    except Exception as e:
        logging.error(f"Failed to crawl {base_url}: {e}")
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
        'en': {'the', 'and', 'for', 'with', 'to', 'from', 'by', 'is', 'are', 'on', 'at', 'this', 'that', 'or'},
        'fa_ar': {'و', 'در', 'به', 'از', 'که', 'این', 'آن', 'است'}
    }
    base_stops = general_stop_words.get(lang, set())
    return base_stops.union(most_common_words)

def check_keyword_in_texts(phrase, url_texts_dict):
    best_match = {"found": False, "url": "-", "score": 0, "preview": "", "keyword": phrase}
    phrase_lower = phrase.lower()
    for url, text in url_texts_dict.items():
        if text is None:
            continue
        try:
            index = text.find(phrase_lower)
            if index != -1:
                score = text.count(phrase_lower)
                start = max(0, index - 60)
                end = min(len(text), index + len(phrase_lower) + 60)
                preview_text = text[start:end].strip()
                if score > best_match["score"]:
                    best_match.update({
                        "found": True,
                        "url": url,
                        "score": score,
                        "preview": f"...{preview_text}..."
                    })
        except Exception as e:
            logging.warning(f"Error checking '{phrase}' in {url}: {e}")
            continue
    return best_match

def cleanup_old_files():
    now = time.time()
    cutoff = now - (CLEANUP_DAYS * 86400)
    folders = [app.config['UPLOAD_FOLDER'], app.config['RESULTS_FOLDER']]
    for folder in folders:
        try:
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path) and os.path.getmtime(file_path) < cutoff:
                    os.remove(file_path)
                    logging.info(f"Deleted old file: {file_path}")
        except Exception as e:
            logging.warning(f"Cleanup error in {folder}: {e}")

@app.route('/', methods=['GET', 'POST'])
def index():
    start_time = time.time()
    results_data = []
    download_filename = None
    error_message = None
    website_url = request.form.get('website', '').strip()
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        file = request.files.get('file')
        if not email or not validators.email(email):
            error_message = "Invalid email."
        if not website_url or not validators.url(website_url):
            if not website_url.startswith(('http://', 'https://')):
                website_url = 'http://' + website_url
            if not validators.url(website_url):
                error_message = "Invalid website URL."
        if not file or not file.filename or not allowed_file(file.filename):
            error_message = "Invalid file."
        if error_message:
            return render_template("index.html", error=error_message, website=website_url, email=email)

        save_user_data(email, website_url)
        safe_filename = secure_filename(file.filename)
        temp_filename = f"{uuid.uuid4()}_{safe_filename}"
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename)
        file.save(temp_path)
        df = pd.read_excel(temp_path)
        keywords = df.iloc[:, 0].dropna().astype(str).unique().tolist()

        with requests.Session() as session:
            session.headers.update({'User-Agent': 'Mozilla/5.0 KeywordCheckerBot/1.0'})
            urls_to_check = get_internal_urls(website_url, session)
            if not urls_to_check:
                error_message = "Could not fetch internal URLs."
                os.remove(temp_path)
                return render_template("index.html", error=error_message)
            url_texts = fetch_and_extract_texts_parallel(urls_to_check)

        sample_text = ' '.join(url_texts.values())
        lang = detect_language(sample_text)
        stop_words = get_dynamic_stop_words(sample_text, lang)
        phrases_to_check = set(kw.lower() for kw in keywords)

        temp_results = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_CHECK) as executor:
            future_to_phrase = {executor.submit(check_keyword_in_texts, phrase, url_texts): phrase for phrase in phrases_to_check}
            for future in as_completed(future_to_phrase):
                temp_results.append(future.result())

        results_data = sorted(temp_results, key=lambda x: x['keyword'])

        try:
            output_filename = f"results_{uuid.uuid4()}.xlsx"
            output_path = os.path.join(app.config['RESULTS_FOLDER'], output_filename)
            output_df = pd.DataFrame(results_data, columns=['keyword', 'found', 'url', 'score', 'preview'])
            output_df.to_excel(output_path, index=False, engine='openpyxl')
            download_filename = output_filename
        except Exception as e:
            error_message = "Could not generate result file."
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    total_time = time.time() - start_time
    logging.info(f"Processed in {total_time:.2f} seconds")
    return render_template("index.html", results=results_data, download_filename=download_filename, error=error_message, website=website_url)

@app.route('/download/<filename>')
def download(filename):
    safe_filename = secure_filename(filename)
    file_path = os.path.join(app.config['RESULTS_FOLDER'], safe_filename)
    if not os.path.exists(file_path):
        abort(404)
    return send_file(file_path, as_attachment=True)

if __name__ == '__main__':
    init_db()
    cleanup_old_files()
    app.run(debug=True, host='0.0.0.0', port=5000)
