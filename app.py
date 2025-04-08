# -*- coding: utf-8 -*-
import os
import re
import uuid
import logging
from collections import Counter
from typing import List, Dict, Tuple, Optional, Set

import pandas as pd
from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_FOLDER = os.path.join(BASE_DIR, 'instance')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')
ALLOWED_EXTENSIONS: Set[str] = {'.xlsx'}
SELENIUM_TIMEOUT = 25
MAX_FILE_SIZE = 10 * 1024 * 1024

for folder in [UPLOAD_FOLDER, RESULTS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

app = Flask(__name__, instance_path=INSTANCE_FOLDER)
app.config.update(
    UPLOAD_FOLDER=UPLOAD_FOLDER,
    RESULTS_FOLDER=RESULTS_FOLDER,
    MAX_CONTENT_LENGTH=MAX_FILE_SIZE,
    SECRET_KEY=os.environ.get('FLASK_SECRET_KEY', 'a_default_dev_secret_key')
)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(process)d - %(thread)d - %(message)s'
)

def allowed_file(filename: str) -> bool:
    return '.' in filename and os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def fetch_page_text(url: str) -> str:
    driver = None
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument("user-agent=Mozilla/5.0")

        driver = uc.Chrome(options=chrome_options)
        driver.set_page_load_timeout(SELENIUM_TIMEOUT)
        driver.get(url)

        WebDriverWait(driver, SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        html = driver.page_source
        if not html:
            return ""

        text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        return re.sub(r'\s+', ' ', text).strip().lower()

    except (TimeoutException, WebDriverException) as e:
        logging.error(f"Fetch error: {e}")
        return ""
    finally:
        if driver:
            driver.quit()

def get_keywords_from_file(filepath: str) -> Tuple[Optional[List[str]], Optional[str]]:
    try:
        df = pd.read_excel(filepath, header=None)
        if df.empty or df.shape[1] == 0:
            return None, "فایل اکسل خالی است."
        keywords = df.iloc[:, 0].dropna().astype(str).str.strip().str.lower().tolist()
        return list(set(keywords)), None
    except Exception as e:
        return None, str(e)

def check_keywords(keywords: List[str], text: str, url: str) -> List[Dict]:
    results = []
    for kw in keywords:
        try:
            matches = list(re.finditer(r'\b' + re.escape(kw) + r'\b', text, re.IGNORECASE))
            found = len(matches) > 0
            preview = ""
            if found:
                m = matches[0]
                preview = "..." + text[max(0, m.start()-50):m.end()+50] + "..."
            results.append({
                'keyword': kw,
                'found': found,
                'url': url if found else "-",
                'score': len(matches),
                'preview': preview
            })
        except Exception:
            results.append({'keyword': kw, 'found': False, 'url': "-", 'score': 0, 'preview': ""})
    return results

@app.route('/', methods=['GET', 'POST'])
def index():
    results, error, download_filename = [], None, None
    email, website = request.form.get('email', '').strip(), request.form.get('website', '').strip()

    if request.method == 'POST':
        file = request.files.get('file')
        if not email or not website or not file or not allowed_file(file.filename):
            error = "اطلاعات کامل نیست یا فایل نامعتبر است."
        else:
            try:
                filename = secure_filename(file.filename)
                path = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
                file.save(path)

                keywords, err = get_keywords_from_file(path)
                if err or not keywords:
                    raise ValueError(err or "کلمات کلیدی یافت نشد.")

                if not website.startswith(('http://', 'https://')):
                    website = 'http://' + website

                page_text = fetch_page_text(website)
                if not page_text:
                    raise ValueError("محتوایی از سایت دریافت نشد.")

                results = check_keywords(keywords, page_text, website)
                df = pd.DataFrame(results)
                output_file = f"results_{uuid.uuid4().hex}.xlsx"
                df.to_excel(os.path.join(app.config['RESULTS_FOLDER'], output_file), index=False, engine='openpyxl')
                download_filename = output_file
            except Exception as e:
                error = f"خطا: {e}"

    return render_template("index.html", results=results, error=error,
                           download_filename=download_filename, email=email, website=website)

@app.route('/download/<filename>')
def download(filename: str):
    path = os.path.join(app.config['RESULTS_FOLDER'], secure_filename(filename))
    if not os.path.exists(path):
        return "فایل یافت نشد", 404
    return send_file(path, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
