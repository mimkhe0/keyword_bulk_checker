import flask
from flask import Flask, render_template, request, send_file, abort
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor
import os
import uuid
from werkzeug.utils import secure_filename
import validators
import re
from collections import Counter
from datetime import datetime
import logging
from functools import lru_cache
import shutil

app = Flask(__name__)
INSTANCE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
os.makedirs(INSTANCE_FOLDER, exist_ok=True)
app.config['INSTANCE_FOLDER'] = INSTANCE_FOLDER

# Configuration
TIMEOUT_PER_URL = 5
MAX_URLS = 20
MAX_WORKERS = 10
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'.xlsx'}

# Setup logging
logging.basicConfig(
    filename=os.path.join(INSTANCE_FOLDER, 'app.log'),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Check allowed file extension
def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

# Cache URL text extraction
@lru_cache(maxsize=100)
def extract_text_from_urls(urls_tuple):
    urls = list(urls_tuple)
    texts = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    for url in urls:
        try:
            response = requests.get(url, timeout=TIMEOUT_PER_URL, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')
            for script in soup(['script', 'style', 'footer', 'nav', 'form', 'head']):
                script.decompose()
            text = soup.get_text(separator=' ').lower()
            texts.append(text)
        except requests.RequestException as e:
            logging.error(f"Failed to extract text from {url}: {str(e)}")
            continue
    return ' '.join(texts)

# Detect language
def detect_language(word):
    if re.search(r'[\u0600-\u06FF]', word):
        return 'fa_ar'
    return 'en'

# Generate dynamic stop words
def get_dynamic_stop_words(text, lang, top_n=30):
    words = re.findall(r'\b\w{2,}\b', text)
    word_counts = Counter(words)
    most_common = word_counts.most_common(top_n)

    general_stop_words = {
        'en': {'the', 'and', 'in', 'of', 'for', 'with', 'to', 'from', 'by', 'is', 'are', 'on', 'at'},
        'fa_ar': {'از', 'و', 'به', 'در', 'با', 'که', 'برای', 'را', 'این', 'آن', 'یا', 'على', 'علىه', 'فی'}
    }

    return general_stop_words[lang].union({word for word, _ in most_common})

# Get URLs from website with error handling
def get_urls(website):
    urls = set()
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        res = requests.get(website, timeout=10, headers=headers)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'lxml')
        urls.add(website.strip('/'))
        for a in soup.select('a[href]'):
            full_url = urljoin(website, a['href']).split('#')[0]
            if full_url.startswith(website.strip('/')) and validators.url(full_url):
                urls.add(full_url)
            if len(urls) >= MAX_URLS:
                break
    except requests.RequestException as e:
        logging.error(f"Failed to get URLs from {website}: {str(e)}")
        urls.add(website.strip('/'))
    return list(urls)

# Enhanced keyword checker with preview
def check_keyword(keyword, urls, dynamic_stop_words):
    results = []
    important_words = [w for w in re.findall(r'\b\w+\b', keyword.lower()) if w not in dynamic_stop_words]
    phrases_to_check = [keyword] + important_words
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    for phrase in phrases_to_check:
        found, best_url, score, preview = False, '-', 0, ''
        for url in urls:
            try:
                res = requests.get(url, timeout=TIMEOUT_PER_URL, headers=headers)
                content = res.text.lower()
                if phrase in content:
                    found, best_url, score = True, url, content.count(phrase)
                    # Get preview snippet
                    index = content.index(phrase)
                    start = max(0, index - 50)
                    end = min(len(content), index + len(phrase) + 50)
                    preview = content[start:end].strip()
                    break
            except requests.RequestException:
                continue
        results.append({"keyword": phrase, "found": found, "url": best_url, "score": score, "preview": preview})
    return results

# Save user data with error handling
def save_user_data(email, website):
    users_db = os.path.join(INSTANCE_FOLDER, 'users.xlsx')
    try:
        if not os.path.exists(users_db):
            df_users = pd.DataFrame(columns=["Email", "Website", "Date"])
            df_users.to_excel(users_db, index=False)

        df_users = pd.read_excel(users_db)
        new_row = {"Email": email, "Website": website, "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        df_users = pd.concat([df_users, pd.DataFrame([new_row])], ignore_index=True)
        df_users.to_excel(users_db, index=False)
    except Exception as e:
        logging.error(f"Failed to save user data: {str(e)}")

@app.route('/', methods=['GET', 'POST'])
def index():
    results, download_filename, error = [], None, None
    app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

    if request.method == 'POST':
        try:
            email = request.form.get('email', '').strip()
            website_url = request.form.get('website', '').strip()
            file = request.files.get('file')

            # Enhanced input validation
            if not all([email, website_url, file]) or not validators.email(email) or \
               not validators.url(website_url) or not allowed_file(file.filename):
                error = 'Invalid input: Check email, URL, or file format (.xlsx only)'
                return render_template("index.html", error=error)

            save_user_data(email, website_url)

            temp_filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
            temp_path = os.path.join(INSTANCE_FOLDER, temp_filename)
            file.save(temp_path)

            df = pd.read_excel(temp_path)
            keywords = df.iloc[:, 0].dropna().astype(str).tolist()

            urls_to_check = get_urls(website_url)
            site_text = extract_text_from_urls(tuple(urls_to_check[:5]))
            lang = detect_language(site_text)
            dynamic_stop_words = get_dynamic_stop_words(site_text, lang)

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(check_keyword, kw, urls_to_check, dynamic_stop_words) 
                          for kw in keywords]
                for future in futures:
                    results.extend(future.result())

            output_filename = f"results_{uuid.uuid4()}.xlsx"
            output_path = os.path.join(INSTANCE_FOLDER, output_filename)
            pd.DataFrame(results).to_excel(output_path, index=False)
            download_filename = output_filename

        except Exception as e:
            logging.error(f"Processing error: {str(e)}")
            error = f"An error occurred: {str(e)}"
        finally:
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.remove(temp_path)

    return render_template("index.html", results=results, download_filename=download_filename, error=error)

@app.route('/download/<filename>')
def download(filename):
    try:
        output_path = os.path.join(INSTANCE_FOLDER, secure_filename(filename))
        if not os.path.exists(output_path):
            abort(404)
        response = send_file(output_path, as_attachment=True)
        # Clean up after sending
        os.remove(output_path)
        return response
    except Exception as e:
        logging.error(f"Download error: {str(e)}")
        abort(500)

# Cleanup old files
def cleanup_old_files():
    now = datetime.now()
    for filename in os.listdir(INSTANCE_FOLDER):
        file_path = os.path.join(INSTANCE_FOLDER, filename)
        if os.path.isfile(file_path):
            file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
            if (now - file_time).days > 1:  # Remove files older than 1 day
                os.remove(file_path)

if __name__ == '__main__':
    cleanup_old_files()  # Clean up on startup
    # قدیمی ممکنه این باشه:
# app.run()



