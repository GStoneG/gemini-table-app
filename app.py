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

# Расширенный CSS для точечного скрытия GitHub и Fork
hide_github_and_fork = """
    <style>
    /* 1. Скрываем иконки и ссылки GitHub */
    #GithubIcon,
    a[href*="github.com"],
    a[aria-label*="GitHub"],
    a[title*="GitHub"] {
        display: none !important;
    }

    /* 2. Скрываем кнопку Fork и экшены шапки */
    button[title*="Fork"],
    button[aria-label*="Fork"],
    [data-testid="stAppHeaderAction"],
    [data-testid="stHeaderAction"] {
        display: none !important;
    }

    /* 3. Скрываем правый тулбар разработки (кнопки GitHub/Fork/Deploy) */
    [data-testid="stToolbar"] {
        display: none !important;
    }

    /* 4. Скрываем плашку "Made with Streamlit" / Viewer Badge */
    .viewerBadge_container__1QSob,
    [class*="viewerBadge_container"] {
        display: none !important;
    }

    /* 5. ВАЖНО: Сохраняем видимость шапки и формы авторизации */
    header[data-testid="stHeader"],
    [data-testid="stAppHeader"] {
        display: flex !important;
        background: transparent !important;
    }

    /* Гарантируем, что поля ввода логина/пароля в основном окне или сайдбаре видимы */
    [data-testid="stSidebar"],
    [data-testid="stMain"] {
        display: block !important;
    }
    </style>
"""

st.markdown(hide_github_and_fork, unsafe_allow_html=True)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С КОЛОНКАМИ И СТРОКАМИ ===
def col2num(col):
    """Преобразует букву колонки (A, B, AF...) в число (1, 2, 32...)"""
    if isinstance(col, int):
        return col
    if not col:
        return 1
    col_str = str(col).strip().upper()
    if col_str.isdigit():
        return int(col_str)
    num = 0
    for c in col_str:
        if 'A' <= c <= 'Z':
            num = num * 26 + (ord(c) - ord('A') + 1)
    return num if num > 0 else 1

def num2col(col):
    """Преобразует число колонки в букву (32 -> AF)"""
    if isinstance(col, str):
        col_str = col.strip().upper()
        if not col_str.isdigit():
            return col_str if col_str else "A"
        num = int(col_str)
    else:
        num = int(col)
    
    res = ""
    while num > 0:
        num, remainder = divmod(num - 1, 26)
        res = chr(65 + remainder) + res
    return res or "A"

def parse_target_rows(rows_str):
    """Парсит строку с номерами строк (например: '320, 325, 340' или '320-325') в множество чисел"""
    if not rows_str or not str(rows_str).strip():
        return set()
    rows = set()
    parts = re.split(r'[,;\s]+', str(rows_str).strip())
    for part in parts:
        if not part:
            continue
        if '-' in part:
            try:
                start, end = part.split('-', 1)
                rows.update(range(int(start), int(end) + 1))
            except ValueError:
                pass
        else:
            try:
                rows.add(int(part))
            except ValueError:
                pass
    return rows

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def sanitize_string(text):
    if not isinstance(text, str):
        return text
    text = text.strip()
    markdown_match = re.search(r'\[.*?\]\((.*?)\)', text)
    if markdown_match:
        return markdown_match.group(1).strip()
    if text.startswith("https://"):
        url_match = re.search(r'(https://[^\s\'\"\]\)]+)', text)
        if url_match:
            return url_match.group(1).strip()
    return text

def clean_json_text(text):
    """Очищает ответ Gemini от блоков разметки Markdown ```json ... ``` для корректного парсинга"""
    if not text:
        return ""
    cleaned = re.sub(r'```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def clear_profile_widget_keys():
    """Сбрасывает сохраненные значения полей в session_state для корректного обновления UI"""
    keys_to_clear = [k for k in st.session_state.keys() if any(k.startswith(prefix) for prefix in [
        "gemini_api_key_", "sheet_id_", "worksheet_name_", "url_col_", "out_col_",
        "target_rows_", "model_name_", "output_template_", "system_prompt_", "prompt_text_"
    ])]
    for k in keys_to_clear:
        del st.session_state[k]

def is_valid_profiles_dict(data):
    """Проверяет, является ли JSON словарем профилей, а не credentials.json"""
    if not isinstance(data, dict) or len(data) == 0:
        return False
    
    # Ключи, характерные для Google Service Account (credentials.json)
    service_account_keys = {"type", "project_id", "private_key_id", "private_key", "client_email"}
    if service_account_keys.issubset(data.keys()):
        return False
    
    # Проверяем, что значения профилей — это словари
    for profile_name, profile_content in data.items():
        if not isinstance(profile_content, dict):
            return False
            
    return True

# === БЛОК РАБОТЫ С НАСТРОЙКАМИ И ПРОФИЛЯМИ ===
CONFIGS_FILE = "app_profiles.json"

DEFAULT_PROFILES = {
    "Определение Партнеров": {
        "gemini_api_key": "Вставьте API токен Gemini",
        "sheet_id": "ID таблицы",
        "worksheet_name": "Лоты",
        "url_column": "AU",
        "out_column": "S",
        "target_rows": "",
        "model_name": "gemini-2.5-flash",
        "output_template": "Партнер - {partner}",
        "system_prompt": (
            "Ты — специализированный ИИ-аналитик по аудиту тендерной документации и спецификаций.\n\n"
            "Твоя единственная задача — проанализировать текст предоставленного PDF-документа, "
            "найти в нем присутствие специфических ключевых слов, фраз или тем и на основе этого строго определить Партнера.\n\n"
            "Правила классификации:\n"
            "1. 'Заман Эдюкейшен' — если в тексте встречаются темы: Цикл Борна-Габера, Спектроскопия ЯМР, Стеариновая кислота, Реакции нуклеофильного замещения, Сердце человека, Лямблия, Нефрон, Азиатский слон, Фетальная свинья, Движение тел по окружности, Динамика движения тел по окружности, Потенциометр, Планетарная коробка передач.\n"
            "2. 'Медиа Холл' — если в тексте встречаются темы: Астрономия (Цефеиды, Звездный параллакс, Луноход Аполлон-15), Ботаника (Внутреннее строение листа/стебля), Барометр-анероид, Видикон, Волосной гигрометр, Гигрометр психрометрический, Аллотропные модификации углерода, Бензилпенициллин, Гуанозиндифосфат.\n"
            "3. 'КазРобоЛаб' — если в тексте встречаются темы: Бицепс, Вены человека, Моногибридное скрещивание, Бабочка Махаон, Вирус Марбург/бешенства, Лошадь, Люцерна, Сальтаторная проводимость, Изохорный процесс, Соленоиды, Спектр электромагнитных волн, Камера Вильсона, Топливная ячейка, Закон Кулона (Крутильные весы).\n\n"
            "Критически важные правила:\n"
            "- Обращай внимание на маркерные (уникальные) слова для каждого партнера.\n"
            "- Если в тексте нет явного упоминания этих специфических учебных тем и терминов, или текст относится к совершенно другой тематике, возвращай строго 'Не определен'.\n"
            "- Ответ должен быть возвращен СТРОГО в формате JSON, без какого-либо Markdown-форматирования (без ```json), комментариев или объяснений."
        ),
        "prompt_text": (
            "Проанализируй текст загруженного документа. Проверь его на наличие ключевых слов, "
            "учебных тем или фраз, соответствующих одному из трех партнеров: 'Заман Эдюкейшен', 'Медиа Холл' или 'КазРобоЛаб'.\n\n"
            "Верни результат строго в формате JSON со следующей структурой:\n"
            "{\n"
            "  \"partner\": \"Название определенного партнера (строго один из вариантов: 'Заман Эдюкейшен', 'Медиа Холл', 'КазРобоЛаб'). "
            "Если совпадений по ключевым паттернам не найдено, запиши 'Не определен'\"\n"
            "}"
        )
    },
    "Анализ компьютерной техники": {
        "gemini_api_key": "Вставьте API токен Gemini",
        "sheet_id": "ID таблицы",
        "worksheet_name": "Лоты",
        "url_column": "AU",
        "out_column": "S",
        "target_rows": "",
        "model_name": "gemini-2.5-flash",
        "output_template": "Тип: {type}; CPU: {cpu}; RAM: {ram}; SSD/HDD: {storage}; GPU: {gpu}; Экран: {screen}; ОС: {os}; Дополнительные программы: {po}; Периферия: {peripherals}",
        "system_prompt": (
            "Ты — первоклассный эксперт по компьютерному аппаратному обеспечению и архитектуре процессоров. "
            "Твоя главная задача — проанализировать числовые параметры процессора из текста госзакупок "
            "(количество ядер, потоков, базовую и максимальную частоту, кэш, шину, частоту встроенной графики) "
            "и на основе этих данных определить и написать ТОЧНУЮ КОММЕРЧЕСКУЮ МОДЕЛЬ ПРОЦЕССОРА (например, 'Intel Core i5-11400' или 'Intel Core i5-10400'). "
            "В тексте спецификаций модели намеренно скрывают за фразами 'не менее', но ты обязан вычислить конкретную модель на рынке, которая идеально подпадает под эти характеристики. "
            "Строго запрещено писать сухие параметры вроде 'не менее 6 ядер' или 'Не указано' в поле 'cpu', если в тексте есть частоты и ядра. "
            "Возвращай результат СТРОГО в формате JSON без комментариев и без разметки markdown (без ```json)."
        ),
        "prompt_text": (
            "Проанализируй текст спецификации и заполни следующие поля в формате JSON.\n"
            "Используй свои глубокие знания о процессорах Intel и AMD, чтобы сопоставить частоты и ядра с конкретным рыночным названием процессора.\n\n"
            "{\n"
            "  \"type\": \"Тип устройства (например: Моноблок, Ноутбук, Компьютер в комплекте)\",\n"
            "  \"cpu\": \"Точная модель процессора, которую ты определил по характеристикам (например: Intel Core i5-11400)\",\n"
            "  \"ram\": \"Объем и тип памяти (например: 16 ГБ DDR4)\",\n"
            "  \"storage\": \"Накопитель (например: 512 ГБ SSD)\",\n"
            "  \"gpu\": \"Видеокарта\",\n"
            "  \"screen\": \"Экран (Диагональ, разрешение, тип матрицы)\",\n"
            "  \"os\": \"Операционная система\",\n"
            "  \"po\": \"Дополнительные программы (Например: Microsoft Office Home and Business 2024, Антивирус\",\n"
            "  \"peripherals\": \"Периферия и доп. устройства (Клавиатура, мышь, наушники, веб-камера, коврик, если есть в тексте)\"\n"
            "}"
        )
    }
}

# === ФУНКЦИИ АВТОМАТИЧЕСКОГО СОХРАНЕНИЯ / ЗАГРУЗКИ ПРОФИЛЕЙ ===
def get_google_creds():
    """Получает Google Credentials из st.secrets, файла или из загруженного файла."""
    google_scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if "gcp_service_account" in st.secrets:
        try:
            return Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=google_scopes)
        except Exception:
            pass
    if os.path.exists("credentials.json"):
        try:
            return Credentials.from_service_account_file("credentials.json", scopes=google_scopes)
        except Exception:
            pass
    if "uploaded_creds_dict" in st.session_state and st.session_state["uploaded_creds_dict"]:
        try:
            return Credentials.from_service_account_info(st.session_state["uploaded_creds_dict"], scopes=google_scopes)
        except Exception:
            pass
    return None

def load_profiles_cloud_or_local():
    """Загрузка профилей: сначала из Google Диска, затем из файла, затем дефолтные."""
    creds = get_google_creds()
    if creds:
        try:
            client = gspread.authorize(creds)
            sh = client.open("AI_Parser_Profiles_Backup")
            ws = sh.sheet1
            val = ws.cell(1, 1).value
            if val:
                data = json.loads(val)
                if is_valid_profiles_dict(data):
                    return data
        except Exception:
            pass
            
    if os.path.exists(CONFIGS_FILE):
        try:
            with open(CONFIGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if is_valid_profiles_dict(data):
                    return data
        except Exception:
            pass

    return DEFAULT_PROFILES.copy()

def save_profiles_cloud_and_local(profiles_data):
    """Сохранение профилей локально + дублирование на Google Диск."""
    if not is_valid_profiles_dict(profiles_data):
        return
        
    st.session_state["profiles"] = profiles_data
    try:
        with open(CONFIGS_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles_data, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

    creds = get_google_creds()
    if creds:
        try:
            client = gspread.authorize(creds)
            try:
                sh = client.open("AI_Parser_Profiles_Backup")
            except Exception:
                sh = client.create("AI_Parser_Profiles_Backup")
            ws = sh.sheet1
            ws.update_cell(1, 1, json.dumps(profiles_data, ensure_ascii=False, indent=2))
        except Exception:
            pass

# Инициализация профилей в session_state
if "profiles" not in st.session_state:
    st.session_state["profiles"] = load_profiles_cloud_or_local()

profiles = st.session_state["profiles"]

# Инициализация динамических ключей для сброса виджетов (защита от бесконечного цикла)
if "uploader_key" not in st.session_state:
    st.session_state["uploader_key"] = 0
if "profile_creator_key" not in st.session_state:
    st.session_state["profile_creator_key"] = 0

# Инициализация конфигурации страницы
st.set_page_config(page_title="Универсальный AI Табличный Парсер", layout="wide", page_icon="📊")

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
st.title("📊 Универсальный ИИ Анализатор документов")

if st.session_state["username"]:
    st.sidebar.header("👤 Аккаунт")
    st.sidebar.write(f"Вы вошли как: **{st.session_state['username']}**")
    if st.sidebar.button("🚪 Выйти из системы", type="secondary"):
        st.session_state["authenticated"] = False
        st.session_state["username"] = ""
        st.session_state["processing"] = False
        st.rerun()
    st.sidebar.markdown("---")

# === УПРАВЛЕНИЕ ПРОФИЛЯМИ В СИДБАРЕ ===
st.sidebar.header("📂 Профили настроек")
profile_names = list(profiles.keys())

# Гарантируем корректный выбор профиля в session_state
if "selected_profile" not in st.session_state or st.session_state["selected_profile"] not in profile_names:
    st.session_state["selected_profile"] = profile_names[0]

selected_profile = st.sidebar.selectbox(
    "Выберите активный профиль", 
    profile_names, 
    index=profile_names.index(st.session_state["selected_profile"])
)
st.session_state["selected_profile"] = selected_profile

# Возможность создать новый профиль (с динамеческим ключом от зацикливания)
new_profile_name = st.sidebar.text_input(
    "➕ Создать новый профиль (введите имя)", 
    key=f"new_profile_input_{st.session_state['profile_creator_key']}"
)
if st.sidebar.button("Создать профиль", use_container_width=True):
    if new_profile_name.strip() and new_profile_name not in profiles:
        profiles[new_profile_name] = profiles[selected_profile].copy()
        save_profiles_cloud_and_local(profiles)
        clear_profile_widget_keys()
        st.session_state["selected_profile"] = new_profile_name
        st.session_state["profile_creator_key"] += 1  # Сбрасываем поле ввода
        st.success(f"Профиль '{new_profile_name}' успешно создан!")
        time.sleep(0.5)
        st.rerun()
    elif new_profile_name in profiles:
        st.warning("Профиль с таким именем уже существует!")

# Кнопка удаления профиля
if len(profiles) > 1:
    if st.sidebar.button("🗑️ Удалить текущий профиль", type="secondary", use_container_width=True):
        del profiles[selected_profile]
        save_profiles_cloud_and_local(profiles)
        clear_profile_widget_keys()
        st.session_state["selected_profile"] = list(profiles.keys())[0]
        st.success("Профиль успешно удален!")
        time.sleep(0.5)
        st.rerun()

# === БЛОК ИМПОРТА И ЭКСПОРТА БЭКАПА ПРОФИЛЕЙ ===
st.sidebar.markdown("---")
st.sidebar.subheader("💾 Бэкап и Восстановление профилей")

st.sidebar.download_button(
    label="📥 Скачать бэкап профилей (JSON)",
    data=json.dumps(profiles, ensure_ascii=False, indent=4),
    file_name="app_profiles.json",
    mime="application/json",
    use_container_width=True
)

# Загрузчик с уникальным ключом для сброса состояния
uploaded_profiles_backup = st.sidebar.file_uploader(
    "Загрузить бэкап профилей (.json)", 
    type=["json"],
    key=f"backup_uploader_{st.session_state['uploader_key']}"
)

if uploaded_profiles_backup is not None:
    try:
        imported_data = json.load(uploaded_profiles_backup)
        if is_valid_profiles_dict(imported_data):
            save_profiles_cloud_and_local(imported_data)
            clear_profile_widget_keys()
            st.session_state["selected_profile"] = list(imported_data.keys())[0]
            # Увеличиваем счетчик ключа, чтобы очстить file_uploader и не допустить бесконечного цикла
            st.session_state["uploader_key"] += 1
            st.sidebar.success("Профили успешно загружены!")
            time.sleep(0.5)
            st.rerun()
        else:
            st.sidebar.error("Ошибка: загруженный файл не является файлом профилей! (Возможно, выбран credentials.json)")
    except Exception as err:
        st.sidebar.error(f"Ошибка при чтении JSON: {err}")

# Загружаем настройки активного профиля
active_config = profiles[selected_profile]

st.sidebar.header("🔑 Настройки доступа")
gemini_api_key = st.sidebar.text_input(
    "Gemini API Key", 
    value=active_config.get("gemini_api_key", ""), 
    type="password",
    key=f"gemini_api_key_{selected_profile}"
)
uploaded_creds = st.sidebar.file_uploader(
    "Загрузите credentials.json (необязательно, если настроен st.secrets)", 
    type=["json"],
    key=f"uploaded_creds_{selected_profile}"
)

if uploaded_creds:
    try:
        uploaded_creds.seek(0)
        st.session_state["uploaded_creds_dict"] = json.load(uploaded_creds)
    except Exception:
        pass

st.header(f"⚙️ Конфигурация: {selected_profile}")
col1, col2 = st.columns(2)
with col1:
    sheet_id = st.text_input(
        "Google Sheet ID", 
        value=active_config.get("sheet_id", ""),
        key=f"sheet_id_{selected_profile}"
    )
    worksheet_name = st.text_input(
        "Название листа", 
        value=active_config.get("worksheet_name", ""),
        key=f"worksheet_name_{selected_profile}"
    )
    target_rows_input = st.text_input(
        "Номера строк для обработки (например: 320, 325, 340 или пусто для всех)", 
        value=str(active_config.get("target_rows", "")),
        key=f"target_rows_{selected_profile}"
    )

with col2:
    url_col_input = st.text_input(
        "Буква колонки со ссылками на PDF (A, B, AF...)", 
        value=num2col(active_config.get("url_column", "AF")),
        key=f"url_col_{selected_profile}"
    )
    out_col_input = st.text_input(
        "Буква колонки для вывода результата (A, B, L...)", 
        value=num2col(active_config.get("out_column", "L")),
        key=f"out_col_{selected_profile}"
    )

url_column = col2num(url_col_input)
out_column = col2num(out_col_input)

model_options = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash", "gemini-1.5-pro"]
saved_model = active_config.get("model_name")
model_index = model_options.index(saved_model) if saved_model in model_options else 0
model_name = st.selectbox("Версия модели Gemini", model_options, index=model_index, key=f"model_name_{selected_profile}")

# === УНИВЕРСАЛЬНЫЙ ШАБЛОН ВЫВОДА ===
output_template = st.text_input(
    "Шаблон вывода в ячейку таблицы (Используйте плейсхолдеры в фигурных скобках {})", 
    value=active_config.get("output_template", "{partner}"),
    key=f"output_template_{selected_profile}"
)
st.caption(
    "Пример: `Партнер - {partner}` или `CPU: {cpu}; RAM: {ram}`. "
    "Названия полей внутри скобок {} должны в точности совпадать с ключами JSON из промта ниже."
)

system_prompt = st.text_area(
    "Системная инструкция (System Instruction)", 
    value=active_config.get("system_prompt", ""), 
    height=180,
    key=f"system_prompt_{selected_profile}"
)
prompt_text = st.text_area(
    "Основной текст промта (Ожидаемая JSON структура)", 
    value=active_config.get("prompt_text", ""), 
    height=270,
    key=f"prompt_text_{selected_profile}"
)

# Сохранение настроек в активный профиль
if st.button("💾 Сохранить изменения в текущий профиль", use_container_width=True):
    profiles[selected_profile] = {
        "gemini_api_key": gemini_api_key,
        "sheet_id": sheet_id,
        "worksheet_name": worksheet_name,
        "url_column": num2col(url_col_input),
        "out_column": num2col(out_col_input),
        "target_rows": target_rows_input,
        "model_name": model_name,
        "output_template": output_template,
        "system_prompt": system_prompt,
        "prompt_text": prompt_text
    }
    save_profiles_cloud_and_local(profiles)
    st.success("✅ Изменения в профиле успешно сохранены!")
    time.sleep(0.5)
    st.rerun()

st.markdown("---")

# === УПРАВЛЕНИЕ КНОПКАМИ СТАРТ / СТОП ===
if not st.session_state["processing"]:
    if st.button("🚀 Запустить потоковую обработку таблицы", type="primary", use_container_width=True):
        if not gemini_api_key or gemini_api_key == "Вставьте API токен Gemini":
            st.error("Критическая ошибка: Заполните Gemini API Key!")
        elif not get_google_creds():
            st.error("Критическая ошибка: Файл credentials.json не найден ни в st.secrets, ни в папке, ни в загрузчике!")
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
        
        creds = get_google_creds()
        if not creds:
            st.error("Ошибка аутентификации Google API. Загрузите credentials.json.")
            st.session_state["processing"] = False
            st.stop()
            
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
        
        target_rows = parse_target_rows(target_rows_input)
        if target_rows:
            log(f"🎯 Выбраны строки для обработки: {sorted(list(target_rows))}")
        else:
            log("ℹ️ Список строк пуст: обработка всех незаполненных ячеек.")
        
        for index in range(1, total_rows):
            if not st.session_state["processing"]:
                log("🛑 Обработка была принудительно прервана пользователем.")
                break
                
            row_number = index + 1
            
            # Пропускаем строки, если задан фильтр и текущая строка в него не входит
            if target_rows and row_number not in target_rows:
                continue

            row = all_rows[index]
            progress_bar.progress(index / (total_rows - 1))
            
            raw_url_cell = row[url_column - 1] if len(row) >= url_column else ""
            current_result = row[out_column - 1] if len(row) >= out_column else ""
            
            raw_url_str = str(raw_url_cell).strip()
            current_result_str = str(current_result).strip()
            
            # Определяем целевые строки
            should_process = bool(raw_url_str) if target_rows else bool(raw_url_str and not current_result_str)
            
            if should_process:
                download_url = raw_url_str
                
                if download_url.startswith('='):
                    match = re.search(r'HYPERLINK\(\s*["\']([^"\']+)["\']', download_url, re.IGNORECASE)
                    if match: 
                        download_url = match.group(1).strip()
                
                log(f"[{row_number}/{total_rows}] Скачивание: {download_url[:50]}...")
                
                try:
                    # ПОТОКОВОЕ СКАЧИВАНИЕ С ОГРАНИЧЕНИЕМ ПО РАЗМЕРУ В 15 МБ
                    res = requests.get(download_url, headers={'User-Agent': 'Mozilla/5.0'}, stream=True, timeout=20)
                    
                    if res.status_code == 200:
                        content_length = res.headers.get('content-length')
                        if content_length and int(content_length) > 15 * 1024 * 1024:
                            raise Exception("Файл слишком большой (размер по заголовку > 15MB)")
                            
                        pdf_bytes = bytearray()
                        for chunk in res.iter_content(chunk_size=16384):
                            if not st.session_state["processing"]:
                                break
                            pdf_bytes.extend(chunk)
                            if len(pdf_bytes) > 15 * 1024 * 1024:
                                raise Exception("Размер файла превысил предел в 15MB при загрузке")
                        
                        if not pdf_bytes.startswith(b'%PDF'):
                            try:
                                sample_text = pdf_bytes[:500].decode('utf-8', errors='ignore')
                                if "<html" in sample_text.lower() or "<!doctype" in sample_text.lower():
                                    raise Exception("Сайт вернул страницу блокировки (HTML) вместо файла PDF")
                            except Exception:
                                pass
                            raise Exception("Загруженный файл не является валидным PDF")

                        log(f"-> Скачано ({len(pdf_bytes) / (1024 * 1024):.2f} МБ). Отправка в Gemini API...")
                        
                        for attempt in range(3):
                            if not st.session_state["processing"]:
                                break

                            try:
                                api_res = model.generate_content([
                                    {'mime_type': 'application/pdf', 'data': bytes(pdf_bytes)}, 
                                    prompt_text
                                ], 
                                generation_config=genai.types.GenerationConfig(response_mime_type="application/json"),
                                request_options={"timeout": 120}
                                )
                                
                                log("-> Ответ от Gemini получен. Парсинг...")
                                
                                cleaned_raw_json = clean_json_text(api_res.text)
                                data = json.loads(cleaned_raw_json)
                                
                                # === УНИВЕРСАЛЬНОЕ ФОРМАТИРОВАНИЕ ВЫВОДА ===
                                if isinstance(data, dict) and output_template.strip():
                                    try:
                                        keys_needed = re.findall(r'\{(.*?)\}', output_template)
                                        format_dict = {k: data.get(k, "—") for k in keys_needed}
                                        formatted_str = output_template.format(**format_dict)
                                    except Exception as format_err:
                                        log(f"Предупреждение: не удалось отформатировать по шаблону ({format_err}). Вывод в сыром виде.")
                                        formatted_str = str(data)
                                else:
                                    formatted_str = "; ".join([f"{k}: {v}" for k, v in data.items()]) if isinstance(data, dict) else str(data)
                                
                                # Запись в Google Таблицу
                                log("-> Запись результата в Google Таблицу...")
                                sheet.update_cell(row_number, out_column, formatted_str)
                                log(f"-> Строка {row_number}: Успешно записано: {formatted_str[:60]}...")
                                request_count += 1
                                break
                            except Exception as api_err:
                                if "429" in str(api_err):
                                    wait_time = (2 ** attempt) * 20
                                    log(f"Превышен лимит (429)! Ожидание {wait_time} сек...")
                                    time.sleep(wait_time)
                                else:
                                    log(f"Ошибка Gemini / JSON: {api_err}")
                                    sheet.update_cell(row_number, out_column, f"Ошибка API/JSON: {str(api_err)[:100]}")
                                    break
                                    
                        if request_count > 0 and request_count % 10 == 0:
                            log("Плановая пауза 60 секунд для защиты лимитов...")
                            time.sleep(60)
                        else:
                            time.sleep(5)
                    else:
                        error_msg = f"Ошибка загрузки: код сервера {res.status_code}"
                        log(f"-> {error_msg}")
                        sheet.update_cell(row_number, out_column, error_msg)
                        time.sleep(5)
                except Exception as row_err:
                    error_msg = f"Ошибка: {row_err}"
                    log(f"-> {error_msg}")
                    try:
                        sheet.update_cell(row_number, out_column, f"Ошибка обработки: {str(row_err)[:100]}")
                    except Exception as sheet_err:
                        log(f"Не удалось записать статус ошибки в таблицу: {sheet_err}")
                    time.sleep(5)
                    
        if st.session_state["processing"]:
            st.success("🎉 Обработка выбранных строк/таблицы успешно завершена!")
            st.session_state["processing"] = False
            st.rerun()
            
    except Exception as glob_err:
        st.error(f"Глобальная ошибка при чтении таблицы: {glob_err}")
        st.session_state["processing"] = False
