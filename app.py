# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, send_file, abort, g
import pandas as pd
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
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- Configuration ---
INSTANCE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
DATABASE = os.path.join(INSTANCE_FOLDER, 'database.db')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')

TIMEOUT_PER_URL = 10
MAX_URLS_TO_FETCH = 30
MAX_WORKERS_FETCH = 10
MAX_WORKERS_CHECK = 10
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'.xlsx'}
CLEANUP_DAYS = 1

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

def fetch_page_text(url):
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(TIMEOUT_PER_URL)

        driver.get(url)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        html = driver.page_source
        driver.quit()

        soup = BeautifulSoup(html, 'lxml')
        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()

        text = soup.get_text(separator=' ', strip=True)
        text = re.sub(r'\s+', ' ', text.lower())

        # Save debug version for best-istikhara.com
        if "best-istikhara.com" in url:
            debug_path = os.path.join(RESULTS_FOLDER, "_debug_text.txt")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(text)

        return url, text

    except Exception as e:
        logging.warning(f"Selenium fetch failed for {url}: {e}")
        return url, None

def get_internal_urls(base_url):
    import requests
    found = set()
    base = base_url if base_url.startswith('http') else 'https://' + base_url
    try:
        with requests.Session() as session:
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

def check_keywords(keywords, texts):
    results = []
    for kw in keywords:
        best = {'keyword': kw, 'found': False, 'url': '-', 'score': 0, 'preview': ''}
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
        for url, text in texts.items():
            if not text:
                continue
            matches = list(pattern.finditer(text))
            if matches:
                first = matches[0]
                snippet = text[max(0, first.start()-60):first.end()+60]
                if len(matches) > best['score']:
                    best.update({
                        'found': True,
                        'url': url,
                        'score': len(matches),
                        'preview': f"...{snippet}..."
                    })
        results.append(best)
    return results

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
            input_phrases = df.iloc[:, 0].dropna().astype(str).str.lower().str.strip().unique().tolist()
            if not input_phrases:
                raise ValueError("هیچ کلمه کلیدی معتبری یافت نشد.")
            os.remove(temp_path)

            save_user_data(email, website)
            urls = get_internal_urls(website)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS_FETCH) as exec:
                tasks = {exec.submit(fetch_page_text, url): url for url in urls}
                texts = dict(f.result() for f in as_completed(tasks))

            final_keywords = set()
            for phrase in input_phrases:
                final_keywords.add(phrase)
                words = re.findall(r'\b\w{3,}\b', phrase)
                for word in words:
                    final_keywords.add(word)

            keywords = list(final_keywords)

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

if __name__ == '__main__':
    init_db()
    cleanup_old_files()
    app.run(debug=False, host='0.0.0.0', port=5000)
