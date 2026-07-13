import streamlit as st
import time
import re
import requests
import gspread
import json
import hashlib
import os
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# ИИнициализация конфигурации страницы
st.set_page_config(page_title="AI Табличный Парсер", layout="wide", page_icon="📊")

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def sanitize_string(text):
    """
    Функция защиты от умных редакторов кода.
    Автоматически извлекает чистые строки, если среда разработки превратила их в Markdown.
    """
    if not isinstance(text, str):
        return text
    text = text.strip()
    markdown_match = re.search(r'\[.*?\]\((.*?)\)', text)
    if markdown_match:
        return markdown_match.group(1).strip()
    if text.startswith("https://"):
        url_match = re.search(r'(https://[^\s\'"\]\)]+)', text)
        if url_match:
            return url_match.group(1).strip()
    return text

# Инициализация состояний сессии
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
    st.session_state["username"] = ""
if "processing" not in st.session_state:
    st.session_state["processing"] = False

# === БЛОК АВТОРИЗАЦИИ ===
if "users" in st.secrets and not st.session_state["authenticated"]:
    st.markdown("<h2 style='text-align: center; margin-top: 50px;'>🔒 Вход в AI Парсер</h2>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1.5, 1])
    with col2:
        with st.form("login_form"):
            username = st.text_input("👤 Логин").strip()
            password = st.text_input("🔑 Пароль", type="password").strip()
            submit = st.form_submit_button("Войти", use_container_width=True, type="primary")
            
            if submit:
                users_db = st.secrets["users"]
                if username in users_db and hash_password(password) == users_db[username]:
                    st.session_state["authenticated"] = True
                    st.session_state["username"] = username
                    st.success("Успешный вход!")
                    st.rerun()
                else:
                    st.error("Неверный логин или пароль")
    st.stop()

# --- ОСНОВНОЙ ИНТЕРФЕЙС ПОСЛЕ АВТОРИЗАЦИИ ---
st.title("📊 ИИ Анализатор документов из Google Таблиц")

if st.session_state["username"]:
    st.sidebar.header("👤 Аккаунт")
    st.sidebar.write(f"Вы вошли как: **{st.session_state['username']}**")
    if st.sidebar.button("🚪 Выйти из системы", type="secondary"):
        st.session_state["authenticated"] = False
        st.session_state["username"] = ""
        st.session_state["processing"] = False
        st.rerun()
    st.sidebar.markdown("---")

st.sidebar.header("🔑 Настройки доступа")
gemini_api_key = st.sidebar.text_input("Gemini API Key", value="Вставьте API токен Gemini", type="password")
uploaded_creds = st.sidebar.file_uploader("Загрузите credentials.json (необязательно, если файл лежит в папке скрипта)", type=["json"])

st.header("⚙️ Конфигурация колонок и промтов")
col1, col2 = st.columns(2)
with col1:
    sheet_id = st.text_input("Google Sheet ID", value="13dHYPv7RkIwrqLpiVuegNkL7ZD9eipfJrYbocIP8_OY")
    worksheet_name = st.text_input("Название листа", value="Лоты")
with col2:
    url_column = st.number_input("Номер колонки со ссылками на PDF (A=1, B=2...)", min_value=1, value=32)
    out_column = st.number_input("Номер колонки для вывода результата (A=1, B=2...)", min_value=1, value=12)

model_name = st.selectbox("Версия модели Gemini", ["gemini-2.5-flash", "gemini-2.5-pro"])

# Оригинальные системные инструкции
default_system_instruction = (
    "Ты — первоклассный эксперт по компьютерному аппаратному обеспечению и архитектуре процессоров. "
    "Твоя главная задача — проанализировать числовые параметры процессора из текста госзакупок "
    "(количество ядер, потоков, базовую и максимальную частоту, кэш, шину, частоту встроенной графики) "
    "и на основе этих данных определить и написать ТОЧНУЮ КОММЕРЧЕСКУЮ МОДЕЛЬ ПРОЦЕССОРА (например, 'Intel Core i5-11400' или 'Intel Core i5-10400'). "
    "В тексте спецификаций модели намеренно скрывают за фразами 'не менее', но ты обязан вычислить конкретную модель на рынке, которая идеально подпадает под эти характеристики. "
    "Строго запрещено писать сухие параметры вроде 'не менее 6 ядер' или 'Не указано' в поле 'cpu', если в тексте есть частоты и ядра. "
    "Возвращай результат СТРОГО в формате JSON без комментариев и без разметки markdown (без ```json)."
)

# Промт обновлен: добавлен пункт "peripherals"
default_prompt_text = """Проанализируй текст спецификации и заполни следующие поля в формате JSON.
Используй свои глубокие знания о процессорах Intel и AMD, чтобы сопоставить частоты и ядра с конкретным рыночным названием процессора.

{
  "type": "Тип устройства (например: Моноблок, Ноутбук, Компьютер в комплекте)",
  "cpu": "Точная модель процессора, которую ты определил по характеристикам (например: Intel Core i5-11400)",
  "ram": "Объем и тип памяти (например: 16 ГБ DDR4)",
  "storage": "Накопитель (например: 512 ГБ SSD)",
  "gpu": "Видеокарта",
  "screen": "Экран (Диагональ, разрешение, тип матрицы)",
  "os": "Операционная система",
  "peripherals": "Периферия и доп. устройства (Клавиатура, мышь, наушники, веб-камера, коврик, если есть в тексте)"
}"""

system_prompt = st.text_area("Системная инструкция (System Instruction)", value=default_system_instruction, height=180)
prompt_text = st.text_area("Основной текст промта (Ожидаемая JSON структура)", value=default_prompt_text, height=270)

st.markdown("---")

# === УПРАВЛЕНИЕ КНОПКАМИ СТАРТ / СТОП ===
if not st.session_state["processing"]:
    if st.button("🚀 Запустить потоковую обработку таблицы", type="primary", use_container_width=True):
        if not gemini_api_key:
            st.error("Критическая ошибка: Заполните Gemini API Key!")
        elif not uploaded_creds and not os.path.exists("credentials.json"):
            st.error("Критическая ошибка: Файл credentials.json не найден ни в папке, ни в загрузчике!")
        else:
            st.session_state["processing"] = True
            st.rerun()
else:
    if st.button("🛑 Остановить обработку таблицы", type="secondary", use_container_width=True):
        st.session_state["processing"] = False
        st.warning("Запрос на остановку отправлен. Текущий цикл завершится...")
        st.rerun()

# === ЛОГИКА ПОТОКОВОЙ ОБРАБОТКИ ===
if st.session_state["processing"]:
    try:
        genai.configure(api_key=sanitize_string(gemini_api_key))
        model = genai.GenerativeModel(model_name=model_name, system_instruction=system_prompt)
        
        google_scopes = [
            sanitize_string("[https://www.googleapis.com/auth/spreadsheets](https://www.googleapis.com/auth/spreadsheets)"),
            sanitize_string("[https://www.googleapis.com/auth/drive](https://www.googleapis.com/auth/drive)")
        ]
        
        if uploaded_creds:
            uploaded_creds.seek(0)
            creds_dict = json.load(uploaded_creds)
            creds = Credentials.from_service_account_info(creds_dict, scopes=google_scopes)
        else:
            creds = Credentials.from_service_account_file("credentials.json", scopes=google_scopes)
            
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sanitize_string(sheet_id)).worksheet(worksheet_name)
        st.success("Успешное подключение к Google API и таблице!")
    except Exception as e:
        st.error(f"Ошибка инициализации подключения: {e}")
        st.session_state["processing"] = False
        st.stop()

    st.subheader("📋 Лог выполнения операций")
    log_container = st.empty()
    progress_bar = st.progress(0)
    logs = []

    def log(msg):
        logs.append(msg)
        log_container.code("\n".join(logs[-12:]))

    try:
        all_rows = sheet.get_values(value_render_option='FORMULA')
        total_rows = len(all_rows)
        request_count = 0
        
        for index in range(1, total_rows):
            if not st.session_state["processing"]:
                log("🛑 Обработка была принудительно прервана пользователем.")
                break
                
            row_number = index + 1
            row = all_rows[index]
            progress_bar.progress(index / (total_rows - 1))
            
            raw_url_cell = row[url_column - 1] if len(row) >= url_column else ""
            current_result = row[out_column - 1] if len(row) >= out_column else ""
            
            if raw_url_cell and not current_result.strip():
                download_url = raw_url_cell.strip()
                
                if download_url.startswith('='):
                    match = re.search(r'HYPERLINK\(\s*["\']([^"\']+)["\']', download_url, re.IGNORECASE)
                    if match: 
                        download_url = match.group(1).strip()
                
                log(f"[{row_number}/{total_rows}] Скачивание: {download_url[:50]}...")
                
                try:
                    res = requests.get(download_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
                    if res.status_code == 200:
                        pdf_bytes = res.content
                        
                        for attempt in range(3):
                            if not st.session_state["processing"]:
                                break

                            try:
                                api_res = model.generate_content([
                                    {'mime_type': 'application/pdf', 'data': pdf_bytes}, 
                                    prompt_text
                                ], generation_config=genai.types.GenerationConfig(response_mime_type="application/json"))
                                
                                data = json.loads(api_res.text)
                                
                                # Сборка строки спецификации: добавлено поле Периферия
                                formatted_str = (
                                    f"Тип: {data.get('type')}; "
                                    f"CPU: {data.get('cpu')}; "
                                    f"RAM: {data.get('ram')}; "
                                    f"SSD/HDD: {data.get('storage')}; "
                                    f"GPU: {data.get('gpu')}; "
                                    f"Экран: {data.get('screen')}; "
                                    f"ОС: {data.get('os')}; "
                                    f"Периферия: {data.get('peripherals')}"
                                )
                                
                                sheet.update_cell(row_number, out_column, formatted_str)
                                log(f"-> Строка {row_number}: Успешно записано CPU: {data.get('cpu')}")
                                request_count += 1
                                break
                            except Exception as api_err:
                                if "429" in str(api_err):
                                    wait_time = (2 ** attempt) * 20
                                    log(f"Превышен лимит! Ожидание {wait_time} сек...")
                                    time.sleep(wait_time)
                                else:
                                    log(f"Ошибка парсинга JSON: {api_err}")
                                    break
                                    
                        if request_count > 0 and request_count % 10 == 0:
                            log("Плановая пауза 60 секунд для защиты лимитов...")
                            time.sleep(60)
                        else:
                            time.sleep(5)
                    else:
                        sheet.update_cell(row_number, out_column, f"Ошибка загрузки {res.status_code}")
                        time.sleep(5)
                except Exception as row_err:
                    log(f"Ошибка обработки строки {row_number}: {row_err}")
                    time.sleep(10)
                    
        if st.session_state["processing"]:
            st.success("🎉 Обработка всей таблицы успешно завершена!")
            st.session_state["processing"] = False
            st.rerun()
            
    except Exception as glob_err:
        st.error(f"Глобальная ошибка при чтении таблицы: {glob_err}")
        st.session_state["processing"] = False