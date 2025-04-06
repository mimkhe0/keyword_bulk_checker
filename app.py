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
INSTANCE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
DATABASE = os.path.join(INSTANCE_FOLDER, 'database.db')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')

TIMEOUT_PER_URL = 8
MAX_URLS_TO_FETCH = 30
MAX_WORKERS_FETCH = 15
MAX_WORKERS_CHECK = 10
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'.xlsx'}
CLEANUP_DAYS = 1

# --- Flask App Setup ---
app = Flask(__name__)
app.config.from_mapping(
    INSTANCE_FOLDER=INSTANCE_FOLDER,
    DATABASE=DATABASE,
    MAX_CONTENT_LENGTH=MAX_FILE_SIZE,
    UPLOAD_FOLDER=UPLOAD_FOLDER,
    RESULTS_FOLDER=RESULTS_FOLDER
)

for folder in [INSTANCE_FOLDER, UPLOAD_FOLDER, RESULTS_FOLDER]:
    os.makedirs(folder, exist_ok=True)
    if not os.access(folder, os.W_OK):
        raise OSError(f"No write permission for directory: {folder}")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- Database Functions ---
def get_db():
    if '_database' not in g:
        g._database = sqlite3.connect(app.config['DATABASE'])
        g._database.row_factory = sqlite3.Row
    return g._database

@app.teardown_appcontext
def close_connection(exception):
    db = g.pop('_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                website TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )''')
        db.commit()

def save_user_data(email, website):
    try:
        db = get_db()
        db.execute('INSERT INTO users (email, website) VALUES (?, ?)', (email, website))
        db.commit()
        return True
    except sqlite3.Error as e:
        logging.error(f"Failed to save user data: {e}")
        return False

# --- Helper Functions ---
def allowed_file(filename):
    return '.' in filename and os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def cleanup_old_files():
    cutoff = time.time() - timedelta(days=CLEANUP_DAYS).total_seconds()
    for folder in [UPLOAD_FOLDER, RESULTS_FOLDER]:
        for f in os.listdir(folder):
            path = os.path.join(folder, f)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                try:
                    os.remove(path)
                    logging.info(f"Removed old file: {path}")
                except Exception as e:
                    logging.warning(f"Failed to remove {path}: {e}")

def fetch_page_text(url, session):
    try:
        resp = session.get(url, timeout=TIMEOUT_PER_URL)
        resp.raise_for_status()
        if 'html' not in resp.headers.get('Content-Type', '').lower():
            return url, None
        soup = BeautifulSoup(resp.content, 'lxml')
        for tag in soup(['script', 'style', 'footer', 'nav', 'form', 'head', 'header', 'aside']):
            tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        return url, re.sub(r'\s+', ' ', text.lower())
    except Exception as e:
        logging.warning(f"Error fetching {url}: {e}")
        return url, None

def get_internal_urls(base_url, session):
    found = set()
    base = base_url if base_url.startswith('http') else 'https://' + base_url
    try:
        resp = session.get(base, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, 'lxml')
        for a in soup.find_all('a', href=True):
            href = urljoin(base, a['href']).split('#')[0].rstrip('/')
            if href.startswith(base) and validators.url(href):
                found.add(href)
                if len(found) >= MAX_URLS_TO_FETCH:
                    break
    except Exception as e:
        logging.warning(f"Crawl failed for {base}: {e}")
    return list(found or [base])

def detect_language(text):
    if re.search(r'[\u0600-\u06FF]', text):
        return 'fa_ar'
    return 'en'

def extract_keywords(text, lang):
    words = re.findall(r'\b\w{3,}\b', text)
    common = Counter(words).most_common(50)
    return {w for w, _ in common}

def check_keywords(keywords, texts):
    results = []
    for kw in keywords:
        best = {'keyword': kw, 'found': False, 'url': '-', 'score': 0, 'preview': ''}
        for url, text in texts.items():
            if not text:
                continue
            count = text.count(kw)
            if count > best['score']:
                idx = text.find(kw)
                best.update({
                    'found': True,
                    'url': url,
                    'score': count,
                    'preview': f"...{text[max(0, idx-60):idx+60]}..."
                })
        results.append(best)
    return results

# --- Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    error = None
    results = []
    download_filename = None
    email = request.form.get('email', '').strip()
    website = request.form.get('website', '').strip()

    if request.method == 'POST':
        file = request.files.get('file')
        if not email or not validators.email(email):
            error = "آدرس ایمیل معتبر نیست."
        elif not website:
            error = "آدرس وب‌سایت را وارد کنید."
        elif not file or not allowed_file(file.filename):
            error = "فایل اکسل معتبر انتخاب نشده است."
        
        if error:
            return render_template("index.html", error=error, email=email, website=website), 400

        try:
            safe_name = secure_filename(file.filename)
            temp_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}{os.path.splitext(safe_name)[1]}")
            file.save(temp_path)
            df = pd.read_excel(temp_path)
            keywords = df.iloc[:, 0].dropna().astype(str).str.lower().str.strip().unique().tolist()
            if not keywords:
                raise ValueError("هیچ کلمه کلیدی معتبری یافت نشد.")
            os.remove(temp_path)

            save_user_data(email, website)
            with requests.Session() as session:
                urls = get_internal_urls(website, session)
                with ThreadPoolExecutor(max_workers=MAX_WORKERS_FETCH) as exec:
                    tasks = {exec.submit(fetch_page_text, url, session): url for url in urls}
                    texts = dict(f.result() for f in as_completed(tasks))

            full_text = ' '.join(t for t in texts.values() if t)
            lang = detect_language(full_text)
            stop_words = extract_keywords(full_text, lang)
            keywords = [k for k in keywords if k not in stop_words]

            with ThreadPoolExecutor(max_workers=MAX_WORKERS_CHECK) as exec:
                futures = [exec.submit(check_keywords, [kw], texts) for kw in keywords]
                for f in as_completed(futures):
                    results.extend(f.result())

            df_out = pd.DataFrame(results)
            output_filename = f"results_{uuid.uuid4()}.xlsx"
            output_path = os.path.join(RESULTS_FOLDER, output_filename)
            df_out.to_excel(output_path, index=False)
            download_filename = output_filename
        except Exception as e:
            logging.error(f"Processing error: {e}")
            error = "خطایی در پردازش رخ داد."

    return render_template("index.html", results=results, error=error, email=email, website=website, download_filename=download_filename)

@app.route('/download/<filename>')
def download(filename):
    safe_name = secure_filename(filename)
    full_path = os.path.join(RESULTS_FOLDER, safe_name)
    if not os.path.abspath(full_path).startswith(os.path.abspath(RESULTS_FOLDER)):
        abort(403)
    if not os.path.exists(full_path):
        abort(404)
    return send_file(full_path, as_attachment=True)

# --- Main ---
if __name__ == '__main__':
    init_db()
    cleanup_old_files()
    app.run(debug=False, host='0.0.0.0', port=5000)
