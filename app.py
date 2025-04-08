# -*- coding: utf-8 -*-
import os
import uuid
import re
import time
import logging
import pandas as pd
from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import validators

import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# -------------------- Config --------------------
INSTANCE_FOLDER = os.path.abspath(os.path.join(os.path.dirname(__file__), 'instance'))
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESULTS_FOLDER, exist_ok=True)

LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

MAX_FILE_SIZE = 10 * 1024 * 1024
TIMEOUT_PER_URL = 20
MAX_URLS_TO_FETCH = 20
MAX_WORKERS = 10

# -------------------- Flask App --------------------
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

# -------------------- Helpers --------------------
def allowed_file(filename):
    return '.' in filename and filename.lower().endswith('.xlsx')


def get_internal_urls(base_url):
    urls = set()
    base_url = base_url if base_url.startswith("http") else "https://" + base_url
    try:
        options = Options()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument("user-agent=Mozilla/5.0")
        driver = uc.Chrome(options=options)
        driver.get(base_url)
        soup = BeautifulSoup(driver.page_source, 'lxml')
        driver.quit()
        for a in soup.find_all('a', href=True):
            href = urljoin(base_url, a['href']).split('#')[0].rstrip('/')
            if href.startswith(base_url) and validators.url(href):
                urls.add(href)
                if len(urls) >= MAX_URLS_TO_FETCH:
                    break
        return list(urls) or [base_url]
    except Exception as e:
        logging.error(f"[CRAWL ERROR] {base_url}: {e}", exc_info=True)
        return [base_url]


def fetch_page_text(url):
    text = ""
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        driver = uc.Chrome(options=chrome_options)
        driver.set_page_load_timeout(TIMEOUT_PER_URL)
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        html = driver.page_source
        driver.quit()
        soup = BeautifulSoup(html, 'lxml')
        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()
        text = soup.get_text(separator=' ', strip=True)
        text = re.sub(r'\s+', ' ', text.lower())
        if not text.strip():
            text = "[EMPTY TEXT]"
    except Exception as e:
        logging.error(f"[FETCH ERROR] {url}: {e}", exc_info=True)
        text = f"[FAILED TO LOAD] {str(e)}"

    # Save debug no matter what
    safe_name = re.sub(r'\W+', '_', url)
    debug_path = os.path.join(RESULTS_FOLDER, f"debug_{safe_name[:40]}.txt")
    try:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        logging.error(f"[DEBUG WRITE ERROR] {debug_path}: {e}", exc_info=True)

    return url, text if "[FAILED" not in text else None


def match_keywords(keywords, url_texts, match_threshold=0.6):
    results = []
    for kw in keywords:
        kw_lc = kw.lower()
        parts = [w for w in re.findall(r'\b\w{3,}\b', kw_lc)]
        best = {'keyword': kw, 'found': False, 'url': '-', 'score': 0, 'preview': ''}
        for url, text in url_texts.items():
            if not text:
                continue
            found_parts = sum(1 for p in parts if p in text)
            ratio = found_parts / len(parts) if parts else 0
            if ratio >= match_threshold:
                idx = text.find(parts[0]) if parts[0] in text else 0
                preview = text[max(0, idx - 60):idx + 60]
                best.update({'found': True, 'url': url, 'score': found_parts, 'preview': f"...{preview}..."})
        results.append(best)
    return results

# -------------------- Routes --------------------
@app.route('/', methods=['GET', 'POST'])
def index():
    error = None
    results = []
    download_filename = None
    if request.method == 'POST':
        email = request.form.get('email', '')
        website = request.form.get('website', '')
        file = request.files.get('file')

        if not email or not validators.email(email):
            error = "ایمیل معتبر نیست."
        elif not website or not validators.domain(website):
            error = "آدرس وب‌سایت معتبر نیست."
        elif not file or not allowed_file(file.filename):
            error = "فایل اکسل معتبر وارد کنید."

        if error:
            return render_template("index.html", error=error), 400

        try:
            safe_name = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4()}_{safe_name}")
            file.save(file_path)
            df = pd.read_excel(file_path)
            os.remove(file_path)

            keywords = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
            urls = get_internal_urls(website)

            texts = {}
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(fetch_page_text, url): url for url in urls}
                for future in as_completed(futures):
                    url, text = future.result()
                    if text:
                        texts[url] = text

            results = match_keywords(keywords, texts)

            # Save results
            output_df = pd.DataFrame(results)
            output_filename = f"results_{uuid.uuid4()}.xlsx"
            output_path = os.path.join(RESULTS_FOLDER, output_filename)
            output_df.to_excel(output_path, index=False)
            download_filename = output_filename

        except Exception as e:
            logging.error(f"[PROCESS ERROR]: {e}", exc_info=True)
            error = "خطا در پردازش فایل یا سایت."

    return render_template('index.html', results=results, download_filename=download_filename, error=error)

@app.route('/download/<filename>')
def download(filename):
    safe = secure_filename(filename)
    path = os.path.join(RESULTS_FOLDER, safe)
    if os.path.exists(path):
        return send_file(path, as_attachment=True)
    return "File not found", 404

# -------------------- Main --------------------
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
